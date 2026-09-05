"""Recipe -> costed, nutrition-bearing dish.

Pipeline for one recipe:

    ingredient line -> URT parse (grams, gross)
                    -> TKPI match (composition, BDD)
                    -> edible grams  = gross x BDD/100
                    -> nutrients     = edible grams x per-100g composition
                    -> purchase cost = gross grams x price/kg
                    -> retention factors for the cooking method
                    -> divide by estimated servings

Two modelling choices are worth stating plainly, because they are the main
source of error in the output and both are visible in `DishQuality`:

**Servings.** The corpus records no yield. Servings are estimated from the mass
of the component that defines the dish's tray slot, divided by the Isi Piringku
reference portion for that slot — 400 g of fish at a 75 g animal-protein
portion is ~5 servings. Everything in the pot (vegetables, seasoning, oil) is
then divided by that number, which is what actually happens at serving time.

**Retention.** TKPI values are raw-basis. Applying them unadjusted to a cooked
dish is what FAO/INFOODS calls the lowest-quality shortcut, so category-level
retention factors are applied per inferred cooking method. These are explicitly
approximations (FAO/INFOODS says so itself) and are reported as such.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from .config import REFERENCE_PORTION_G, SLOTS
from .prices import PriceTable
from .tkpi import NUTRIENT_KEYS, FoodDB, MatchResult
from .urt import Confidence, ParsedIngredient, parse_ingredient_block

# ---------------------------------------------------------------- retention

# Nutrient retention factors by processing class (FAO/INFOODS Module 8;
# USDA Table of Nutrient Retention Factors r.6). Applied to the labile
# nutrients only; energy and macronutrients are treated as conserved.
RETENTION_FACTORS = {
    "moist": {"vit_c_mg": 0.50, "thiamin_mg": 0.70, "riboflavin_mg": 0.80,
              "niacin_mg": 0.75, "vit_a_mcg": 0.90, "iron_mg": 0.85,
              "potassium_mg": 0.80},
    "dry":   {"vit_c_mg": 0.75, "thiamin_mg": 0.80, "riboflavin_mg": 0.90,
              "niacin_mg": 0.85, "vit_a_mcg": 0.90},
    "fat":   {"vit_c_mg": 0.75, "thiamin_mg": 0.80, "riboflavin_mg": 0.85,
              "niacin_mg": 0.85, "vit_a_mcg": 0.85},
    "raw":   {},
}

_METHOD_PATTERNS = (
    ("fat",   r"\bgoreng|menggoreng|tumis|menumis|sangrai|deep fry\b"),
    ("moist", r"\brebus|merebus|kukus|mengukus|ungkep|didihkan|simmer|kuah|santan\b"),
    ("dry",   r"\bbakar|membakar|panggang|memanggang|oven|grill\b"),
)


def infer_cooking_method(steps: str) -> str:
    """Classify a recipe's dominant cooking method from its instructions."""
    text = str(steps).lower()
    scores = {m: len(re.findall(p, text)) for m, p in _METHOD_PATTERNS}
    best = max(scores, key=scores.get)
    return best if scores[best] else "raw"


# ---------------------------------------------------------------- records


@dataclass
class IngredientLine:
    """One recipe line, fully resolved."""

    parsed: ParsedIngredient
    match: Optional[MatchResult]
    gross_g: float            # as-purchased weight
    edible_g: float           # what reaches the plate
    cost_idr: float
    cost_basis: str
    nutrients: dict[str, float] = field(default_factory=dict)
    # Set when a line is rehydrated from the database, where the TKPI name was
    # persisted but the Food object is not reconstructed.
    food_name_override: str = ""

    @property
    def name(self) -> str:
        return self.parsed.name

    @property
    def matched(self) -> bool:
        return self.match is not None

    @property
    def food_name(self) -> str:
        if self.match:
            return self.match.food.name
        return self.food_name_override or self.parsed.name

    @property
    def code(self) -> Optional[str]:
        return self.match.food.code if self.match else None


@dataclass
class DishQuality:
    """How much of this dish's numbers rest on solid ground."""

    lines_total: int
    lines_matched: int
    mass_matched_share: float      # share of gross mass with a TKPI match
    unquantified_share: float      # share of lines that were "secukupnya"
    piece_default_share: float     # share of lines using a fallback piece weight
    proxy_matches: int
    servings_estimated: bool = True
    # Set when loading from the database, where the score was already computed
    # from the complete line records.
    stored_score: Optional[float] = None

    @property
    def score(self) -> float:
        """0-1 confidence, weighted toward mass coverage."""
        if self.stored_score is not None:
            return self.stored_score
        return round(
            0.6 * self.mass_matched_share
            + 0.25 * (1.0 - self.unquantified_share)
            + 0.15 * (1.0 - self.piece_default_share),
            3,
        )

    @property
    def label(self) -> str:
        s = self.score
        return "high" if s >= 0.80 else "medium" if s >= 0.60 else "low"


@dataclass
class Dish:
    """A corpus recipe costed and scaled to one MBG portion."""

    dish_id: int
    name: str
    province: str
    island: str
    slot: str
    servings: int
    method: str
    lines: list[IngredientLine]
    per_portion: dict[str, float]       # nutrients per portion
    cost_per_portion_idr: float
    portion_mass_g: float               # edible grams per portion
    quality: DishQuality
    steps: str = ""
    confidence: str = ""                # corpus region-assignment confidence
    # Lowercased haystack of every ingredient term in the dish, kept so that
    # allergy/preference filtering never has to load the full line records.
    ingredient_index: str = ""
    # The single ingredient that defines the dish (heaviest component-group
    # line). Used to keep a week from becoming five variations on tempe.
    main_food_code: str = ""
    main_food_name: str = ""

    # ---------------------------------------------------------------- helpers
    @property
    def energy_kcal(self) -> float:
        return self.per_portion.get("energy_kcal", 0.0)

    @property
    def protein_g(self) -> float:
        return self.per_portion.get("protein_g", 0.0)

    @property
    def fat_g(self) -> float:
        return self.per_portion.get("fat_g", 0.0)

    def scaled_ingredients(self, portions: int) -> list[tuple[str, float, float]]:
        """Procurement list for `portions` servings: (name, gross_g, cost_idr)."""
        factor = portions / self.servings
        return [(ln.food_name, ln.gross_g * factor, ln.cost_idr * factor)
                for ln in self.lines]

    def to_dict(self) -> dict:
        return {
            "dish_id": self.dish_id, "name": self.name, "province": self.province,
            "island": self.island, "slot": self.slot, "servings": self.servings,
            "method": self.method,
            "cost_per_portion_idr": round(self.cost_per_portion_idr or 0.0, 1),
            "portion_mass_g": round(self.portion_mass_g or 0.0, 1),
            "per_portion": {k: round(v, 3) for k, v in self.per_portion.items()},
            "quality": self.quality.score, "quality_label": self.quality.label,
        }


# ---------------------------------------------------------------- engine

# Which tray slot each TKPI group votes for when classifying a dish.
_SLOT_GROUPS = {
    "hewani": ("F", "G", "H", "J"),
    "nabati": ("C",),
    "sayur": ("D",),
    "staple": ("A", "B"),
    "buah": ("E",),
}
# Groups that are cooking media / seasoning rather than tray components.
_NON_COMPONENT_GROUPS = ("K", "M", "N", "Q", "X")

MIN_SERVINGS, MAX_SERVINGS = 1, 40


class RecipeEngine:
    """Builds `Dish` records from raw corpus rows."""

    def __init__(self, foods: FoodDB, prices: PriceTable):
        self.foods = foods
        self.prices = prices

    # ------------------------------------------------------------ one recipe
    def build(self, dish_id: int, name: str, ingredients: str, steps: str = "",
              province: str = "", island: str = "",
              confidence: str = "") -> Optional[Dish]:
        parsed = parse_ingredient_block(ingredients)
        if not parsed:
            return None

        method = infer_cooking_method(steps)
        retention = RETENTION_FACTORS[method]

        lines: list[IngredientLine] = []
        for item in parsed:
            if item.omitted:
                continue        # the cook explicitly left it out
            lines.append(self._resolve(item, retention))
        if not lines:
            return None

        _apply_frying_absorption(lines)

        slot, component_mass = self._classify(lines)
        if slot is None:
            return None
        main = _main_food(lines, slot)

        servings = self._estimate_servings(slot, component_mass)
        totals = _sum_nutrients(lines)
        total_cost = sum(ln.cost_idr for ln in lines)
        total_edible = sum(ln.edible_g for ln in lines)

        return Dish(
            dish_id=dish_id, name=name, province=province, island=island,
            slot=slot, servings=servings, method=method, lines=lines,
            per_portion={k: v / servings for k, v in totals.items()},
            cost_per_portion_idr=total_cost / servings,
            portion_mass_g=total_edible / servings,
            quality=_assess(lines), steps=steps, confidence=confidence,
            ingredient_index=_index_terms(lines),
            main_food_code=main[0], main_food_name=main[1],
        )

    # ------------------------------------------------------------ internals
    def _resolve(self, item: ParsedIngredient,
                 retention: dict[str, float]) -> IngredientLine:
        match = self.foods.match(item.name)
        gross = item.grams

        if match is None:
            cost, price = self.prices.cost_idr(gross, name=item.name)
            return IngredientLine(item, None, gross, 0.0, cost, price.basis, {})

        food = match.food
        edible = gross * (food.bdd / 100.0)
        nutrients = {
            key: food.per_gram(key) * edible * retention.get(key, 1.0)
            for key in NUTRIENT_KEYS
        }
        cost, price = self.prices.cost_idr(
            gross, code=food.code, name=item.name, group_code=food.group_code)
        return IngredientLine(item, match, gross, edible, cost, price.basis, nutrients)

    def _classify(self, lines: list[IngredientLine]) -> tuple[Optional[str], float]:
        """Pick the tray slot this dish fills, and the mass that defines it."""
        mass_by_slot = {slot: 0.0 for slot in SLOTS}
        for line in lines:
            if not line.matched:
                continue
            group = line.match.food.group_code
            if group in _NON_COMPONENT_GROUPS:
                continue
            for slot, groups in _SLOT_GROUPS.items():
                if group in groups:
                    mass_by_slot[slot] += line.edible_g
                    break

        total = sum(mass_by_slot.values())
        if total <= 0:
            return None, 0.0
        share = {slot: mass / total for slot, mass in mass_by_slot.items()}

        # Order matters. A sayur bening with a spoon of teri in it is still the
        # vegetable, so a clearly vegetable-dominated dish is claimed first;
        # otherwise animal and plant protein define the dish even when a
        # vegetable out-weighs them (80 g of chicken in a 400 g stew is the lauk).
        if share["sayur"] >= 0.50 and share["hewani"] < 0.25:
            return "sayur", mass_by_slot["sayur"]
        if share["staple"] >= 0.55 and share["hewani"] < 0.20:
            return "staple", mass_by_slot["staple"]
        # Both protein slots are tested together and the larger one wins, so a
        # tempe dish with an egg in the batter stays a lauk nabati.
        protein_claims = [(slot, share[slot]) for slot, floor
                          in (("hewani", 0.18), ("nabati", 0.22))
                          if share[slot] >= floor]
        if protein_claims:
            slot = max(protein_claims, key=lambda kv: kv[1])[0]
            return slot, mass_by_slot[slot]

        slot = max(mass_by_slot, key=mass_by_slot.get)
        return (slot, mass_by_slot[slot]) if mass_by_slot[slot] > 0 else (None, 0.0)

    @staticmethod
    def _estimate_servings(slot: str, component_mass: float) -> int:
        reference = REFERENCE_PORTION_G[slot]
        servings = int(round(component_mass / reference)) if reference else 1
        return max(MIN_SERVINGS, min(MAX_SERVINGS, servings))

    # ------------------------------------------------------------ batch
    def build_many(self, rows: Iterable[dict]) -> list[Dish]:
        dishes = []
        for i, row in enumerate(rows):
            dish = self.build(
                dish_id=row.get("dish_id", i),
                name=row.get("name", ""),
                ingredients=row.get("ingredients", ""),
                steps=row.get("steps", ""),
                province=row.get("province", ""),
                island=row.get("island", ""),
                confidence=row.get("confidence", ""),
            )
            if dish is not None:
                dishes.append(dish)
        return dishes


# Frying oil sits in the pan; only a fraction ends up in the food. Deep-fried
# Indonesian foods take up roughly 8-12% of their own weight in oil (Bognar,
# Tables on weight yield and retention factors). Without this cap a recipe that
# lists "500 ml minyak goreng" for deep-frying reports every one of those
# calories as eaten, which overstates fat several-fold.
OIL_ABSORPTION_RATE = 0.10
_FAT_GROUP = "K"


def _apply_frying_absorption(lines: list[IngredientLine]) -> None:
    """Scale oil lines down to the mass the food actually absorbs.

    Cost is deliberately left at the full purchased amount — the kitchen buys
    all of it — while nutrition counts only the absorbed share.
    """
    oil_lines = [ln for ln in lines
                 if ln.matched and ln.match.food.group_code == _FAT_GROUP]
    if not oil_lines:
        return

    food_mass = sum(ln.edible_g for ln in lines if ln not in oil_lines)
    absorbable = food_mass * OIL_ABSORPTION_RATE
    declared = sum(ln.edible_g for ln in oil_lines)
    if declared <= absorbable or declared <= 0:
        return                      # a normal saute amount: all of it is eaten

    keep = absorbable / declared
    for line in oil_lines:
        line.nutrients = {k: v * keep for k, v in line.nutrients.items()}
        line.edible_g *= keep
        line.parsed.notes.append(
            f"frying oil: {keep:.0%} counted as absorbed, full amount costed")


# ---------------------------------------------------------------- components

# Staple and fruit reach an MBG tray as served components, not as recipes: rice
# is cooked plain, fruit is cut. The corpus therefore has almost none of them,
# and they are built directly from TKPI instead.
#
# `factor` is the Kemenkes matang->mentah conversion (Pedoman Konversi Berat
# Matang-Mentah, 2014): raw weight = cooked weight x factor. Nasi is 0.4, so a
# 150 g serving of rice is 60 g of raw beras — which is both what TKPI's AR001
# describes and what the kitchen buys.
COMPONENT_DISHES = [
    # (name, TKPI code, slot, served grams, matang->mentah factor)
    ("Nasi putih",        "AR001", "staple", 150.0, 0.40),
    ("Nasi merah",        "AR013", "staple", 150.0, 0.40),
    ("Nasi jagung",       "AR005", "staple", 150.0, 0.45),
    ("Ubi jalar rebus",   "BR030", "staple", 150.0, 1.00),
    ("Kentang rebus",     "BR013", "staple", 150.0, 1.00),
    ("Singkong rebus",    "BR016", "staple", 150.0, 1.00),
    ("Jagung rebus",      "AR015", "staple", 150.0, 1.00),
    ("Bihun",             "AP006", "staple", 150.0, 0.35),
    ("Pisang",            "ER074", "buah",   100.0, 1.00),
    ("Pepaya",            "ER073", "buah",   100.0, 1.00),
    ("Jeruk manis",       "ER039", "buah",   100.0, 1.00),
    ("Semangka",          "ER105", "buah",   100.0, 1.00),
    ("Melon",             "ER067", "buah",   100.0, 1.00),
    ("Salak",             "ER101", "buah",   100.0, 1.00),
    ("Rambutan",          "ER097", "buah",   100.0, 1.00),
    ("Mangga",            "ER054", "buah",   100.0, 1.00),
    ("Nanas",             "ER070", "buah",   100.0, 1.00),
    ("Apel",              "ER004", "buah",   100.0, 1.00),
    ("Jambu biji",        "ER031", "buah",   100.0, 1.00),
    ("Belimbing",         "ER006", "buah",   100.0, 1.00),
    ("Sawo",              "ER104", "buah",   100.0, 1.00),
    ("Susu sapi segar",   "JR006", "hewani", 200.0, 1.00),
]

COMPONENT_ID_BASE = 900_000

# Served components come in portion sizes, and choosing the size is part of the
# planning problem — an SPPG plates more rice for SMA than for PAUD. Offering
# each component at several sizes lets the optimiser scale the tray to the age
# band instead of forcing one fixed gram figure to satisfy every cohort.
COMPONENT_PORTION_VARIANTS = {
    "staple": (100.0, 150.0, 200.0, 250.0),
    "buah":   (75.0, 100.0, 150.0),
    "hewani": (200.0,),
}


def build_component_dishes(foods: FoodDB, prices: PriceTable) -> list[Dish]:
    """Plain served components (rice, boiled tubers, cut fruit, milk)."""
    dishes: list[Dish] = []
    specs = [
        (name, code, slot, portion, factor)
        for name, code, slot, base_g, factor in COMPONENT_DISHES
        for portion in COMPONENT_PORTION_VARIANTS.get(slot, (base_g,))
    ]
    for offset, (base_name, code, slot, served_g, factor) in enumerate(specs):
        food = foods.by_code.get(code)
        if food is None:
            continue
        name = f"{base_name} ({served_g:.0f} g)"
        edible_g = served_g * factor
        gross_g = food.purchase_grams(edible_g)
        cost, price = prices.cost_idr(gross_g, code=code, name=name,
                                      group_code=food.group_code)
        nutrients = {key: food.per_gram(key) * edible_g for key in NUTRIENT_KEYS}

        item = ParsedIngredient(
            raw=f"{gross_g:.0f} g {food.name}", name=name,
            quantity=round(gross_g, 1), unit="g", grams=gross_g,
            confidence=Confidence.EXACT, rule="component:reference_portion",
        )
        line = IngredientLine(item, foods.match(name) or MatchResult(food, 100.0, "alias"),
                              gross_g, edible_g, cost, price.basis, nutrients)
        quality = DishQuality(1, 1, 1.0, 0.0, 0.0, 0, servings_estimated=False)
        dishes.append(Dish(
            dish_id=COMPONENT_ID_BASE + offset, name=name, province="Nasional",
            island="Nasional", slot=slot, servings=1, method="raw",
            lines=[line], per_portion=nutrients, cost_per_portion_idr=cost,
            portion_mass_g=served_g, quality=quality,
            steps="Disajikan langsung sesuai porsi rujukan Isi Piringku.",
            confidence="High", ingredient_index=_index_terms([line]),
            main_food_code=food.code, main_food_name=food.name,
        ))
    return dishes


def _sum_nutrients(lines: list[IngredientLine]) -> dict[str, float]:
    totals = {key: 0.0 for key in NUTRIENT_KEYS}
    for line in lines:
        for key, value in line.nutrients.items():
            totals[key] += value
    return totals


def _main_food(lines: list[IngredientLine], slot: str) -> tuple[str, str]:
    """The heaviest ingredient belonging to the dish's own slot."""
    groups = _SLOT_GROUPS.get(slot, ())
    best = None
    for line in lines:
        if not line.matched or line.match.food.group_code not in groups:
            continue
        if best is None or line.edible_g > best.edible_g:
            best = line
    if best is None:
        return "", ""
    return best.match.food.code, best.match.food.name


def _index_terms(lines: list[IngredientLine]) -> str:
    """Searchable blob of every ingredient term, for exclusion filtering."""
    terms = set()
    for line in lines:
        terms.add(line.parsed.name.lower())
        if line.match:
            terms.add(line.match.food.norm_name)
    return " | ".join(sorted(t for t in terms if t))


def _assess(lines: list[IngredientLine]) -> DishQuality:
    total_mass = sum(ln.gross_g for ln in lines) or 1.0
    matched_mass = sum(ln.gross_g for ln in lines if ln.matched)
    n = len(lines)
    return DishQuality(
        lines_total=n,
        lines_matched=sum(1 for ln in lines if ln.matched),
        mass_matched_share=matched_mass / total_mass,
        unquantified_share=sum(1 for ln in lines if ln.parsed.unquantified) / n,
        piece_default_share=sum(
            1 for ln in lines
            if ln.parsed.confidence is Confidence.PIECE_DEFAULT) / n,
        proxy_matches=sum(1 for ln in lines
                          if ln.match and ln.match.method == "proxy"),
    )
