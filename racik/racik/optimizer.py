"""Menu optimisation — the diet problem, in MBG's shape.

Decision variables are binary: x[day, dish] = 1 when a dish fills its tray slot
on that day. Constraints encode AKG 2019 floors, the Rp10,000 ingredient
sub-budget, the Isi Piringku tray composition, and no-repeat variety.

Why CP-SAT rather than an LP: the problem is pure-integer selection over
thousands of candidate dishes, which is exactly CP-SAT's structure, and it
gives assumption-based infeasibility reporting for free.

**Nutrient floors are soft.** Every floor carries a penalised slack variable, so
the solver always returns a menu and reports which floors it had to relax and by
how much, rather than failing with "infeasible". That is the difference between
a planner an SPPG cook can act on and one that stonewalls them. Budget is soft
for the same reason, at a higher penalty — overspending is worse than being
slightly short on fibre, and the report says so either way.
"""
from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

from ortools.sat.python import cp_model

from .akg import AgeBand, BINDING_NUTRIENTS, NUTRIENT_META
from .config import (COMPONENT_ONLY_SLOTS, MBG, OPTIONAL_SLOTS, SLOTS,
                     TRAY_CACHE_PATH, MBGConfig)
from .nutrition import COMPONENT_ID_BASE, Dish
from .urt import normalize

# CP-SAT is integral: floats are scaled to integers by these factors.
NUTRIENT_SCALE = 10       # 0.1 g / 0.1 kcal resolution
COST_SCALE = 1            # rupiah are already integers

# Objective weights. Relative magnitudes encode policy: missing a nutrient floor
# is penalised far above rupiah, and going over budget above that again.
W_NUTRIENT_SLACK = 400
W_BUDGET_SLACK = 900
W_COST = 1
W_UNDERSPEND = 8
W_PREFERENCE = 250
W_QUALITY = 120

# Override via RACIK_CANDIDATES_PER_SLOT on CPU-constrained hosts (e.g.
# Render's free 0.1-vCPU tier), where the default search space doesn't
# finish inside a request's time budget.
DEFAULT_CANDIDATES_PER_SLOT = int(os.environ.get("RACIK_CANDIDATES_PER_SLOT", "220"))

_TRAY_CACHE: Optional[dict] = None
_TRAY_CACHE_LOADED = False


def _tray_cache() -> Optional[dict]:
    """Lazily load the precomputed-tray bank; None if it doesn't exist.

    Optional by design: most requests (most regions, any custom exclusion
    or tighter budget) never touch this and go through the real solver.
    """
    global _TRAY_CACHE, _TRAY_CACHE_LOADED
    if not _TRAY_CACHE_LOADED:
        _TRAY_CACHE_LOADED = True
        if TRAY_CACHE_PATH.exists():
            _TRAY_CACHE = json.loads(TRAY_CACHE_PATH.read_text())
    return _TRAY_CACHE

# ---------------------------------------------------------------- the rubric
#
# Candidate pre-filtering is two stages, and both are stated here rather than
# buried in the loop, because "why was this dish never considered?" is the
# question an SPPG nutritionist will actually ask.
#
# Stage 1 — hard gates. A dish failing any of these is ineligible, full stop.
#   G1  slot            the dish must fill the tray slot being filled
#   G2  source          staple and buah come only from served components
#   G3  exclusions      allergen / preference terms, matched against the dish
#                       name and its full ingredient index
#   G4  dish id         explicit operator veto
#   G5  cost ceiling    one component may not consume the whole tray budget
#
# Stage 2 — ranking. Survivors are scored, de-duplicated by name, and the top
# N per slot go to the solver. The score is deliberately simple and readable:
# nutrition per rupiah, nudged by regional fit and by how much of the dish's
# data is actually grounded.
#
#   value       = min(protein / target, CAP) + min(energy / target, CAP)
#   efficiency  = value / (cost / budget + COST_FLOOR)
#   score       = efficiency
#               + W_PREFERENCE x regional_fit
#               + W_QUALITY    x data_quality
#
COST_CEILING_SHARE = 0.85    # G5: max share of the ingredient budget, one slot
VALUE_CAP = 1.5              # credit for overshooting a target stops here, so a
                             # single enormous dish cannot win on energy alone
RANK_COST_FLOOR = 0.15       # stops near-free items dividing by ~zero
W_RANK_PREFERENCE = 3.0      # regional fit is worth up to 3 points
W_RANK_QUALITY = 2.0         # data quality is worth up to 2 points

# Regional fit, 0-1. Exact province beats island group beats national staple.
PREFERENCE_EXACT_PROVINCE = 1.0
PREFERENCE_SAME_ISLAND = 0.6
PREFERENCE_NATIONAL = 0.5
PREFERENCE_OTHER = 0.2


@dataclass
class PlanRequest:
    """What the kitchen is asking for."""

    days: int = 5
    stage: str = "sd"                       # AKG school stage
    portions: int = 100                     # headcount per day
    province: str = ""                      # regional preference
    island: str = ""
    config: MBGConfig = field(default_factory=lambda: MBG)
    exclude_terms: Sequence[str] = ()       # allergy / preference exclusions
    exclude_dish_ids: Sequence[int] = ()
    no_repeat_slots: Sequence[str] = ("hewani", "nabati", "sayur", "buah")
    repeat_window: int = 7                  # days a dish must not recur within
    max_family_per_plan: int = 2            # times one main ingredient may fill a slot
    candidates_per_slot: int = DEFAULT_CANDIDATES_PER_SLOT
    enforce_micronutrients: bool = False    # AKG micro values pending verification
    max_solve_seconds: float = 20.0
    random_seed: int = 0


@dataclass
class DayPlan:
    day_index: int
    dishes: dict[str, Optional[Dish]]
    nutrients: dict[str, float]
    cost_per_portion_idr: float

    @property
    def selected(self) -> list[Dish]:
        return [d for d in self.dishes.values() if d is not None]


@dataclass
class CandidateFunnel:
    """How a slot's candidate pool was narrowed, gate by gate."""

    slot: str
    in_slot: int                    # dishes classified into this slot
    cost_ceiling_idr: float
    cut_dish_id: int = 0            # G4
    cut_not_component: int = 0      # G2
    cut_exclusion: int = 0          # G3
    cut_cost_ceiling: int = 0       # G5
    eligible: int = 0               # survived every hard gate
    cut_duplicate_name: int = 0     # removed by name de-duplication
    selected: int = 0               # handed to the solver
    top: list[dict] = field(default_factory=list)

    @property
    def binding_gate(self) -> Optional[str]:
        """The gate that removed the most dishes — the one worth explaining."""
        cuts = {"dish_id veto": self.cut_dish_id,
                "source (component-only slot)": self.cut_not_component,
                "exclusions": self.cut_exclusion,
                "cost ceiling": self.cut_cost_ceiling}
        gate, removed = max(cuts.items(), key=lambda kv: kv[1])
        return gate if removed else None

    def to_dict(self) -> dict:
        return {
            "slot": self.slot, "in_slot": self.in_slot,
            "cost_ceiling_idr": round(self.cost_ceiling_idr),
            "removed": {
                "G2_source": self.cut_not_component,
                "G3_exclusions": self.cut_exclusion,
                "G4_dish_id": self.cut_dish_id,
                "G5_cost_ceiling": self.cut_cost_ceiling,
                "duplicate_name": self.cut_duplicate_name,
            },
            "eligible": self.eligible, "selected": self.selected,
            "binding_gate": self.binding_gate,
            "top": self.top,
        }


@dataclass
class Relaxation:
    day_index: int
    nutrient: str
    shortfall: float
    target: float

    @property
    def share(self) -> float:
        return self.shortfall / self.target if self.target else 0.0


@dataclass
class PlanResult:
    request: PlanRequest
    band: AgeBand
    targets: dict[str, float]
    days: list[DayPlan]
    relaxations: list[Relaxation]
    budget_overrun_idr: float
    status: str
    solve_seconds: float
    candidates_considered: int

    @property
    def feasible(self) -> bool:
        return bool(self.days)

    @property
    def mean_cost_per_portion(self) -> float:
        if not self.days:
            return 0.0
        return sum(d.cost_per_portion_idr for d in self.days) / len(self.days)

    def adequacy(self, day: DayPlan) -> float:
        """Share of binding nutrient targets met, 0-1, capped per nutrient."""
        if not self.targets:
            return 0.0
        scores = [
            min(1.0, day.nutrients.get(n, 0.0) / self.targets[n])
            for n in BINDING_NUTRIENTS if self.targets.get(n)
        ]
        return sum(scores) / len(scores) if scores else 0.0

    def summary(self) -> dict:
        return {
            "status": self.status,
            "days": len(self.days),
            "solve_seconds": round(self.solve_seconds, 2),
            "candidates": self.candidates_considered,
            "mean_cost_per_portion_idr": round(self.mean_cost_per_portion, 1),
            "ingredient_budget_idr": round(
                self.request.config.ingredient_budget_idr, 1),
            "budget_overrun_idr": round(self.budget_overrun_idr, 1),
            "relaxed_floors": len(self.relaxations),
            "mean_adequacy": round(
                sum(self.adequacy(d) for d in self.days) / len(self.days), 3
            ) if self.days else 0.0,
        }


class MenuOptimizer:
    """Selects dishes into a multi-day MBG menu under AKG and budget."""

    def __init__(self, dishes: Iterable[Dish]):
        self.dishes = list(dishes)
        self.by_slot: dict[str, list[Dish]] = {s: [] for s in SLOTS}
        self.by_id: dict[int, Dish] = {}
        for dish in self.dishes:
            if dish.slot in self.by_slot:
                self.by_slot[dish.slot].append(dish)
            self.by_id[dish.dish_id] = dish

    # ------------------------------------------------------------ candidates
    def _candidates(self, request: PlanRequest,
                    targets: dict[str, float]) -> dict[str, list[Dish]]:
        """Pre-filter to a workable candidate set per slot.

        Full-corpus selection is unnecessary and slows the solve; ranking by
        cost-efficiency, data quality and regional fit keeps the good dishes.
        """
        return {slot: pool for slot, (pool, _) in
                self._candidates_with_funnel(request, targets).items()}

    def _candidates_with_funnel(
            self, request: PlanRequest, targets: dict[str, float]
    ) -> dict[str, tuple[list[Dish], "CandidateFunnel"]]:
        """Pre-filter, recording how many dishes each gate removed.

        The funnel is what turns "no dish was available" into an answerable
        question: it names the gate that emptied the pool.
        """
        excluded = {t.lower() for t in request.exclude_terms if t.strip()}
        excluded_ids = set(request.exclude_dish_ids)
        budget = request.config.ingredient_budget_idr
        ceiling = budget * COST_CEILING_SHARE

        out: dict[str, tuple[list[Dish], CandidateFunnel]] = {}
        for slot in SLOTS:
            in_slot = self.by_slot[slot]
            funnel = CandidateFunnel(slot=slot, in_slot=len(in_slot),
                                     cost_ceiling_idr=ceiling)
            pool = []
            for dish in in_slot:
                if dish.dish_id in excluded_ids:
                    funnel.cut_dish_id += 1
                    continue
                # Rice and fruit come from the standard served components.
                if slot in COMPONENT_ONLY_SLOTS and dish.dish_id < COMPONENT_ID_BASE:
                    funnel.cut_not_component += 1
                    continue
                if excluded and _excluded(dish, excluded):
                    funnel.cut_exclusion += 1
                    continue
                # A single component may not eat the entire tray budget.
                if dish.cost_per_portion_idr > ceiling:
                    funnel.cut_cost_ceiling += 1
                    continue
                pool.append(dish)

            funnel.eligible = len(pool)
            pool.sort(key=lambda d: -_rank(d, request, targets, budget))
            deduped = _dedupe_by_name(pool)
            funnel.cut_duplicate_name = len(pool) - len(deduped)
            selected = deduped[:request.candidates_per_slot]
            funnel.selected = len(selected)
            funnel.top = [
                {"dish_id": d.dish_id, "name": d.name, "province": d.province,
                 "cost_per_portion_idr": round(d.cost_per_portion_idr),
                 "score": rank_breakdown(d, request, targets, budget).to_dict()}
                for d in selected[:5]
            ]
            out[slot] = (selected, funnel)
        return out

    def explain_candidates(self, request: PlanRequest) -> dict:
        """The pre-filter rubric and its effect, without running a solve."""
        from .akg import stage_band

        targets = stage_band(request.stage).per_meal(request.config.akg_fraction)
        funnels = self._candidates_with_funnel(request, targets)
        return {
            "rubric": {
                "hard_gates": [
                    {"id": "G1", "name": "slot",
                     "rule": "dish must fill the tray slot being filled"},
                    {"id": "G2", "name": "source",
                     "rule": f"slots {list(COMPONENT_ONLY_SLOTS)} accept served "
                             "components only, never corpus recipes"},
                    {"id": "G3", "name": "exclusions",
                     "rule": "term matched against dish name + full ingredient index"},
                    {"id": "G4", "name": "dish_id veto",
                     "rule": "explicitly excluded by the operator"},
                    {"id": "G5", "name": "cost ceiling",
                     "rule": f"cost per portion <= {COST_CEILING_SHARE:.0%} of the "
                             f"ingredient budget "
                             f"(Rp{request.config.ingredient_budget_idr * COST_CEILING_SHARE:,.0f})"},
                ],
                "score": {
                    "formula": ("efficiency + W_PREFERENCE x regional_fit "
                                "+ W_QUALITY x data_quality"),
                    "efficiency": ("(min(protein/target, CAP) + min(energy/target, CAP)) "
                                   "/ (cost/budget + COST_FLOOR)"),
                    "weights": {"W_PREFERENCE": W_RANK_PREFERENCE,
                                "W_QUALITY": W_RANK_QUALITY,
                                "VALUE_CAP": VALUE_CAP,
                                "COST_FLOOR": RANK_COST_FLOOR},
                    "regional_fit": {
                        "exact province": PREFERENCE_EXACT_PROVINCE,
                        "same island": PREFERENCE_SAME_ISLAND,
                        "national": PREFERENCE_NATIONAL,
                        "other": PREFERENCE_OTHER},
                },
                "then": {"dedupe": "one dish per normalised name",
                         "cap": request.candidates_per_slot},
                "targets_per_meal": {k: round(targets[k], 1)
                                     for k in ("energy_kcal", "protein_g")},
            },
            "funnel": [f.to_dict() for _, f in funnels.values()],
        }

    # ------------------------------------------------------------ tray cache
    def _try_cache(self, request: PlanRequest, band: AgeBand,
                   targets: dict[str, float]) -> Optional[PlanResult]:
        """Assemble a plan from scripts/precompute_trays.py's bank, if eligible.

        Only for the handful of (region, stage) combos the bank actually
        covers, and only when this specific request's constraints are no
        stricter than what the trays were validated against — every other
        request (most regions, tighter budgets, enforced micronutrients,
        custom exclusions the bank wasn't checked against) falls through to
        the real solver unchanged. Returns None to signal "solve normally".
        """
        bank = _tray_cache()
        if bank is None or request.enforce_micronutrients:
            return None
        pool = bank.get("trays", {}).get(request.province, {}).get(request.stage)
        if not pool:
            return None

        budget = request.config.ingredient_budget_idr
        exclude_ids = set(request.exclude_dish_ids)
        exclude_terms = {t.lower() for t in request.exclude_terms if t.strip()}

        # A tiny tolerance for rounding noise: the bank's cost is the exact
        # sum of each dish's real per-portion cost, while the precompute
        # solve's own overrun check used integer-rupiah-rounded costs
        # internally — the two can disagree by a few rupiah on an otherwise
        # genuinely-in-budget tray.
        BUDGET_TOLERANCE_IDR = 5.0
        eligible: list[dict[str, Dish]] = []
        for tray in pool:
            if tray["cost_per_portion_idr"] > budget + BUDGET_TOLERANCE_IDR:
                continue
            dish_objs = {slot: self.by_id.get(dish_id)
                        for slot, dish_id in tray["dish_ids"].items()}
            if any(d is None for d in dish_objs.values()):
                continue  # corpus changed since the bank was built
            if any(d.dish_id in exclude_ids for d in dish_objs.values()):
                continue
            if exclude_terms and any(_excluded(d, exclude_terms)
                                     for d in dish_objs.values()):
                continue
            eligible.append(dish_objs)
        if len(eligible) < request.days:
            return None

        # Greedy family-diverse pick — the same max_family_per_plan rule the
        # live solver enforces (per slot, not shared across slots — tempe
        # dominating nabati says nothing about hewani), applied here without
        # needing CP-SAT for it.
        family_count: dict[tuple[str, str], int] = {}
        chosen: list[dict[str, Dish]] = []
        for dish_objs in eligible:
            if len(chosen) >= request.days:
                break
            over_cap = False
            for slot in request.no_repeat_slots:
                d = dish_objs.get(slot)
                if d is None:
                    continue
                fam = d.main_food_code or dish_key(d)
                if family_count.get((slot, fam), 0) >= request.max_family_per_plan:
                    over_cap = True
                    break
            if over_cap:
                continue
            chosen.append(dish_objs)
            for slot in request.no_repeat_slots:
                d = dish_objs.get(slot)
                if d is not None:
                    fam = d.main_food_code or dish_key(d)
                    family_count[(slot, fam)] = family_count.get((slot, fam), 0) + 1
        if len(chosen) < request.days:
            return None

        from .tkpi import NUTRIENT_KEYS
        day_plans = []
        for idx, dish_objs in enumerate(chosen):
            nutrients = {k: 0.0 for k in NUTRIENT_KEYS}
            cost = 0.0
            for d in dish_objs.values():
                cost += d.cost_per_portion_idr
                for k in NUTRIENT_KEYS:
                    nutrients[k] += d.per_portion.get(k, 0.0)
            day_plans.append(DayPlan(idx, dict(dish_objs), nutrients, cost))

        return PlanResult(request, band, targets, day_plans, [], 0.0,
                          "CACHED", 0.0, len(pool))

    # ------------------------------------------------------------ solve
    def solve(self, request: PlanRequest) -> PlanResult:
        from .akg import stage_band

        band = stage_band(request.stage)
        targets = band.per_meal(request.config.akg_fraction)
        binding = list(BINDING_NUTRIENTS)
        if request.enforce_micronutrients:
            binding += ["calcium_mg", "iron_mg", "zinc_mg", "vit_a_mcg", "vit_c_mg"]

        cached = self._try_cache(request, band, targets)
        if cached is not None:
            return cached

        candidates = self._candidates(request, targets)
        total_candidates = sum(len(v) for v in candidates.values())
        if any(not candidates[s] for s in SLOTS if s not in OPTIONAL_SLOTS):
            return PlanResult(request, band, targets, [], [], 0.0,
                              "NO_CANDIDATES", 0.0, total_candidates)

        model = cp_model.CpModel()
        days = range(request.days)
        budget = int(round(request.config.ingredient_budget_idr * COST_SCALE))

        # x[day][slot][i]
        x: dict[tuple[int, str, int], cp_model.IntVar] = {}
        for d in days:
            for slot in SLOTS:
                for i in range(len(candidates[slot])):
                    x[d, slot, i] = model.NewBoolVar(f"x_{d}_{slot}_{i}")

        # --- tray composition: one dish per slot per day
        for d in days:
            for slot in SLOTS:
                picks = [x[d, slot, i] for i in range(len(candidates[slot]))]
                if not picks:
                    continue
                if slot in OPTIONAL_SLOTS:
                    model.Add(sum(picks) <= 1)
                else:
                    model.AddExactlyOne(picks)

        # --- variety: no dish recurs inside the window
        for slot in request.no_repeat_slots:
            for i in range(len(candidates.get(slot, []))):
                for start in days:
                    window = [x[d, slot, i]
                              for d in range(start,
                                             min(start + request.repeat_window,
                                                 request.days))]
                    if len(window) > 1:
                        model.Add(sum(window) <= 1)

        # --- variety: cap how often one ingredient family fills a slot.
        # Without this a week reads "tempe mendoan, tempe kriuk, tempe tepung"
        # — five distinct dish_ids, one actual ingredient.
        for slot in request.no_repeat_slots:
            families: dict[str, list] = {}
            for i, dish in enumerate(candidates.get(slot, [])):
                key = dish.main_food_code or dish_key(dish)
                families.setdefault(key, []).extend(x[d, slot, i] for d in days)
            for key, picks in families.items():
                if len(picks) > request.max_family_per_plan:
                    model.Add(sum(picks) <= request.max_family_per_plan)

        # --- nutrients (soft floors) and budget (soft cap)
        nutrient_slack: dict[tuple[int, str], cp_model.IntVar] = {}
        budget_slack: dict[int, cp_model.IntVar] = {}
        penalties: list[cp_model.LinearExpr] = []

        for d in days:
            terms = {n: [] for n in binding}
            energy_terms, fat_terms, cost_terms = [], [], []
            quality_terms, preference_terms = [], []

            for slot in SLOTS:
                for i, dish in enumerate(candidates[slot]):
                    var = x[d, slot, i]
                    for n in binding:
                        value = int(round(dish.per_portion.get(n, 0.0) * NUTRIENT_SCALE))
                        if value:
                            terms[n].append(value * var)
                    energy_terms.append(
                        int(round(dish.energy_kcal * NUTRIENT_SCALE)) * var)
                    fat_terms.append(
                        int(round(dish.fat_g * NUTRIENT_SCALE)) * var)
                    cost_terms.append(
                        int(round(dish.cost_per_portion_idr * COST_SCALE)) * var)
                    # Bonuses are folded into the coefficient: CP-SAT linear
                    # expressions take integer coefficients, never division.
                    quality_terms.append(
                        int(round(dish.quality.score * W_QUALITY)) * var)
                    preference_terms.append(
                        int(round(_preference(dish, request) * W_PREFERENCE)) * var)

            for n in binding:
                target = int(round(targets[n] * NUTRIENT_SCALE))
                if target <= 0:
                    continue
                slack = model.NewIntVar(0, target, f"slack_{d}_{n}")
                nutrient_slack[d, n] = slack
                model.Add(sum(terms[n]) + slack >= target)
                # Normalise by the target so a gram of protein short and a gram
                # of fibre short cost the same, proportionally.
                weight = max(1, round(W_NUTRIENT_SLACK * 100 / target))
                penalties.append(weight * slack)

            # energy ceiling: a tray 35% over target wastes budget
            model.Add(sum(energy_terms)
                      <= int(round(targets["energy_kcal"] * 1.35 * NUTRIENT_SCALE)))

            # Fat at most 30% of meal energy. 9 kcal/g x fat <= ceiling x energy;
            # scaled by 100 to stay integral (900 x fat <= 30 x energy at 0.30).
            fat_ceiling = int(round(request.config.fat_energy_ceiling * 100))
            model.Add(900 * sum(fat_terms) <= fat_ceiling * sum(energy_terms))

            over = model.NewIntVar(0, budget * 3, f"over_{d}")
            budget_slack[d] = over
            model.Add(sum(cost_terms) <= budget + over)

            # Pure cost-minimisation (W_COST alone) had no reason to spend a
            # rupiah more than the nutrient floors required, so a solve
            # routinely landed 30-45% under the per-portion pagu — budget the
            # program actually allocated, sitting unused rather than buying
            # more/better food. `under` mirrors `over`'s shape on the other
            # side of the ceiling: minimising it pulls spend up toward the
            # budget (never past it — `over` still owns that side), with
            # W_COST left in only as a light tie-breaker among otherwise
            # equal options, not the dominant term it was before.
            under = model.NewIntVar(0, budget, f"under_{d}")
            model.Add(under >= budget - sum(cost_terms))

            penalties.append(W_BUDGET_SLACK * over)
            penalties.append(W_UNDERSPEND * under)
            penalties.append(W_COST * sum(cost_terms))
            penalties.append(-sum(preference_terms))
            penalties.append(-sum(quality_terms))

        model.Minimize(sum(penalties))

        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = request.max_solve_seconds
        # Match worker threads to what the host actually has — on a
        # CPU-constrained container (e.g. Render's free/starter tier),
        # spawning more search workers than cores adds scheduling overhead
        # and slows the solve down rather than speeding it up.
        solver.parameters.num_search_workers = max(1, min(8, os.cpu_count() or 1))
        solver.parameters.random_seed = request.random_seed
        status = solver.Solve(model)

        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return PlanResult(request, band, targets, [], [], 0.0,
                              solver.StatusName(status),
                              solver.WallTime(), total_candidates)

        return self._extract(solver, x, candidates, request, band, targets,
                             binding, nutrient_slack, budget_slack, status,
                             total_candidates)

    # ------------------------------------------------------------ extraction
    @staticmethod
    def _extract(solver, x, candidates, request, band, targets, binding,
                 nutrient_slack, budget_slack, status,
                 total_candidates) -> PlanResult:
        from .tkpi import NUTRIENT_KEYS

        day_plans: list[DayPlan] = []
        for d in range(request.days):
            chosen: dict[str, Optional[Dish]] = {s: None for s in SLOTS}
            for slot in SLOTS:
                for i, dish in enumerate(candidates[slot]):
                    if solver.Value(x[d, slot, i]):
                        chosen[slot] = dish
                        break
            nutrients = {k: 0.0 for k in NUTRIENT_KEYS}
            cost = 0.0
            for dish in chosen.values():
                if dish is None:
                    continue
                cost += dish.cost_per_portion_idr
                for k in NUTRIENT_KEYS:
                    nutrients[k] += dish.per_portion.get(k, 0.0)
            day_plans.append(DayPlan(d, chosen, nutrients, cost))

        relaxations = [
            Relaxation(d, n, solver.Value(slack) / NUTRIENT_SCALE, targets[n])
            for (d, n), slack in nutrient_slack.items()
            if solver.Value(slack) > 0
        ]
        relaxations.sort(key=lambda r: -r.share)
        overrun = sum(solver.Value(v) for v in budget_slack.values()) / COST_SCALE

        return PlanResult(
            request, band, targets, day_plans, relaxations, overrun,
            solver.StatusName(status), solver.WallTime(), total_candidates,
        )


# ---------------------------------------------------------------- heuristics


_DISH_NAME_NOISE = re.compile(
    r"\b(simple|simpel|sederhana|enak|mudah|praktis|ala|khas|homemade|"
    r"kekinian|anti gagal|super|spesial|special|no ribet|ekonomis|rumahan|"
    r"lezat|gurih|mantap|favorit|kesukaan|resep|by|untuk|kids?|friendly)\b")


def dish_key(dish: Dish) -> str:
    """Identity for variety purposes.

    The corpus holds many separate uploads of the same dish — a dozen rows all
    named some variant of "Tempe Mendoan". Constraining variety on dish_id lets
    those all appear in one week, so variety is enforced on a normalised name
    with the marketing words stripped out.
    """
    name = _DISH_NAME_NOISE.sub(" ", normalize(dish.name))
    return " ".join(sorted(set(name.split())))[:60] or normalize(dish.name)


def _dedupe_by_name(pool: list[Dish]) -> list[Dish]:
    """Keep the best-ranked dish per name; the pool arrives already sorted."""
    seen: set[str] = set()
    out: list[Dish] = []
    for dish in pool:
        key = dish_key(dish)
        if key in seen:
            continue
        seen.add(key)
        out.append(dish)
    return out


def _excluded(dish: Dish, terms: set[str]) -> bool:
    """True if any exclusion term appears in the dish name or its ingredients."""
    haystack = dish.name.lower() + " | " + dish.ingredient_index
    return any(term in haystack for term in terms)


def _preference(dish: Dish, request: PlanRequest) -> float:
    """0-1 regional fit: exact province beats island group beats national."""
    if request.province and dish.province == request.province:
        return PREFERENCE_EXACT_PROVINCE
    if request.island and dish.island == request.island:
        return PREFERENCE_SAME_ISLAND
    if dish.island == "Nasional":
        return PREFERENCE_NATIONAL
    return PREFERENCE_OTHER


@dataclass
class RankBreakdown:
    """Every term of a dish's pre-filter score, so the total can be audited."""

    protein_value: float
    energy_value: float
    cost_ratio: float
    efficiency: float
    preference: float
    quality: float
    total: float

    def to_dict(self) -> dict:
        return {k: round(v, 3) for k, v in self.__dict__.items()}


def rank_breakdown(dish: Dish, request: PlanRequest, targets: dict[str, float],
                   budget: float) -> RankBreakdown:
    """Score a dish and show the working."""
    cost = max(dish.cost_per_portion_idr, 1.0)
    protein_value = min(dish.protein_g / max(targets["protein_g"], 1.0), VALUE_CAP)
    energy_value = min(dish.energy_kcal / max(targets["energy_kcal"], 1.0), VALUE_CAP)
    cost_ratio = cost / budget
    efficiency = (protein_value + energy_value) / (cost_ratio + RANK_COST_FLOOR)
    preference = _preference(dish, request)
    quality = dish.quality.score
    return RankBreakdown(
        protein_value=protein_value, energy_value=energy_value,
        cost_ratio=cost_ratio, efficiency=efficiency,
        preference=preference, quality=quality,
        total=(efficiency
               + W_RANK_PREFERENCE * preference
               + W_RANK_QUALITY * quality),
    )


def _rank(dish: Dish, request: PlanRequest, targets: dict[str, float],
          budget: float) -> float:
    return rank_breakdown(dish, request, targets, budget).total
