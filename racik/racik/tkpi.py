"""TKPI 2020 — Tabel Komposisi Pangan Indonesia.

Loads the official composition table and resolves free-text Indonesian recipe
ingredient names onto TKPI codes.

Two facts about TKPI shape everything here:

1. Values are per 100 g of **edible portion**, with BDD (Berat Dapat Dimakan)
   giving the edible share of the as-purchased food. Nutrition is therefore
   computed on edible grams, while *cost* must be charged on purchase grams:

       purchase_g = edible_g / (BDD/100)

   Chicken is the clearest case: "Ayam, daging, segar" is BDD 58%, so 75 g of
   edible chicken means buying ~129 g of bone-in chicken.

2. Names are **inverted and comma-qualified**: beef is "Sapi, daging, lemak
   sedang, segar", not "daging sapi". Plain substring matching is not merely
   imprecise here, it is wrong — the substring "ayam" matches "Bayam" (spinach).
   Matching therefore runs on token sets, never on substrings.
"""
from __future__ import annotations

import csv
import math
import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from rapidfuzz import fuzz, process

from .urt import normalize

# ---------------------------------------------------------------- schema

# TKPI CSV column -> canonical nutrient key.
NUTRIENT_COLUMNS = {
    "Energi (Kal)": "energy_kcal",
    "Protein (g)": "protein_g",
    "Lemak (g)": "fat_g",
    "KH (g)": "carb_g",
    "Serat (g)": "fibre_g",
    "Kalsium (mg)": "calcium_mg",
    "Fosfor (mg)": "phosphorus_mg",
    "Besi (mg)": "iron_mg",
    "Natrium (mg)": "sodium_mg",
    "Kalium (mg)": "potassium_mg",
    "Seng (mg)": "zinc_mg",
    "Vit_C (mg)": "vit_c_mg",
    "Thiamin (mg)": "thiamin_mg",
    "Riboflavin (mg)": "riboflavin_mg",
    "Niasin (mg)": "niacin_mg",
    "Air (g)": "water_g",
}

NUTRIENT_KEYS = tuple(NUTRIENT_COLUMNS.values()) + ("vit_a_mcg",)

# Code prefix -> the Isi Piringku slot that food group naturally fills.
GROUP_SLOT = {
    "A": "staple",   # serealia
    "B": "staple",   # umbi berpati
    "C": "nabati",   # kacang / bean (tahu, tempe)
    "D": "sayur",    # sayuran
    "E": "buah",     # buah
    "F": "hewani",   # daging, unggas
    "G": "hewani",   # ikan, kerang, udang
    "H": "hewani",   # telur
    "J": "hewani",   # susu
    "K": None,       # lemak & minyak — a cooking medium, not a tray slot
    "M": None,       # gula
    "N": None,       # bumbu
    "Q": None,       # minuman
}

GROUP_LABEL = {
    "A": "Serealia", "B": "Umbi berpati", "C": "Kacang & biji", "D": "Sayuran",
    "E": "Buah", "F": "Daging & unggas", "G": "Ikan & seafood", "H": "Telur",
    "J": "Susu", "K": "Lemak & minyak", "M": "Gula", "N": "Bumbu",
    "Q": "Minuman",
}

# Processing words. If the query does not ask for them, an entry carrying them
# is a worse match than a plain "segar"/"mentah" entry.
_PROCESSED_MARKERS = (
    "goreng", "kering", "dendeng", "asin", "keripik", "masakan", "rebus",
    "kukus", "bakar", "asap", "kaleng", "manis", "instan", "bubuk", "sangan",
    "sangrai", "presto", "pindang", "abon", "kerupuk", "tepung",
)
_RAW_MARKERS = ("segar", "mentah")


@dataclass(frozen=True)
class Food:
    code: str
    name: str
    group_code: str
    group_label: str
    jenis: str
    source: str
    bdd: float                      # % edible, 0-100
    bdd_imputed: bool
    nutrients: dict[str, float]     # per 100 g EDIBLE portion
    tokens: frozenset[str] = field(default=frozenset(), compare=False)
    norm_name: str = field(default="", compare=False)

    @property
    def slot(self) -> Optional[str]:
        return GROUP_SLOT.get(self.group_code)

    @property
    def is_raw(self) -> bool:
        return any(m in self.norm_name for m in _RAW_MARKERS)

    def per_gram(self, key: str) -> float:
        """Nutrient per gram of edible portion."""
        return self.nutrients.get(key, 0.0) / 100.0

    def purchase_grams(self, edible_grams: float) -> float:
        """As-purchased weight needed to yield `edible_grams` of edible food."""
        if self.bdd <= 0:
            return edible_grams
        return edible_grams / (self.bdd / 100.0)


def _to_float(raw) -> Optional[float]:
    if raw is None:
        return None
    s = str(raw).strip().replace(",", ".")
    if not s or s.lower() in {"nan", "-", "tr", "na"}:
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return None if math.isnan(v) else v


# Foods that carry real nutritional weight but are absent from TKPI as such.
# Values are documented, not invented.
SYNTHETIC_FOODS = [
    # Salt: pure NaCl is 39.34% sodium by mass -> 39,340 mg Na per 100 g.
    dict(code="XX001", name="Garam dapur", group_code="N", jenis="TUNGGAL/SINGLE",
         source="RACIK-SYNTHETIC (NaCl stoichiometry)", bdd=100.0,
         nutrients={"sodium_mg": 39_340.0}),
    dict(code="XX002", name="Air", group_code="Q", jenis="TUNGGAL/SINGLE",
         source="RACIK-SYNTHETIC", bdd=100.0, nutrients={"water_g": 100.0}),
    # Bouillon powder: ~17 g sodium/100 g, plus a little salt-carrier starch.
    dict(code="XX003", name="Kaldu bubuk penyedap", group_code="N",
         jenis="OLAHAN/PRODUK/KOMPOSIT",
         source="RACIK-SYNTHETIC (label-typical)", bdd=100.0,
         nutrients={"energy_kcal": 200.0, "protein_g": 8.0, "carb_g": 40.0,
                    "sodium_mg": 17_000.0}),
]


class FoodDB:
    """The TKPI table plus the index used to resolve recipe names onto it."""

    def __init__(self, foods: list[Food]):
        self.foods = foods
        self.by_code = {f.code: f for f in foods}
        self._choices = [f.norm_name for f in foods]
        self._alias_cache: dict[str, Optional[MatchResult]] = {}

    # ------------------------------------------------------------ loading
    @classmethod
    def from_csv(cls, path: str | Path) -> "FoodDB":
        rows: list[dict] = []
        with open(path, newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                rows.append(row)

        # BDD is missing for 156 entries. Impute the median of the food's own
        # group rather than assuming 100%, and record that it was imputed.
        bdd_by_group: dict[str, list[float]] = {}
        for row in rows:
            bdd = _to_float(row.get("BDD (%)"))
            if bdd is not None:
                bdd_by_group.setdefault(row["Kode"][:1], []).append(bdd)
        group_median = {g: statistics.median(v) for g, v in bdd_by_group.items()}

        foods: list[Food] = []
        for row in rows:
            code = row["Kode"].strip()
            group_code = code[:1]
            bdd = _to_float(row.get("BDD (%)"))
            imputed = bdd is None
            if imputed:
                bdd = group_median.get(group_code, 100.0)

            nutrients = {}
            for col, key in NUTRIENT_COLUMNS.items():
                val = _to_float(row.get(col))
                nutrients[key] = 0.0 if val is None else val

            # Vitamin A as retinol equivalents: RE = retinol + beta-carotene/6.
            retinol = _to_float(row.get("Retinol (mcg)")) or 0.0
            beta_carotene = _to_float(row.get("B-Kar (mcg)")) or 0.0
            nutrients["vit_a_mcg"] = retinol + beta_carotene / 6.0

            foods.append(_make_food(
                code=code, name=row["Nama Bahan"].strip(), group_code=group_code,
                jenis=row.get("Jenis", "").strip(),
                source=row.get("Sumber", "").strip(),
                bdd=float(bdd), bdd_imputed=imputed, nutrients=nutrients,
            ))

        for spec in SYNTHETIC_FOODS:
            nutrients = {k: 0.0 for k in NUTRIENT_KEYS}
            nutrients.update(spec["nutrients"])
            foods.append(_make_food(
                code=spec["code"], name=spec["name"],
                group_code=spec["group_code"], jenis=spec["jenis"],
                source=spec["source"], bdd=spec["bdd"], bdd_imputed=False,
                nutrients=nutrients,
            ))
        return cls(foods)

    # ------------------------------------------------------------ matching
    def match(self, raw_name: str, min_score: float = 78.0) -> Optional["MatchResult"]:
        """Resolve a recipe ingredient name onto a TKPI food.

        Alias table first (curated, exact intent), then token-set fuzzy match
        with a preference score that favours raw/plain entries.
        """
        name = normalize(raw_name)
        if not name:
            return None
        if name in self._alias_cache:
            return self._alias_cache[name]

        result = self._match_alias(name) or self._match_fuzzy(name, min_score)
        self._alias_cache[name] = result
        return result

    def _match_alias(self, name: str) -> Optional["MatchResult"]:
        hit = lookup_alias(name)
        if hit is None:
            return None
        code, term = hit
        food = self.by_code.get(code)
        if food is None or _identity_conflict(name, term, food.norm_name):
            return None
        proxy = PROXY_ALIASES.get(term)
        return MatchResult(food, 100.0, "proxy" if proxy else "alias",
                           note=proxy[1] if proxy else "")

    def _match_fuzzy(self, name: str, min_score: float) -> Optional["MatchResult"]:
        if name in IGNORE_TERMS or name in DISCARDED_AROMATICS:
            return None
        query_tokens = set(name.split())
        hits = process.extract(
            name, self._choices, scorer=fuzz.token_set_ratio, limit=25,
            score_cutoff=min_score - 12,
        )
        best: Optional[tuple[float, Food]] = None
        for _, score, idx in hits:
            food = self.foods[idx]
            if _identity_conflict(name, name, food.norm_name):
                continue
            adjusted = score + _preference_bonus(food, query_tokens)
            if best is None or adjusted > best[0]:
                best = (adjusted, food)
        if best is None or best[0] < min_score:
            return None
        return MatchResult(best[1], min(best[0], 100.0), "fuzzy")


def _make_food(**kw) -> Food:
    norm = normalize(kw["name"])
    return Food(
        group_label=GROUP_LABEL.get(kw["group_code"], "Lainnya"),
        tokens=frozenset(norm.replace(",", " ").split()),
        norm_name=norm, **kw,
    )


def _preference_bonus(food: Food, query_tokens: set[str]) -> float:
    """Tie-break between entries the fuzzy scorer rates equally.

    Rewards a raw single-ingredient entry; penalises a processed or composite
    entry that the query never asked for, and penalises extra qualifier tokens
    ("gemuk", "var. pelita") that make the entry narrower than the query.
    """
    bonus = 0.0
    if food.jenis.startswith("TUNGGAL"):
        bonus += 4.0
    for marker in _PROCESSED_MARKERS:
        if marker in food.norm_name and marker not in query_tokens:
            bonus -= 6.0
    if any(m in food.norm_name for m in _RAW_MARKERS):
        bonus += 3.0
    # Every food token the query did not ask for makes the entry more specific
    # than the request; a small penalty keeps generic entries winning.
    extra = len(food.tokens - query_tokens - {"segar", "mentah"})
    bonus -= 0.9 * extra
    return bonus


@dataclass(frozen=True)
class MatchResult:
    food: Food
    score: float
    method: str          # "alias" | "proxy" | "fuzzy"
    note: str = ""       # substitution rationale, when method == "proxy"


# ---------------------------------------------------------------- aliases

# Curated recipe-term -> TKPI code. These cover the highest-frequency and
# highest-nutritional-weight ingredients in the corpus, where an automatic match
# would be ambiguous or wrong. Every code below was read off the loaded table,
# not inferred: TKPI's inverted naming makes inference unsafe (a plausible-looking
# guess of "kentang" lands on BR009 "Ganyong"). Longest key wins.
DIRECT_ALIASES: dict[str, str] = {
    # cereals & staples
    "beras": "AR001", "nasi": "AR001", "beras putih": "AR001",
    "beras merah": "AR013", "beras ketan": "AR008", "ketan": "AR008",
    "tepung terigu": "AP025", "terigu": "AP025", "maizena": "AP019",
    "tepung maizena": "AP019", "tepung beras": "AP004",
    "tepung tapioka": "BP070", "tapioka": "BP070", "tepung kanji": "BP070",
    "tepung sagu": "BR018", "sagu": "BR018",
    "mie": "AP022", "mi kering": "AP022", "mie kering": "AP022",
    "mi basah": "AP021", "mie basah": "AP021", "bihun": "AP006",
    # tubers
    "kentang": "BR013", "singkong": "BR016", "ketela": "BR016",
    "ubi kayu": "BR016", "ubi": "BR030", "ubi jalar": "BR030",
    "talas": "BR025",
    # soy / legumes
    "tahu": "CP061", "tahu putih": "CP061", "tahu kuning": "CP061",
    "tempe": "CP077", "tempe kedelai": "CP077",
    "kacang tanah": "CR032", "kacang hijau": "CR014",
    "kacang kedelai": "CR017", "kedelai": "CR017", "oncom": "CP051",
    # vegetables
    "bayam": "DR008", "kangkung": "DR100", "wortel": "DR166",
    "kol": "DR114", "kubis": "DR114", "sawi": "DR141",
    "buncis": "DR013", "labu siam": "DR123", "terong": "DR154",
    "timun": "DR109", "mentimun": "DR109", "ketimun": "DR109",
    "daun singkong": "DR071", "daun pepaya": "DR065",
    "tauge": "DR148", "toge": "DR148", "taoge": "DR148",
    "jagung": "AR015", "jagung manis": "AR015",
    "kembang kol": "DR113", "jamur": "DR090", "jamur tiram": "DR090",
    "jamur kuping": "DR088", "jamur merang": "DR089",
    "nangka muda": "DR130", "pare": "DR131", "paria": "DR131",
    "rebung": "DR138", "kacang panjang": "DR097",
    "daun bawang": "DR018", "seledri": "DR147", "bawang bombay": "DR007",
    "jantung pisang": "DR092", "bengkuang": "BR005",
    # fruit
    "pisang": "ER074", "pepaya": "ER073", "jeruk": "ER039",
    "jeruk manis": "ER039", "jeruk nipis": "ER040", "jeruk limau": "ER040",
    "semangka": "ER105", "melon": "ER067", "mangga": "ER054",
    "apel": "ER004", "salak": "ER101", "nanas": "ER070",
    "jambu biji": "ER031", "alpukat": "ER001", "rambutan": "ER097",
    "sawo": "ER104",
    # animal protein
    "ayam": "FR005", "daging ayam": "FR005", "ayam kampung": "FR005",
    "dada ayam": "FR005", "paha ayam": "FR005", "ceker ayam": "FR005",
    "hati ayam": "FR007",
    "daging sapi": "FR026", "sapi": "FR026", "daging": "FR026",
    "iga sapi": "FR026", "babat": "FR023", "hati sapi": "FR031",
    "daging kambing": "FR019", "kambing": "FR019",
    "bebek": "FR012", "itik": "FR012", "sosis": "FP016",
    "telur": "HR002", "telur ayam": "HR002", "telur ayam ras": "HR002",
    "telur bebek": "HR008", "telur puyuh": "HR011",
    # fish & seafood
    "ikan bandeng": "GR007", "bandeng": "GR007",
    "ikan kembung": "GR050", "kembung": "GR050",
    "ikan tongkol": "GR070", "tongkol": "GR070",
    "ikan cakalang": "GR019", "cakalang": "GR019",
    "ikan mas": "GR046", "ikan patin": "GR053", "patin": "GR053",
    "ikan bawal": "GR012", "bawal": "GR012",
    "ikan selar": "GR058", "ikan layang": "GR039",
    "ikan sarden": "GR057", "sarden": "GR057",
    "ikan teri": "GR068", "teri": "GR068", "ikan asin": "GP004",
    "udang": "GR084", "rebon": "GR080", "cumi": "GR003", "cumi-cumi": "GR003",
    # dairy & fats
    "susu": "JR006", "susu sapi": "JR006", "susu bubuk": "JP006",
    "minyak": "KR012", "minyak goreng": "KR012", "minyak sayur": "KR012",
    "minyak kelapa": "KR011", "minyak wijen": "KR013",
    "margarin": "KP001", "mentega": "KP002",
    "kelapa": "KR002", "kelapa parut": "KR002",
    "santan": "KP003", "santan kara": "KP004", "santan kental": "KP004",
    "santan instan": "KP004", "air kelapa": "QR001",
    # seasoning
    "bawang merah": "NR007", "bawang putih": "NR008",
    "cabe": "NR014", "cabai": "NR014", "cabe merah": "NR014",
    "cabai merah": "NR014", "cabe keriting": "NR014",
    "cabe merah keriting": "NR014", "cabai merah keriting": "NR014",
    "cabe rawit": "NR015", "cabai rawit": "NR015",
    "cabe rawit merah": "NR015", "cabai rawit merah": "NR015",
    "cabe hijau": "NR012", "cabai hijau": "NR012",
    "kemiri": "NR019", "ketumbar": "NR020", "merica": "NR023",
    "lada": "NR023", "lada bubuk": "NR023", "merica bubuk": "NR023",
    "jahe": "NR018", "kunyit": "NR022", "lengkuas": "NR010",
    "laos": "NR010", "cengkeh": "NR016", "pala": "NR024",
    "kluwek": "NR021", "keluak": "NR021",
    "terasi": "NP011", "kecap": "NP005", "kecap manis": "NP005",
    "kecap asin": "NP005", "saos tomat": "NP009", "saus tomat": "NP009",
    "cuka": "NP004", "tomat": "DR161", "tomat merah": "DR161",
    "tomat muda": "DR162", "tomat hijau": "DR162",
    "gula": "MP007", "gula pasir": "MP007", "gula putih": "MP007",
    "gula merah": "MP006", "gula jawa": "MP006", "gula aren": "MP005",
    "gula kelapa": "MP006", "madu": "MP010",
    # more vegetables / legumes seen in the corpus
    "labu kuning": "DR122", "labu": "DR122", "jengkol": "DR093",
    "petai": "DR134", "pete": "DR134", "kacang merah": "CR026",
    "belimbing": "ER006", "belimbing wuluh": "ER006", "roti": "AP024",
    "roti tawar": "AP024",
    # common colloquial spellings and kitchen shorthand
    "telor": "HR002", "baput": "NR008", "bamer": "NR007",
    "bawang": "NR007", "rawit": "NR015", "rawit merah": "NR015",
    "bombay": "DR007", "sawi hijau": "DR141", "sawi putih": "DR141",
    "daun seledri": "DR147", "daun bayam": "DR008", "daun kangkung": "DR100",
    # synthetic
    "garam": "XX001", "air": "XX002", "kaldu bubuk": "XX003",
    "kaldu jamur": "XX003", "penyedap": "XX003", "penyedap rasa": "XX003",
    "royco": "XX003", "masako": "XX003", "kaldu": "XX003",
    "micin": "XX003", "msg": "XX003", "vetsin": "XX003",
    "seasoning": "XX003", "ladaku": "NR023",
}

# Documented substitutions: the ingredient is absent from TKPI 2020, so a
# nutritionally comparable entry stands in. Per INFOODS Food Matching Guidelines
# the substitution is recorded rather than hidden — these resolve with
# method="proxy" so the compliance panel can disclose them.
PROXY_ALIASES: dict[str, tuple[str, str]] = {
    "soun": ("AP006", "not in TKPI; bihun (rice vermicelli) used as proxy"),
    "brokoli": ("DR113", "not in TKPI; kool kembang (cauliflower) used as proxy"),
    "ikan": ("GR050", "generic 'ikan'; kembung used as the reference fish"),
    "lele": ("GR053", "not in TKPI; ikan patin used as proxy (freshwater, similar fat)"),
    "nila": ("GR046", "not in TKPI; ikan mas used as proxy (freshwater)"),
    "mujair": ("GR046", "not in TKPI; ikan mas used as proxy (freshwater)"),
    "gurame": ("GR046", "not in TKPI; ikan mas used as proxy (freshwater)"),
    "tuna": ("GR019", "not in TKPI as raw; cakalang used as proxy (same family)"),
    "asam jawa": ("NR005", "tamarind; 'asam masak pohon' is the TKPI listing"),
    "asem jawa": ("NR005", "tamarind; 'asam masak pohon' is the TKPI listing"),
    "bakso": ("FP016", "not in TKPI; beef sausage used as proxy"),
    "tenggiri": ("GR070", "not in TKPI as raw; ikan tongkol used as proxy"),
    "ebi": ("GR080", "dried shrimp; rebon (small shrimp) used as proxy"),
    "tepung roti": ("AP025", "breadcrumbs; wheat flour used as proxy"),
    "tepung panir": ("AP025", "breadcrumbs; wheat flour used as proxy"),
    "lontong": ("AR001", "compressed rice cake; priced and counted as rice"),
    "ketupat": ("AR001", "compressed rice cake; priced and counted as rice"),
}

ALIASES: dict[str, str] = {
    **DIRECT_ALIASES,
    **{term: code for term, (code, _) in PROXY_ALIASES.items()},
}

# Terms that carry no meaningful nutrition and should not be force-matched.
IGNORE_TERMS = {
    "es batu", "tusuk sate", "lidi", "tali", "daun pisang", "kertas",
    "plastik", "tusuk gigi", "air matang", "air panas", "air hangat",
    "air dingin", "air es",
}

# Aromatics added for flavour and lifted out before serving. They are known
# ingredients — they cost money and appear on the procurement list — but they
# contribute no nutrition to the portion, so they resolve to no TKPI food
# rather than being force-matched to a lookalike.
DISCARDED_AROMATICS = {
    "daun salam", "salam", "daun jeruk", "daun pandan", "pandan",
    "serai", "sereh", "sere", "serei", "sereh batang", "batang serai",
    "lengkuas", "laos", "lengkoas", "kayu manis", "kayumanis", "kapulaga",
    "bunga lawang", "pekak", "cengkeh", "cengkih", "daun kari", "daun kunyit",
    "daun bawang batang", "asam kandis", "daun jeruk purut", "jeruk purut",
    "kencur", "jari kencur", "jinten", "jintan", "jinten bubuk",
    "jintan bubuk", "adas", "daun pisang", "bumbu", "pelengkap",
}

# Words that change what a food *is*. If the recipe line carries one and the
# alias term did not, the alias is rejected: "daun jeruk" (lime leaf) must not
# resolve to "Jeruk manis" (orange), and "kerupuk udang" is not fresh shrimp.
IDENTITY_MODIFIERS = (
    "daun", "kerupuk", "keripik", "kulit", "bunga", "batang", "akar",
    "tulang", "kepala", "ceker", "kaki", "ekor", "sari", "abon", "rempeyek",
)

_ALIAS_KEYS_BY_LENGTH = sorted(ALIASES, key=len, reverse=True)
_ALIAS_WORD_RE = {k: re.compile(r"\b" + re.escape(k) + r"\b") for k in ALIASES}


def _identity_conflict(name: str, alias_term: str, food_name: str) -> bool:
    """True if `name` carries a part/preparation word the match does not honour."""
    for modifier in IDENTITY_MODIFIERS:
        if re.search(r"\b" + modifier + r"\b", name) \
                and modifier not in alias_term and modifier not in food_name:
            return True
    return False


def lookup_alias(name: str) -> Optional[tuple[str, str]]:
    """Longest whole-word alias contained in `name`. Returns (code, alias_term)."""
    if name in IGNORE_TERMS or name in DISCARDED_AROMATICS:
        return None
    if name in ALIASES:
        return ALIASES[name], name
    for key in _ALIAS_KEYS_BY_LENGTH:
        if _ALIAS_WORD_RE[key].search(name):
            return ALIASES[key], key
    return None


def load_default(path: str | Path | None = None) -> FoodDB:
    from .config import RAW_DIR
    path = Path(path) if path else RAW_DIR / "TKPI_2020_Table_4_All_Data.csv"
    return FoodDB.from_csv(path)
