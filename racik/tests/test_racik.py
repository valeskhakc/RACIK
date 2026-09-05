"""Correctness tests for the parts of Racik where a silent error is dangerous.

Run:  python -m pytest tests -q        (or: python tests/test_racik.py)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from racik.akg import AGE_BANDS, blended_band, binding_band, stage_band
from racik.config import MBG, REFERENCE_PORTION_G, SLOTS
from racik.nutrition import (OIL_ABSORPTION_RATE, RecipeEngine,
                             build_component_dishes, infer_cooking_method)
from racik.prices import PriceTable
from racik.tkpi import load_default
from racik.urt import (Confidence, parse_line, parse_quantity, parse_unit,
                       _to_float)


# ---------------------------------------------------------------- fixtures

@pytest.fixture(scope="session")
def foods():
    return load_default()


@pytest.fixture(scope="session")
def prices():
    return PriceTable.load()


@pytest.fixture(scope="session")
def engine(foods, prices):
    return RecipeEngine(foods, prices)


# ---------------------------------------------------------------- numbers

@pytest.mark.parametrize("text,expected", [
    ("1 1/2", 1.5), ("1/2", 0.5), ("3/4", 0.75), ("10-15", 12.5),
    ("1 / 2", 0.5), ("1,5", 1.5), ("5 s/d 7", 6.0), ("2", 2.0),
])
def test_quantity_evaluation(text, expected):
    assert _to_float(text) == pytest.approx(expected)


def test_malformed_quantity_does_not_raise():
    """A stray slash must degrade, not crash the whole corpus build."""
    assert _to_float("2/") == 2.0
    assert _to_float("/") == 1.0


def test_vulgar_fraction():
    qty, rest = parse_quantity("½ butir telur")
    assert qty == 0.5 and rest.startswith("butir")


def test_word_number():
    qty, rest = parse_quantity("dua siung bawang putih")
    assert qty == 2 and rest.startswith("siung")


# ---------------------------------------------------------------- URT units

def test_ons_is_one_hundred_grams():
    """Indonesian culinary 'ons' is a hectogram, not the 28 g English ounce."""
    assert parse_line("2 ons ikan asin").grams == pytest.approx(200.0)


def test_mass_units():
    assert parse_line("1 kg beras").grams == pytest.approx(1000.0)
    assert parse_line("250 gr daging ayam").grams == pytest.approx(250.0)


def test_spoon_conversions():
    """1 sdm = 10 ml and 1 sdt = 1/3 sdm, per the DBMP anchor."""
    sdm = parse_line("1 sdm minyak").grams
    sdt = parse_line("1 sdt minyak").grams
    assert sdm == pytest.approx(9.2)          # 10 ml x 0.92 g/ml
    assert sdm / sdt == pytest.approx(3.0, rel=1e-3)


def test_unit_abbreviations_canonicalise():
    """'bh' must reach the same piece weight as 'buah'."""
    assert parse_line("1 bh tahu").grams == parse_line("1 buah tahu").grams
    assert parse_line("2 btr telur").grams == parse_line("2 butir telur").grams


def test_ingredient_specific_piece_weights():
    """A clove of garlic and an egg are not the same 'one thing'."""
    assert parse_line("1 siung bawang putih").grams == pytest.approx(4.0)
    assert parse_line("1 butir telur ayam").grams == pytest.approx(60.0)
    assert parse_line("1 papan tempe").grams == pytest.approx(200.0)


def test_unquantified_gets_small_nominal_amount():
    """'secukupnya' must never invent a large mass."""
    line = parse_line("secukupnya garam")
    assert line.unquantified is True
    assert line.confidence is Confidence.NOMINAL
    assert line.grams <= 5.0


def test_omitted_ingredient_is_flagged():
    assert parse_line("Soun (saya tidak pakai)").omitted is True


def test_range_takes_midpoint():
    # 12.5 chillies x 1.5 g each
    assert parse_line("10-15 cabe rawit").grams == pytest.approx(18.75)


def test_parenthetical_mass_wins_over_piece_default():
    assert parse_line("2 bungkus (65 g) santan").grams == pytest.approx(130.0)


# ---------------------------------------------------------------- matching

def test_substring_trap_ayam_is_not_bayam(foods):
    """The reason matching is token-based: 'ayam' is a substring of 'Bayam'."""
    assert foods.match("ayam").food.code == "FR005"
    assert foods.match("bayam").food.code == "DR008"


def test_inverted_tkpi_names_resolve(foods):
    """TKPI writes beef as 'Sapi, daging, lemak sedang, segar'."""
    assert foods.match("daging sapi").food.code == "FR026"


def test_identity_modifier_blocks_wrong_match(foods):
    """A lime *leaf* is not an orange; a shrimp *cracker* is not a shrimp."""
    assert foods.match("daun jeruk") is None
    kerupuk = foods.match("kerupuk udang")
    assert kerupuk is None or "kerupuk" in kerupuk.food.norm_name


def test_discarded_aromatics_carry_no_nutrition(foods):
    for term in ("daun salam", "serai", "lengkuas"):
        assert foods.match(term) is None, f"{term} should not be force-matched"


def test_proxy_matches_are_disclosed(foods):
    match = foods.match("lele")
    assert match is not None and match.method == "proxy" and match.note


def test_every_alias_code_exists(foods):
    """Guards against a hand-written alias pointing at a non-existent code."""
    from racik.tkpi import ALIASES
    missing = [c for c in ALIASES.values() if c not in foods.by_code]
    assert not missing, f"aliases point at unknown TKPI codes: {missing}"


# ---------------------------------------------------------------- BDD / cost

def test_bdd_purchase_weight(foods):
    """Cost is charged on gross weight: 75 g of edible chicken at BDD 58%."""
    chicken = foods.by_code["FR005"]
    assert chicken.bdd == pytest.approx(58.0)
    assert chicken.purchase_grams(75.0) == pytest.approx(75.0 / 0.58, rel=1e-6)


def test_purchase_weight_never_below_edible(foods):
    for food in foods.foods:
        assert food.purchase_grams(100.0) >= 100.0 - 1e-9


def test_vitamin_a_uses_retinol_equivalents(foods):
    """RE = retinol + beta-carotene / 6."""
    carrot = foods.by_code["DR166"]
    assert carrot.nutrients["vit_a_mcg"] > 0


def test_price_falls_back_and_reports_basis(prices):
    assert prices.lookup(code="AR001").basis == "commodity"
    assert prices.lookup(code="ZZ999", group_code="F").basis == "group"
    assert prices.lookup(code="ZZ999", name="nothing at all").basis == "default"


# ---------------------------------------------------------------- AKG

def test_akg_meal_is_one_third_of_daily():
    band = AGE_BANDS["7-9"]
    meal = band.per_meal(MBG.akg_fraction)
    assert meal["energy_kcal"] == pytest.approx(1650 / 3)


def test_sd_meal_target_matches_published_mbg_range():
    """MBG for ages 7-12 is documented at roughly 500-700 kkal per meal."""
    meal = stage_band("sd").per_meal(MBG.akg_fraction)
    assert 500 <= meal["energy_kcal"] <= 700
    assert 10 <= meal["protein_g"] <= 25


def test_blend_reports_the_band_it_under_serves():
    keys = ["7-9", "m10-12", "f10-12"]
    blend = blended_band(keys)
    hungriest = binding_band(keys)
    assert hungriest.protein_g >= blend.protein_g


def test_ingredient_budget_is_the_full_operator_input():
    # The operator's budget input is Racik's own food/ingredient budget, not
    # an all-inclusive program pagu — nothing held back by default. See
    # MBGConfig.ingredient_budget_share's docstring.
    assert MBG.ingredient_budget_idr == pytest.approx(10_000.0)


# ---------------------------------------------------------------- engine

def test_cooking_method_inference():
    assert infer_cooking_method("Goreng tahu hingga kecoklatan.") == "fat"
    assert infer_cooking_method("Rebus hingga matang, lalu kukus.") == "moist"
    assert infer_cooking_method("Bakar di atas arang.") == "dry"
    assert infer_cooking_method("Campur semua bahan.") == "raw"


def test_frying_oil_is_absorbed_not_eaten_whole(engine):
    """500 ml of deep-frying oil must not land on the plate as 460 g of fat."""
    dish = engine.build(
        1, "Tahu goreng",
        "500 ml minyak goreng | 500 gr tahu",
        "Goreng tahu dalam minyak panas.")
    oil = [ln for ln in dish.lines if ln.matched
           and ln.match.food.group_code == "K"][0]
    assert oil.cost_idr > 0                      # still fully purchased
    assert oil.edible_g <= 500 * OIL_ABSORPTION_RATE + 1


def test_saute_oil_is_fully_counted(engine):
    """A normal one-spoon saute is eaten in full; the absorption cap must not fire.

    1 sdm = 10 ml x 0.92 = 9.2 g of oil against 180 g of edible kangkung
    (300 g gross at BDD 60%), whose absorbable ceiling is 18 g — well clear.
    """
    dish = engine.build(2, "Tumis kangkung",
                        "1 sdm minyak goreng | 300 gr kangkung",
                        "Tumis kangkung dengan minyak.")
    oil = [ln for ln in dish.lines if ln.matched
           and ln.match.food.group_code == "K"][0]
    assert oil.edible_g == pytest.approx(9.2, rel=1e-3)


def test_oil_absorption_cap_is_proportional_not_a_cliff(engine):
    """Just over the ceiling, oil is trimmed to the absorbable mass, not zeroed."""
    dish = engine.build(7, "Tumis kangkung banyak minyak",
                        "2 sdm minyak goreng | 300 gr kangkung",
                        "Tumis kangkung dengan minyak.")
    oil = [ln for ln in dish.lines if ln.matched
           and ln.match.food.group_code == "K"][0]
    # 180 g edible kangkung x 10% = 18 g absorbable, against 18.4 g declared.
    assert oil.edible_g == pytest.approx(18.0, rel=1e-3)


def test_dish_slot_classification(engine):
    fish = engine.build(3, "Ikan goreng", "1 ekor ikan bandeng | 2 sdm minyak",
                        "Goreng ikan.")
    veg = engine.build(4, "Tumis kangkung", "2 ikat kangkung | 3 siung bawang putih",
                       "Tumis sebentar.")
    soy = engine.build(5, "Tempe goreng", "1 papan tempe | 2 sdm minyak",
                       "Goreng tempe.")
    assert fish.slot == "hewani"
    assert veg.slot == "sayur"
    assert soy.slot == "nabati"


def test_per_portion_is_total_over_servings(engine):
    dish = engine.build(6, "Semur", "1 kg daging sapi | 500 gr kentang",
                        "Rebus hingga empuk.")
    total_protein = sum(ln.nutrients.get("protein_g", 0.0) for ln in dish.lines)
    assert dish.protein_g == pytest.approx(total_protein / dish.servings)
    assert dish.servings >= 1


def test_component_dishes_cover_every_required_slot(foods, prices):
    components = build_component_dishes(foods, prices)
    slots = {d.slot for d in components}
    assert {"staple", "buah"} <= slots
    for dish in components:
        assert dish.cost_per_portion_idr > 0
        assert dish.energy_kcal >= 0


def test_rice_portion_uses_matang_mentah_factor(foods, prices):
    """150 g of cooked rice is 60 g of raw beras (factor 0.4)."""
    rice = [d for d in build_component_dishes(foods, prices)
            if d.name.startswith("Nasi putih (150")][0]
    beras = foods.by_code["AR001"]
    assert rice.energy_kcal == pytest.approx(beras.nutrients["energy_kcal"] * 0.60,
                                             rel=1e-6)


# ---------------------------------------------------------------- optimiser

@pytest.fixture(scope="session")
def optimizer():
    from racik.optimizer import MenuOptimizer
    from racik.store import RacikStore
    store = RacikStore()
    return MenuOptimizer(store.load_dishes())


def test_plan_fills_every_tray_slot(optimizer):
    from racik.optimizer import PlanRequest
    result = optimizer.solve(PlanRequest(days=3, stage="sd",
                                         max_solve_seconds=10))
    assert result.feasible
    for day in result.days:
        for slot in SLOTS:
            assert day.dishes[slot] is not None, f"{slot} empty on day {day.day_index}"


def test_plan_respects_exclusions(optimizer):
    from racik.optimizer import PlanRequest
    result = optimizer.solve(PlanRequest(days=3, stage="sd",
                                         exclude_terms=["telur", "udang"],
                                         max_solve_seconds=10))
    assert result.feasible
    for day in result.days:
        for dish in day.selected:
            haystack = (dish.name + " " + dish.ingredient_index).lower()
            assert "telur" not in haystack and "udang" not in haystack


def test_plan_reports_relaxations_instead_of_failing(optimizer):
    """An impossible budget must still return a menu, with the shortfall named."""
    from racik.config import MBGConfig
    from racik.optimizer import PlanRequest
    starved = MBGConfig(budget_per_portion_idr=3000, ingredient_budget_share=0.7)
    result = optimizer.solve(PlanRequest(days=2, stage="sma", config=starved,
                                         max_solve_seconds=10))
    assert result.feasible, "soft floors must keep the model solvable"
    assert result.relaxations, "a starved budget must be reported, not hidden"


def test_variety_no_dish_repeats_within_plan(optimizer):
    from racik.optimizer import PlanRequest, dish_key
    result = optimizer.solve(PlanRequest(days=5, stage="sd",
                                         max_solve_seconds=12))
    for slot in ("hewani", "nabati", "sayur"):
        keys = [dish_key(day.dishes[slot]) for day in result.days
                if day.dishes[slot]]
        assert len(keys) == len(set(keys)), f"{slot} repeated a dish"


def test_variety_caps_one_ingredient_family(optimizer):
    """A week must not be five variations on tempe."""
    from racik.optimizer import PlanRequest
    request = PlanRequest(days=5, stage="sd", max_solve_seconds=12)
    result = optimizer.solve(request)
    for slot in ("hewani", "nabati", "sayur"):
        families: dict[str, int] = {}
        for day in result.days:
            dish = day.dishes[slot]
            if dish and dish.main_food_code:
                families[dish.main_food_code] = families.get(
                    dish.main_food_code, 0) + 1
        assert all(n <= request.max_family_per_plan for n in families.values()), \
            f"{slot}: {families}"


def test_day_nutrients_are_the_sum_of_selected_dishes(optimizer):
    from racik.optimizer import PlanRequest
    result = optimizer.solve(PlanRequest(days=2, stage="sd",
                                         max_solve_seconds=10))
    for day in result.days:
        expected = sum(d.per_portion.get("protein_g", 0.0) for d in day.selected)
        assert day.nutrients["protein_g"] == pytest.approx(expected)
        expected_cost = sum(d.cost_per_portion_idr for d in day.selected)
        assert day.cost_per_portion_idr == pytest.approx(expected_cost)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
