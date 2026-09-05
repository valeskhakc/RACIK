"""Precompute a bank of verified single-day trays for the Render demo.

For each of 5 well-covered regions x 4 AKG stages, solves ~20 diverse
single-day plans (not a full week — a single day is a far smaller, faster
CP-SAT problem) and keeps only the ones that are already fully compliant
(zero relaxed floors, zero budget overrun). The live app can then assemble
a week for any of these regions by picking non-repeating trays from the
bank instantly, with no CP-SAT solve on the request path at all.

This does NOT replace live generation for other regions or edge cases —
those still hit the normal solver. It's a fast, honestly-labeled path for
a curated set of demo-friendly regions.

Writes racik/data/precomputed_trays.json.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from racik.akg import SCHOOL_STAGES
from racik.config import MBGConfig, SLOTS
from racik.optimizer import MenuOptimizer, PlanRequest, dish_key
from racik.prices import PriceTable
from racik.store import RacikStore

REGIONS = [
    ("Jawa Tengah", "jawa"), ("Sumatera Barat", "sumatera"),
    ("Jawa Barat", "jawa"), ("Jawa Timur", "jawa"),
    ("Sumatera Selatan", "sumatera"),
]
STAGES = list(SCHOOL_STAGES)
TARGET_TRAYS = 20
MAX_ATTEMPTS = 100  # generous ceiling — family-level exclusion means more
                    # attempts miss (excluded pool) before finding a new one
# Matches the onboarding form's default per-portion budget (14,000) — but
# NOT a flat regional_cost_index=1.0. Agen Biaya resolves each province's
# *real* WFP-survey index (0.7-1.6 range), so a flat baseline built every
# tray right up against a 14,000 ceiling, then broke for every region whose
# real index is below 1.0 (4 of these 5) since their live effective budget
# comes out lower than what the bank was validated against. Building each
# region's bank against its own real index makes the two exactly agree.
BUDGET_PER_PORTION_IDR = 14_000.0
_PRICE_TABLE = PriceTable.load()


def demo_config(province: str) -> MBGConfig:
    return MBGConfig(budget_per_portion_idr=BUDGET_PER_PORTION_IDR,
                     regional_cost_index=_PRICE_TABLE.regional_index_for(province))


def tray_signature(day) -> tuple:
    dishes = day.dishes
    return tuple(dishes[s].dish_id if dishes[s] else None for s in SLOTS)


def family_of(dish) -> str:
    return dish.main_food_code or dish_key(dish)


# Matches the live solver's own default (PlanRequest.max_family_per_plan) —
# the bank needs at least this many distinct families per slot to ever let
# a request-time pick satisfy the same cap the real solver enforces.
MAX_PER_FAMILY = 2
VARIETY_SLOTS = ("hewani", "nabati", "sayur")


def collect_trays(opt: MenuOptimizer, province: str, island: str, stage: str) -> list[dict]:
    # Every dish_id belonging to each (slot, family) — built once so a
    # maxed-out family can be excluded *entirely*, not just its one dish
    # that happened to get picked. Excluding individual dish_ids isn't
    # enough: the corpus has dozens of distinct tempe recipes alone, so the
    # solver just picks a different one from the same family every time.
    family_dishes: dict[tuple[str, str], set[int]] = {}
    for slot in VARIETY_SLOTS:
        for dish in opt.by_slot[slot]:
            fam = dish.main_food_code or dish_key(dish)
            family_dishes.setdefault((slot, fam), set()).add(dish.dish_id)

    seen_signatures: set[tuple] = set()
    exclude_ids: set[int] = set()
    family_count: dict[tuple[str, str], int] = {}
    trays: list[dict] = []

    for attempt in range(MAX_ATTEMPTS):
        if len(trays) >= TARGET_TRAYS:
            break
        req = PlanRequest(
            days=1, stage=stage, portions=80, province=province, island=island,
            config=demo_config(province), exclude_dish_ids=list(exclude_ids),
            max_solve_seconds=5.0, candidates_per_slot=150,
            random_seed=attempt,
        )
        r = opt.solve(req)
        if not r.feasible or r.relaxations or r.budget_overrun_idr > 0:
            continue
        day = r.days[0]
        sig = tray_signature(day)
        if sig in seen_signatures or None in sig:
            continue
        seen_signatures.add(sig)
        trays.append({
            "dish_ids": {s: day.dishes[s].dish_id for s in SLOTS},
            "cost_per_portion_idr": round(day.cost_per_portion_idr, 1),
            "nutrients": {k: round(v, 3) for k, v in day.nutrients.items()},
        })
        for slot in VARIETY_SLOTS:
            dish = day.dishes[slot]
            fam = dish.main_food_code or dish_key(dish)
            family_count[(slot, fam)] = family_count.get((slot, fam), 0) + 1
            if family_count[(slot, fam)] >= MAX_PER_FAMILY:
                exclude_ids.update(family_dishes[(slot, fam)])
            else:
                exclude_ids.add(dish.dish_id)

    return trays


def main() -> None:
    store = RacikStore()
    dishes = store.load_dishes()
    opt = MenuOptimizer(dishes)

    bank: dict[str, dict[str, list[dict]]] = {}
    for province, island in REGIONS:
        bank[province] = {}
        for stage in STAGES:
            t0 = time.time()
            trays = collect_trays(opt, province, island, stage)
            dt = time.time() - t0
            print(f"{province}/{stage}: {len(trays)} trays in {dt:.1f}s")
            bank[province][stage] = trays

    out_path = Path(__file__).resolve().parent.parent / "data" / "precomputed_trays.json"
    out_path.write_text(json.dumps({
        "budget_per_portion_idr": BUDGET_PER_PORTION_IDR,
        "regions": {p: isl for p, isl in REGIONS},
        "trays": bank,
    }, indent=2))
    print(f"\nwrote {out_path}")

    # Real validation: exercise the exact selection code path _try_cache()
    # uses (family-capped per slot), not just a raw tray count — a pool of
    # 20 trays dominated by 2 families still fails a 7-day pick.
    #
    # Force a fresh reload of the module-level cache: collect_trays() above
    # calls opt.solve(), which calls _try_cache() internally, which lazily
    # loaded (and cached as absent) the bank *before* it existed on disk in
    # this same process. Without resetting it here, validation would check
    # against that stale "no bank" state forever.
    import racik.optimizer as _optmod
    _optmod._TRAY_CACHE_LOADED = False
    _optmod._TRAY_CACHE = None

    print("\nValidating actual 7-day selectability via MenuOptimizer._try_cache()...")
    from racik.akg import stage_band
    all_ok = True
    for province, island in REGIONS:
        for stage in STAGES:
            req = PlanRequest(days=7, stage=stage, portions=80, province=province,
                              island=island, config=demo_config(province),
                              max_solve_seconds=1.0)
            band = stage_band(stage)
            targets = band.per_meal(req.config.akg_fraction)
            result = opt._try_cache(req, band, targets)
            ok = result is not None and len(result.days) == 7
            print(f"  {'OK  ' if ok else 'FAIL'}  {province}/{stage}")
            if not ok:
                all_ok = False
    print("ALL REGIONS SELECTABLE FOR A 7-DAY PLAN" if all_ok
          else "SOME REGIONS CANNOT FILL A 7-DAY PLAN — see FAIL above")


if __name__ == "__main__":
    main()
