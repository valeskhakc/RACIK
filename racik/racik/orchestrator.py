"""SEA-LION orchestration layer.

The model's job is routing and explanation, not arithmetic. Every number that
reaches the operator — grams, rupiah, kcal, adequacy — is produced by the
deterministic engine and handed to the model as tool output. The system prompt
forbids inventing figures, and the response carries the tool trace so a reader
can check each claim against the call that produced it. An LLM that quietly
rounds a protein figure would destroy the property that makes Racik worth
using, so it is never given the chance to compute one.

The loop is bounded in code, not in the prompt: a hard step cap plus repeated
call detection. A stop condition that lives only in a prompt is a stop
condition that eventually does not stop.

Without an API key the same tools are driven by a rule-based intent parser, so
the endpoint degrades to a narrower but still working planner rather than
failing.
"""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .agents import agent_biaya, agent_gizi, validate_plan
from .akg import BINDING_NUTRIENTS, NUTRIENT_META, SCHOOL_STAGES, STAGE_LABELS, stage_band
from .config import MBG, SLOTS, MBGConfig
from .i18n import DEFAULT_LANG, LANG_NAMES, norm_lang, t
from .nutrition import COMPONENT_ID_BASE
from .optimizer import MenuOptimizer, PlanRequest, PlanResult
from .prices import PriceTable
from .procurement import aggregate, totals
from .sealion import (ORCHESTRATOR_MODEL, ChatResult, SeaLionClient,
                      SeaLionError, SeaLionNotConfigured, ToolCall)
from .serialize import serialise_plan
from .statedb import StateStore
from .store import RacikStore


class RacikGenerateError(RuntimeError):
    """The generate()/replace_day() pipeline could not produce a plan.

    Carries `run_id`/`trace` when a run was persisted before the failure
    (an infeasible solve still deserves an auditable record of what Agen
    Gizi/Agen Biaya decided), so the caller can surface both.
    """

    def __init__(self, message: str, run_id: Optional[int] = None,
                 trace: Optional[list[dict]] = None):
        super().__init__(message)
        self.run_id = run_id
        self.trace = trace or []

MAX_STEPS = 6
MAX_REPEATED_CALLS = 2

SYSTEM_PROMPT = """\
You are Racik's planning orchestrator for Indonesia's Makan Bergizi Gratis (MBG) \
program. You help SPPG kitchen operators plan compliant, affordable menus.

ABSOLUTE RULE — you never compute or estimate numbers yourself. Every gram, \
rupiah, calorie, gram of protein, and adequacy percentage must come from a tool \
result. If you have not called a tool, you do not know the number. Never round, \
extrapolate, or invent a figure, and never describe a menu you have not planned \
with plan_menu.

How to work:
- Call plan_menu whenever the user asks for a menu. Read the parameters out of \
their message (days, stage, portions, province, budget, allergies).
- If the user asks whether the budget is enough, call budget_sensitivity.
- If they ask about one dish, call get_dish. To find alternatives, call \
search_dishes. For shopping quantities, call procurement_list.
- Call nutrition_reference when asked what the targets are.
- Call translate_dishes when the reader needs English dish names. Always show \
the Indonesian name and put the gloss beside it, never instead of it.
- Call explain_candidates when asked why a dish was or was not chosen, or what \
the filtering rules are.

How to answer:
- Reply in the language the user wrote in. Indonesian input gets an Indonesian \
answer.
- Be concise and concrete. Lead with the answer, then the numbers that support it.
- Report shortfalls honestly. If relaxations were returned, say which nutrient \
fell short and by how much. Never present a relaxed plan as fully compliant.
- Micronutrients (calcium, iron, zinc, vitamin A, vitamin C) are informational \
only: AKG 2019 micronutrient values disagree across secondary sources and are \
not enforced. Say so if you cite them.
- Costs are ingredient costs per portion, charged on gross purchased weight.
"""

# Appended to the system prompt so the interface language wins when the question
# itself is ambiguous (a bare "menu SD 100" reads as either language).
LANG_DIRECTIVE = """
The operator's interface is set to {language}. Write your answer in {language} unless their message is clearly in another language, in which case match theirs.
"""


# ---------------------------------------------------------------- tool schemas

_STAGE_ENUM = list(SCHOOL_STAGES)

TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "plan_menu",
            "description": ("Build an optimised multi-day MBG menu under AKG 2019 "
                            "nutrient floors, the per-portion budget and the Isi "
                            "Piringku tray composition. Returns the chosen dishes, "
                            "per-day cost and adequacy, and any relaxed floors."),
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "minimum": 1, "maximum": 7,
                             "description": "Number of menu days (default 5)."},
                    "stage": {"type": "string", "enum": _STAGE_ENUM,
                              "description": "School stage of the recipients."},
                    "portions": {"type": "integer", "minimum": 1,
                                 "description": "Portions served per day."},
                    "province": {"type": "string",
                                 "description": "Indonesian province for regional preference."},
                    "budget_per_portion_idr": {
                        "type": "number",
                        "description": "Total pagu per portion in rupiah (default 10000)."},
                    "ingredient_budget_share": {
                        "type": "number",
                        "description": "Share of the pagu available for raw "
                                       "ingredients (default 1.0 — the pagu "
                                       "is already a food/ingredient budget, "
                                       "not an all-inclusive program budget)."},
                    "exclude_terms": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Allergens or ingredients to exclude, e.g. ['udang','telur']."},
                },
                "required": ["stage"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "budget_sensitivity",
            "description": ("Compare AKG adequacy and realised cost across several "
                            "ingredient budgets, to answer whether a budget is "
                            "sufficient and what it would take to reach full compliance."),
            "parameters": {
                "type": "object",
                "properties": {
                    "stage": {"type": "string", "enum": _STAGE_ENUM},
                    "province": {"type": "string"},
                    "days": {"type": "integer", "minimum": 1, "maximum": 7},
                    "budgets_idr": {
                        "type": "array", "items": {"type": "number"},
                        "description": "Ingredient budgets per portion to test, e.g. [7000, 8500, 10000]."},
                },
                "required": ["stage"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_dishes",
            "description": ("Search the costed dish corpus by name, tray slot, "
                            "province and maximum cost. Use to find alternatives."),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Text to match in the dish name."},
                    "slot": {"type": "string", "enum": list(SLOTS)},
                    "province": {"type": "string"},
                    "max_cost_idr": {"type": "number"},
                    "min_protein_g": {"type": "number"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 25},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_dish",
            "description": ("Full detail for one dish: per-portion nutrition, cost, "
                            "and every ingredient with its gram provenance and TKPI code."),
            "parameters": {
                "type": "object",
                "properties": {"dish_id": {"type": "integer"}},
                "required": ["dish_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "procurement_list",
            "description": ("Aggregated shopping list for a planned menu, scaled to "
                            "the headcount, in gross purchased kilograms and rupiah."),
            "parameters": {
                "type": "object",
                "properties": {
                    "stage": {"type": "string", "enum": _STAGE_ENUM},
                    "days": {"type": "integer", "minimum": 1, "maximum": 7},
                    "portions": {"type": "integer", "minimum": 1},
                    "province": {"type": "string"},
                    "budget_per_portion_idr": {"type": "number"},
                    "top_n": {"type": "integer", "minimum": 1, "maximum": 40},
                },
                "required": ["stage"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "translate_dishes",
            "description": ("Translate Indonesian dish names into short English "
                            "glosses. The Indonesian name is always kept; the "
                            "gloss is an aid for readers who do not read "
                            "Indonesian. Results are cached."),
            "parameters": {
                "type": "object",
                "properties": {
                    "names": {"type": "array", "items": {"type": "string"},
                              "description": "Indonesian dish names to gloss."},
                },
                "required": ["names"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "explain_candidates",
            "description": ("Explain which dishes were eligible for the menu and "
                            "why others were filtered out: the hard gates, the "
                            "scoring formula, and how many dishes each gate "
                            "removed per tray slot."),
            "parameters": {
                "type": "object",
                "properties": {
                    "stage": {"type": "string", "enum": _STAGE_ENUM},
                    "province": {"type": "string"},
                    "budget_per_portion_idr": {"type": "number"},
                    "exclude_terms": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["stage"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "nutrition_reference",
            "description": ("AKG 2019 per-meal targets for a school stage (one MBG "
                            "meal is about one third of daily AKG), and which "
                            "nutrients are enforced versus informational."),
            "parameters": {
                "type": "object",
                "properties": {"stage": {"type": "string", "enum": _STAGE_ENUM}},
                "required": ["stage"],
            },
        },
    },
]


# ---------------------------------------------------------------- results


@dataclass
class ToolInvocation:
    name: str
    arguments: dict
    result: dict
    seconds: float

    def to_dict(self) -> dict:
        return {"tool": self.name, "arguments": self.arguments,
                "seconds": round(self.seconds, 2), "result": self.result}


@dataclass
class OrchestratorResult:
    answer: str
    trace: list[ToolInvocation] = field(default_factory=list)
    plan: Optional[dict] = None          # last plan payload, for the UI to render
    model: str = ""
    mode: str = "sealion"                # "sealion" | "fallback"
    steps: int = 0
    stopped_because: str = "completed"
    usage: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "answer": self.answer, "model": self.model, "mode": self.mode,
            "steps": self.steps, "stopped_because": self.stopped_because,
            "trace": [t.to_dict() for t in self.trace],
            "plan": self.plan, "usage": self.usage,
        }


@dataclass
class GenerateResult:
    """The result of the deterministic generate() pipeline: preprocessing ->
    Agen Gizi -> Agen Biaya -> CP-SAT -> Validator -> persisted menu plan."""

    run_id: int
    plan: dict
    trace: list[dict]
    resolved: dict

    def to_dict(self) -> dict:
        return {"run_id": self.run_id, "plan": self.plan,
                "trace": self.trace, "resolved": self.resolved}


# ---------------------------------------------------------------- orchestrator


class RacikOrchestrator:
    """Routes natural-language requests onto the deterministic planner."""

    def __init__(self, store: RacikStore, optimizer: MenuOptimizer,
                 client: Optional[SeaLionClient] = None,
                 max_steps: int = MAX_STEPS,
                 price_table: Optional[PriceTable] = None):
        self.store = store
        self.optimizer = optimizer
        self.client = client or SeaLionClient()
        self.max_steps = max_steps
        # Real per-province market data (WFP retail survey) for Agen Biaya —
        # see agents.py's regional_cost_index handling.
        self.price_table = price_table or PriceTable.load()
        self._plan_cache: dict[str, PlanResult] = {}
        self._last_plan_payload: Optional[dict] = None
        self._translator = None
        self._provinces = [p for p, _, _ in store.provinces()]
        self.tools: dict[str, Callable[..., dict]] = {
            "plan_menu": self._tool_plan_menu,
            "budget_sensitivity": self._tool_budget_sensitivity,
            "search_dishes": self._tool_search_dishes,
            "get_dish": self._tool_get_dish,
            "procurement_list": self._tool_procurement_list,
            "nutrition_reference": self._tool_nutrition_reference,
            "translate_dishes": self._tool_translate_dishes,
            "explain_candidates": self._tool_explain_candidates,
        }

    # ------------------------------------------------------------ entry point
    def ask(self, question: str, history: Optional[list[dict]] = None,
            lang: str = DEFAULT_LANG) -> OrchestratorResult:
        lang = norm_lang(lang)
        self._last_plan_payload = None
        if not self.client.configured:
            return self._fallback(question, lang)
        try:
            return self._run_agent(question, history or [], lang)
        except SeaLionNotConfigured:
            return self._fallback(question, lang)
        except SeaLionError as exc:
            result = self._fallback(question, lang)
            result.answer = (t(lang, "error.sealion", error=exc)
                             + "\n\n" + result.answer)
            return result

    # ------------------------------------------------------------ agent loop
    def _run_agent(self, question: str, history: list[dict],
                   lang: str = DEFAULT_LANG) -> OrchestratorResult:
        messages: list[dict] = [{
            "role": "system",
            "content": SYSTEM_PROMPT + LANG_DIRECTIVE.format(
                language=LANG_NAMES[norm_lang(lang)]),
        }]
        messages.extend(history)
        messages.append({"role": "user", "content": question})

        trace: list[ToolInvocation] = []
        seen: dict[str, int] = {}
        usage_total = {"prompt_tokens": 0, "completion_tokens": 0}
        stopped = "completed"
        answer = ""
        step = 0

        while step < self.max_steps:
            step += 1
            reply: ChatResult = self.client.chat(messages, tools=TOOL_SCHEMAS)
            for key in usage_total:
                usage_total[key] += int(reply.usage.get(key, 0) or 0)

            if not reply.wants_tools:
                answer = reply.content
                break

            messages.append(_assistant_turn(reply))
            for call in reply.tool_calls:
                signature = f"{call.name}:{json.dumps(call.arguments, sort_keys=True)}"
                seen[signature] = seen.get(signature, 0) + 1
                if seen[signature] > MAX_REPEATED_CALLS:
                    # Progress detection: the model is circling, not advancing.
                    payload = {"error": "repeated identical call; use the "
                                        "result you already have"}
                    trace.append(ToolInvocation(call.name, call.arguments,
                                                payload, 0.0))
                else:
                    payload = self._invoke(call, trace)
                messages.append({
                    "role": "tool", "tool_call_id": call.id, "name": call.name,
                    "content": json.dumps(payload, ensure_ascii=False)[:12000],
                })
        else:
            stopped = f"hit step cap ({self.max_steps})"

        if not answer:
            answer = self._summarise_without_model(trace, stopped, lang)

        return OrchestratorResult(
            answer=answer, trace=trace, plan=self._last_plan_payload,
            model=self.client.model, mode="sealion", steps=step,
            stopped_because=stopped, usage=usage_total,
        )

    def _invoke(self, call: ToolCall, trace: list[ToolInvocation]) -> dict:
        """Run one tool. Every outcome, including failure, lands in the trace.

        The trace is the audit trail behind the answer's numbers, so a call that
        errored has to be as visible as one that succeeded.
        """
        started = time.monotonic()
        handler = self.tools.get(call.name)
        if handler is None:
            result = {"error": f"unknown tool '{call.name}'",
                      "available": sorted(self.tools)}
        else:
            try:
                result = handler(**call.arguments)
            except TypeError as exc:
                result = {"error": f"bad arguments for {call.name}: {exc}"}
            except Exception as exc:                   # never kill the turn
                result = {"error": f"{type(exc).__name__}: {exc}"}
        trace.append(ToolInvocation(call.name, call.arguments, result,
                                    time.monotonic() - started))
        return result

    # ------------------------------------------------------------ tools
    def _tool_plan_menu(self, stage: str = "sd", days: int = 5,
                        portions: int = 100, province: str = "",
                        budget_per_portion_idr: float = None,
                        ingredient_budget_share: float = None,
                        exclude_terms: list[str] = None) -> dict:
        result = self._solve(stage, days, portions, province,
                             budget_per_portion_idr, ingredient_budget_share,
                             exclude_terms or [])
        payload = _plan_payload(result)
        self._last_plan_payload = payload
        return payload

    def _tool_budget_sensitivity(self, stage: str = "sd", province: str = "",
                                 days: int = 3,
                                 budgets_idr: list[float] = None) -> dict:
        budgets = budgets_idr or [7000, 8500, 10000]
        rows = []
        for budget in budgets[:5]:
            result = self._solve(stage, days, 100, province,
                                 budget_per_portion_idr=float(budget),
                                 ingredient_budget_share=1.0, exclude_terms=[])
            summary = result.summary()
            rows.append({
                "ingredient_budget_idr": round(float(budget)),
                "mean_adequacy": summary["mean_adequacy"],
                "mean_cost_per_portion_idr": summary["mean_cost_per_portion_idr"],
                "mean_energy_kcal": round(
                    sum(d.nutrients["energy_kcal"] for d in result.days)
                    / max(len(result.days), 1)),
                "relaxed_floors": summary["relaxed_floors"],
            })
        return {
            "stage": stage, "stage_label": STAGE_LABELS[stage], "days": days,
            "energy_target_kcal": round(
                stage_band(stage).per_meal(MBG.akg_fraction)["energy_kcal"]),
            "rows": rows,
            "note": ("adequacy is the mean share of enforced AKG macro floors "
                     "met; 1.0 means every floor was met without relaxation"),
        }

    def _tool_search_dishes(self, query: str = "", slot: str = "",
                            province: str = "", max_cost_idr: float = None,
                            min_protein_g: float = None,
                            limit: int = 10) -> dict:
        rows = self.store.search_dishes(
            query=query, slot=slot or None, province=province or None,
            max_cost_idr=max_cost_idr, min_protein_g=min_protein_g,
            limit=max(1, min(int(limit or 10), 25)))
        return {"count": len(rows), "dishes": rows}

    def _tool_get_dish(self, dish_id: int) -> dict:
        dish = self.store.dish(int(dish_id))
        if dish is None:
            return {"error": f"dish {dish_id} not found"}
        lines = self.store.lines(dish.dish_id)
        return {
            "dish_id": dish.dish_id, "name": dish.name, "slot": dish.slot,
            "province": dish.province, "servings_per_recipe": dish.servings,
            "cooking_method": dish.method,
            "cost_per_portion_idr": round(dish.cost_per_portion_idr),
            "data_quality": dish.quality.label,
            "per_portion": {k: round(dish.per_portion.get(k, 0.0), 1)
                            for k in ("energy_kcal", "protein_g", "fat_g",
                                      "carb_g", "fibre_g")},
            "ingredients": [
                {"raw": ln.raw, "gross_g": round(ln.gross_g, 1),
                 "tkpi": ln.tkpi_name, "basis": ln.urt_confidence,
                 "cost_idr": round(ln.cost_idr)}
                for ln in lines[:25]
            ],
        }

    def _tool_procurement_list(self, stage: str = "sd", days: int = 5,
                               portions: int = 100, province: str = "",
                               budget_per_portion_idr: float = None,
                               top_n: int = 15) -> dict:
        result = self._solve(stage, days, portions, province,
                             budget_per_portion_idr, None, [])
        self._last_plan_payload = _plan_payload(result)
        lines = aggregate(result, self.store)
        return {
            "portions_per_day": portions, "days": days,
            "totals": totals(lines),
            "lines": [
                {"name": ln.name, "gross_kg": round(ln.gross_kg, 2),
                 "cost_idr": round(ln.cost_idr)}
                for ln in lines[:max(1, min(int(top_n or 15), 40))]
            ],
        }

    def _tool_translate_dishes(self, names: list[str]) -> dict:
        from .translate import DishTranslator
        if self._translator is None:
            self._translator = DishTranslator(self.client)
        glosses = self._translator.translate([str(n) for n in (names or [])][:60])
        return {"count": len(glosses),
                "glosses": [g.to_dict() for g in glosses],
                "note": ("method 'cache'/'sealion' is a model translation; "
                         "'rule' is an offline approximation")}

    def _tool_explain_candidates(self, stage: str = "sd", province: str = "",
                                 budget_per_portion_idr: float = None,
                                 exclude_terms: list[str] = None) -> dict:
        province = _closest_province(province, self._provinces)
        config = MBGConfig(
            budget_per_portion_idr=float(budget_per_portion_idr
                                         or MBG.budget_per_portion_idr),
            ingredient_budget_share=MBG.ingredient_budget_share,
            regional_cost_index=self.price_table.regional_index_for(province))
        request = PlanRequest(
            stage=stage if stage in SCHOOL_STAGES else "sd",
            province=province,
            island=self.store.island_of(province) if province else "",
            config=config,
            exclude_terms=[t for t in (exclude_terms or []) if str(t).strip()])
        return self.optimizer.explain_candidates(request)

    def _tool_nutrition_reference(self, stage: str) -> dict:
        band = stage_band(stage)
        meal = band.per_meal(MBG.akg_fraction)
        return {
            "stage": stage, "stage_label": STAGE_LABELS[stage],
            "basis": "AKG 2019 (Permenkes 28/2019); one MBG meal ~= 1/3 of daily AKG",
            "enforced": {
                k: {"target": round(meal[k], 1), "unit": NUTRIENT_META[k][2]}
                for k in BINDING_NUTRIENTS},
            "informational_only": {
                k: {"target": round(meal[k], 1), "unit": NUTRIENT_META[k][2]}
                for k in ("calcium_mg", "iron_mg", "zinc_mg", "vit_a_mcg", "vit_c_mg")},
            "caveat": ("micronutrient AKG values disagree across secondary "
                       "sources and are not enforced as constraints"),
        }

    # ------------------------------------------------------------ solving
    def _solve(self, stage: str, days: int, portions: int, province: str,
               budget_per_portion_idr: Optional[float],
               ingredient_budget_share: Optional[float],
               exclude_terms: list[str]) -> PlanResult:
        stage = stage if stage in SCHOOL_STAGES else "sd"
        days = max(1, min(int(days or 5), 7))
        portions = max(1, int(portions or 100))
        province = _closest_province(province, self._provinces)
        config = MBGConfig(
            budget_per_portion_idr=float(budget_per_portion_idr
                                         or MBG.budget_per_portion_idr),
            ingredient_budget_share=float(ingredient_budget_share
                                          if ingredient_budget_share is not None
                                          else MBG.ingredient_budget_share),
            regional_cost_index=self.price_table.regional_index_for(province),
        )
        island = self.store.island_of(province) if province else ""
        request = PlanRequest(
            days=days, stage=stage, portions=portions, province=province,
            island=island, config=config,
            exclude_terms=[t for t in (exclude_terms or []) if str(t).strip()],
            max_solve_seconds=10.0,
        )
        key = json.dumps({
            "d": days, "s": stage, "p": portions, "prov": province,
            "b": config.budget_per_portion_idr,
            "sh": config.ingredient_budget_share,
            "x": sorted(request.exclude_terms)}, sort_keys=True)
        if key not in self._plan_cache:
            self._plan_cache[key] = self.optimizer.solve(request)
        return self._plan_cache[key]

    # ------------------------------------------------------------ generate pipeline
    def generate(self, *, days: int, portions: int, province: str,
                stage: Optional[str], budget_mode: str, budget_value: float,
                notes_text: str, exclude_dish_ids: Optional[list[int]] = None,
                history_days: int = 14, lang: str = DEFAULT_LANG,
                state: StateStore) -> GenerateResult:
        """Preprocessing -> Agen Gizi -> Agen Biaya -> CP-SAT -> Validator ->
        persisted menu plan (workflow-diagram stages 2-6).

        Sequencing is fixed in code, not left to a model's tool choice, so a
        live demo reliably exercises every stage on every call — the same
        "bounded in code, not in the prompt" discipline the ask() loop above
        already applies to its step cap.
        """
        lang = norm_lang(lang)
        days = max(1, min(int(days or 5), 7))
        portions = max(1, int(portions or 100))
        province_r = _closest_province(province, self._provinces)
        island = self.store.island_of(province_r) if province_r else ""

        # Budget-mode arithmetic is plain code, never an agent's job — see
        # agents.py's module docstring on the "never calculates" invariant.
        budget_value = float(budget_value or MBG.budget_per_portion_idr)
        budget_per_portion = (budget_value / max(portions * days, 1)
                              if budget_mode == "total" else budget_value)

        trace: list[dict] = [{
            "agent": "preprocessing", "source": "deterministic",
            "rationale": f"Candidate pool narrowed by gates G1-G5 for "
                         f"{province_r or 'no regional preference'}.",
            "params": {"province": province_r, "island": island},
        }]

        # The operator's past reject decisions — "the feedback for the AI
        # model to learn from" (see the workflow diagram's operator-review ->
        # solver feedback arrow). Passed to Agen Gizi so its rationale names
        # what it's reacting to, and hard-excluded below regardless of what
        # either agent decides, so a rejected dish never silently resurfaces.
        feedback = state.rejected_dishes(limit=20)

        # Independent calls (neither reads the other's output — see the
        # module docstring's workflow diagram, where they run side by side)
        # sharing one client instance, which is safe: SeaLionClient locks its
        # own throttle-and-send sequence, and boto3 clients are documented
        # thread-safe. Running them on two threads instead of one-after-
        # another overlaps their network latency instead of paying it twice.
        with ThreadPoolExecutor(max_workers=2) as pool:
            gizi_future = pool.submit(agent_gizi, self.client, stage_hint=stage,
                                      notes_text=notes_text, feedback_history=feedback,
                                      lang=lang)
            biaya_future = pool.submit(agent_biaya, self.client,
                                       budget_per_portion_idr=budget_per_portion,
                                       portions=portions, province=province_r,
                                       price_table=self.price_table, lang=lang)
            gizi = gizi_future.result()
            biaya = biaya_future.result()
        trace.append(gizi.to_dict())
        trace.append(biaya.to_dict())

        resolved_stage = gizi.params.get("stage") or (
            stage if stage in SCHOOL_STAGES else "sd")
        config = MBGConfig(
            budget_per_portion_idr=budget_per_portion,
            ingredient_budget_share=biaya.params.get(
                "ingredient_budget_share", MBG.ingredient_budget_share),
            regional_cost_index=biaya.params.get("regional_cost_index", 1.0),
        )

        recent_ids = (state.recent_served_dish_ids(within_days=history_days)
                     if history_days > 0 else set())
        rejected_ids = {int(f["dish_id"]) for f in feedback}
        exclude_ids = {int(i) for i in (exclude_dish_ids or [])} | recent_ids | rejected_ids

        resolved = {
            "stage": resolved_stage, "province": province_r, "island": island,
            "budget_per_portion_idr": config.budget_per_portion_idr,
            "ingredient_budget_share": config.ingredient_budget_share,
            "regional_cost_index": config.regional_cost_index,
            "exclude_terms": list(gizi.params.get("exclude_terms", [])),
            "enforce_micronutrients": bool(gizi.params.get(
                "enforce_micronutrients", False)),
            "portions": portions,
        }
        request = PlanRequest(
            days=days, stage=resolved_stage, portions=portions,
            province=province_r, island=island, config=config,
            exclude_terms=resolved["exclude_terms"],
            exclude_dish_ids=list(exclude_ids),
            enforce_micronutrients=resolved["enforce_micronutrients"],
            max_solve_seconds=25.0,
        )
        result = self.optimizer.solve(request)
        cp_sat_note = ""

        # Menu-history rotation (recent_ids) is a preference, not a hard
        # requirement — it should never be the reason a plan can't be built
        # at all. A tight budget can genuinely exhaust a small candidate
        # pool after enough regenerations; retry once without the decay
        # window (explicit vetoes and this run's own rejected-dish exclusion
        # stay in force) before reporting infeasible.
        if not result.feasible and recent_ids:
            narrower_ids = rejected_ids | {int(i) for i in (exclude_dish_ids or [])}
            retry_request = PlanRequest(
                days=days, stage=resolved_stage, portions=portions,
                province=province_r, island=island, config=config,
                exclude_terms=resolved["exclude_terms"],
                exclude_dish_ids=list(narrower_ids),
                enforce_micronutrients=resolved["enforce_micronutrients"],
                max_solve_seconds=25.0,
            )
            retried = self.optimizer.solve(retry_request)
            if retried.feasible:
                result = retried
                cp_sat_note = (f" Retried without the {history_days}-day "
                               f"rotation window (kept explicit vetoes) after "
                               f"the first attempt found no candidates.")

        # Agen Gizi's exclude_terms are a semantic judgement call (e.g. a
        # generic "kacang" for "cut expensive nuts/shrimp" can also knock out
        # unrelated soy-based staples like tempeh/tofu) — a real category of
        # LLM output that should degrade the plan, never break it entirely.
        # If exclusions are still the reason nothing solves, drop them and
        # retry once more; explicit dish-id vetoes (operator rejections,
        # this run's own exclusions) are exact matches, not judgement calls,
        # and stay in force either way.
        if not result.feasible and resolved["exclude_terms"]:
            retry_request = PlanRequest(
                days=days, stage=resolved_stage, portions=portions,
                province=province_r, island=island, config=config,
                exclude_terms=[],
                exclude_dish_ids=list(exclude_ids),
                enforce_micronutrients=resolved["enforce_micronutrients"],
                max_solve_seconds=25.0,
            )
            retried = self.optimizer.solve(retry_request)
            if retried.feasible:
                result = retried
                dropped = ", ".join(resolved["exclude_terms"])
                cp_sat_note = (f" Retried without Agen Gizi's exclusion terms "
                               f"({dropped}) after they left no feasible menu — "
                               f"they were too broad a filter, not a hard "
                               f"requirement.")
                resolved["exclude_terms"] = []

        trace.append({
            "agent": "cp_sat", "source": "solver",
            "rationale": (
                f"{result.status}: {result.candidates_considered} candidates "
                f"considered, solved in {result.solve_seconds:.2f}s.{cp_sat_note}"
                if result.feasible else
                f"{result.status}: no feasible menu under these constraints."),
            "params": {"status": result.status,
                       "candidates_considered": result.candidates_considered,
                       "solve_seconds": round(result.solve_seconds, 2)},
        })

        if not result.feasible:
            run_id = state.record_run(stage=resolved_stage, days=days,
                                      portions=portions, province=province_r,
                                      resolved=resolved, trace=trace)
            raise RacikGenerateError(
                f"no menu could be built (solver status: {result.status})",
                run_id=run_id, trace=trace)

        validator = validate_plan(result, lang=lang)
        trace.append(validator.to_dict())

        plan_payload = serialise_plan(result, lang=lang)
        plan_payload["procurement"] = self._procurement_payload(result)
        run_id = state.record_run(stage=resolved_stage, days=days,
                                  portions=portions, province=province_r,
                                  resolved=resolved, trace=trace)
        self._persist_served(state, run_id, result)
        plan_payload["run_id"] = run_id

        return GenerateResult(run_id=run_id, plan=plan_payload, trace=trace,
                              resolved=resolved)

    def replace_day(self, *, run_id: int, day_index: int,
                    lang: str = DEFAULT_LANG, state: StateStore) -> dict:
        """Solve a one-day replacement for a rejected day, excluding whatever
        was served on it — "operator review" feeding back into the CP-SAT
        solver, per the workflow diagram's arrow from review to the solver.
        """
        lang = norm_lang(lang)
        run = state.run(run_id)
        if run is None:
            raise RacikGenerateError(f"run {run_id} not found")
        resolved = run["resolved"]

        rejected_ids = {
            row["dish_id"] for row in state.recent_served(limit=1000)
            if row["run_id"] == run_id and row["day_index"] == day_index
        }
        exclude_ids = rejected_ids | state.recent_served_dish_ids(within_days=14)

        config = MBGConfig(
            budget_per_portion_idr=resolved["budget_per_portion_idr"],
            ingredient_budget_share=resolved["ingredient_budget_share"],
            regional_cost_index=resolved.get("regional_cost_index", 1.0),
        )
        request = PlanRequest(
            days=1, stage=resolved["stage"], portions=run["portions"],
            province=resolved["province"], island=resolved.get("island", ""),
            config=config, exclude_terms=resolved.get("exclude_terms", []),
            exclude_dish_ids=list(exclude_ids),
            enforce_micronutrients=resolved.get("enforce_micronutrients", False),
            max_solve_seconds=10.0,
        )
        result = self.optimizer.solve(request)
        if not result.feasible:
            raise RacikGenerateError(
                f"no replacement menu could be built (status: {result.status})")

        day_payload = serialise_plan(result, lang=lang)["days"][0]
        day_payload["index"] = day_index      # report under the original day's slot
        day_payload["procurement"] = self._procurement_payload(result)["by_day"][0]
        self._persist_served(state, run_id, result, day_index_override=day_index)
        return day_payload

    def _procurement_payload(self, result: PlanResult) -> dict:
        """Shopping-list totals for the whole run, plus a per-day breakdown —
        computed straight from the already-solved `PlanResult` so it can
        never disagree with the dishes the operator is looking at, unlike a
        separate /api/procurement call that would re-solve from scratch."""
        whole_run = aggregate(result, self.store)
        return {
            "totals": totals(whole_run),
            "lines": [line.to_dict() for line in whole_run],
            "by_day": [
                {
                    "day_index": day.day_index,
                    "totals": totals(aggregate(result, self.store,
                                               day_indexes=[day.day_index])),
                    "lines": [line.to_dict() for line in aggregate(
                        result, self.store, day_indexes=[day.day_index])],
                }
                for day in result.days
            ],
        }

    @staticmethod
    def _persist_served(state: StateStore, run_id: int, result: PlanResult,
                        day_index_override: Optional[int] = None) -> None:
        """Log real (non-synthetic) dishes to the served log — Menu history."""
        entries = []
        for day in result.days:
            day_index = day_index_override if day_index_override is not None else day.day_index
            for slot, dish_ in day.dishes.items():
                if dish_ is not None and dish_.dish_id < COMPONENT_ID_BASE:
                    entries.append((day_index, slot, dish_.dish_id, dish_.name))
        if entries:
            state.record_served(run_id, entries)

    # ------------------------------------------------------------ no-model paths
    @staticmethod
    def _summarise_without_model(trace: list[ToolInvocation], stopped: str,
                                 lang: str = DEFAULT_LANG) -> str:
        """Used when the loop ends without the model producing prose."""
        if not trace:
            return t(lang, "fallback.empty")
        last = trace[-1]
        return (t(lang, "fallback.stopped", reason=stopped, tool=last.name)
                + "\n\n"
                + json.dumps(last.result, ensure_ascii=False, indent=2)[:1500])

    def _fallback(self, question: str,
                  lang: str = DEFAULT_LANG) -> OrchestratorResult:
        """Rule-based path when SEA-LION is unavailable.

        Narrower than the model — it only plans — but it uses the same tools and
        the same numbers, so the answer stays trustworthy.
        """
        params = parse_intent(question, self._provinces)
        trace: list[ToolInvocation] = []
        started = time.monotonic()
        payload = self._tool_plan_menu(**params)
        trace.append(ToolInvocation("plan_menu", params, payload,
                                    time.monotonic() - started))
        return OrchestratorResult(
            answer=_render_plan_text(payload, params, lang),
            trace=trace, plan=payload, model="rule-based fallback",
            mode="fallback", steps=1,
            stopped_because="sea-lion unavailable",
        )


# ---------------------------------------------------------------- helpers


def _assistant_turn(reply: ChatResult) -> dict:
    return {
        "role": "assistant",
        "content": reply.content or None,
        "tool_calls": [
            {"id": c.id, "type": "function",
             "function": {"name": c.name,
                          "arguments": json.dumps(c.arguments, ensure_ascii=False)}}
            for c in reply.tool_calls
        ],
    }


def _plan_payload(result: PlanResult) -> dict:
    """Compact plan summary — small enough to hand back as a tool result."""
    return {
        "summary": result.summary(),
        "stage": result.band.label_id,
        "targets_per_meal": {
            k: round(result.targets[k], 1)
            for k in ("energy_kcal", "protein_g", "fat_g", "carb_g", "fibre_g")},
        "days": [
            {
                "day": day.day_index + 1,
                "cost_per_portion_idr": round(day.cost_per_portion_idr),
                "adequacy": round(result.adequacy(day), 3),
                "energy_kcal": round(day.nutrients["energy_kcal"]),
                "protein_g": round(day.nutrients["protein_g"], 1),
                "dishes": {slot: (day.dishes[slot].name if day.dishes[slot] else None)
                           for slot in SLOTS},
                "dish_ids": {slot: (day.dishes[slot].dish_id if day.dishes[slot] else None)
                             for slot in SLOTS},
            }
            for day in result.days
        ],
        "relaxations": [
            {"day": r.day_index + 1, "nutrient": NUTRIENT_META[r.nutrient][0],
             "short_by": round(r.shortfall, 1),
             "target": round(r.target, 1), "share": round(r.share, 3)}
            for r in result.relaxations[:8]
        ],
    }


def _render_plan_text(payload: dict, params: dict,
                      lang: str = DEFAULT_LANG) -> str:
    """Plain-language rendering of a plan, for the no-model path."""
    summary = payload["summary"]
    province = (t(lang, "plan.province", province=params["province"])
                if params.get("province") else "")
    lines = [
        t(lang, "plan.header", days=len(payload["days"]),
          stage=payload["stage"], portions=params.get("portions", 100),
          province=province),
        t(lang, "plan.cost",
          cost=f"{summary['mean_cost_per_portion_idr']:,.0f}",
          budget=f"{summary['ingredient_budget_idr']:,.0f}",
          adequacy=f"{summary['mean_adequacy'] * 100:.0f}%"),
        "",
    ]
    for day in payload["days"]:
        lines.append(t(lang, "plan.day", day=day["day"],
                       cost=f"{day['cost_per_portion_idr']:,}",
                       kcal=day["energy_kcal"], protein=day["protein_g"],
                       adequacy=f"{day['adequacy'] * 100:.0f}%"))
        lines.append("    " + " · ".join(v for v in day["dishes"].values() if v))
    if payload["relaxations"]:
        lines += ["", t(lang, "plan.relax_header")]
        for r in payload["relaxations"][:5]:
            lines.append(t(lang, "plan.relax_row", day=r["day"],
                           nutrient=r["nutrient"], short=r["short_by"],
                           target=r["target"],
                           share=f"{r['share'] * 100:.0f}%"))
    return "\n".join(lines)


# ---------------------------------------------------------------- intent parsing

_STAGE_PATTERNS = [
    ("paud", r"\bpaud\b|\btk\b|taman kanak|balita|4-6|prasekolah"),
    ("sma", r"\bsma\b|\bsmk\b|\bma\b\b|16-18|menengah atas"),
    ("smp", r"\bsmp\b|\bmts\b|13-15|menengah pertama"),
    ("sd", r"\bsd\b|\bmi\b|7-12|sekolah dasar|dasar"),
]
_NUM_WORDS = {"satu": 1, "dua": 2, "tiga": 3, "empat": 4, "lima": 5,
              "enam": 6, "tujuh": 7, "seminggu": 5, "sepekan": 5}


def parse_intent(text: str, provinces: list[str]) -> dict:
    """Extract planner parameters from free Indonesian/English text.

    Deliberately conservative: anything it cannot read confidently is left at
    the default rather than guessed at.
    """
    low = " " + (text or "").lower() + " "
    params: dict[str, Any] = {}

    for stage, pattern in _STAGE_PATTERNS:
        if re.search(pattern, low):
            params["stage"] = stage
            break

    days = re.search(r"(\d+)\s*hari", low)
    if days:
        params["days"] = max(1, min(int(days.group(1)), 7))
    elif re.search(r"\bseminggu\b|\bsepekan\b|\bone week\b|\bweekly\b", low):
        params["days"] = 5          # an MBG service week is Monday-Friday
    else:
        for word, value in _NUM_WORDS.items():
            if re.search(rf"\b{word}\s+hari\b", low):
                params["days"] = value
                break

    portions = re.search(
        r"(\d[\d.,]*)\s*(?:porsi|siswa|anak|murid|penerima|orang)", low)
    if portions:
        params["portions"] = max(1, int(re.sub(r"[.,]", "", portions.group(1))))

    budget = re.search(
        r"(?:rp\.?\s*)?(\d[\d.,]*)\s*(ribu|rb|k)?\s*(?:per\s*porsi|/porsi|perporsi)?",
        low)
    money = re.search(r"rp\.?\s*(\d[\d.,]*)\s*(ribu|rb|k)?", low)
    chosen = money or (budget if "anggaran" in low or "pagu" in low else None)
    if chosen:
        raw = float(re.sub(r"[.,]", "", chosen.group(1)))
        if chosen.lastindex and chosen.group(2):
            raw *= 1000
        if 1000 <= raw <= 100_000:
            params["budget_per_portion_idr"] = raw

    for province in provinces:
        if province.lower() in low:
            params["province"] = province
            break

    excluded: list[str] = []
    for match in re.finditer(
            r"(?:tanpa|tidak boleh|hindari|alergi|bebas|no)\s+([a-z\s,]{3,60})", low):
        for term in re.split(r"[,\s]+dan\s+|,", match.group(1)):
            term = term.strip()
            if 2 < len(term) < 25:
                excluded.append(term.split(" untuk ")[0].strip())
    if excluded:
        params["exclude_terms"] = excluded[:6]

    params.setdefault("stage", "sd")
    return params


def _closest_province(name: str, provinces: list[str]) -> str:
    """Resolve a loosely-typed province name onto a corpus province."""
    if not name:
        return ""
    target = name.strip().lower()
    for province in provinces:
        if province.lower() == target:
            return province
    for province in provinces:
        if target in province.lower() or province.lower() in target:
            return province
    return ""
