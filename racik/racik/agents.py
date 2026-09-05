"""Agen Gizi, Agen Biaya, and the Validator — the three pipeline stages the
workflow diagram adds around the deterministic CP-SAT solve.

Same invariant as the rest of this codebase (see docs/CODE_MAP.md): **the
model orchestrates, never calculates**. Agen Gizi and Agen Biaya only ever
emit *configuration knobs and a one-paragraph rationale* — never a gram, a
rupiah, or an adequacy percentage. Those knobs feed `PlanRequest`/`MBGConfig`;
`MenuOptimizer.solve()` (unchanged) is still the only place a number is
computed. The Validator runs after the solve and is deliberately **not** an
LLM call at all — it re-derives pass/fail from the same `PlanResult` fields
`/api/plan` already serialises, so it can never disagree with the numbers the
operator sees elsewhere in the app.

Each agent is a single temperature-0 tool-call against whichever client
`bedrock.make_client()` picked (Bedrock Converse, SEA-LION, or a local
OpenAI-compatible server). With no LLM configured — the default, zero-key
state — a conservative rule-based fallback answers instead, so the pipeline
always completes and the app stays demoable without any credentials.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from .akg import SCHOOL_STAGES, STAGE_LABELS
from .i18n import DEFAULT_LANG, norm_lang, t
from .optimizer import PlanResult
from .prices import PriceTable
from .sealion import ChatResult, SeaLionClient, SeaLionError, SeaLionNotConfigured

# ---------------------------------------------------------------- shared shapes


@dataclass
class AgentAdvice:
    agent: str                      # "agen_gizi" | "agen_biaya"
    source: str                     # "llm" | "rule-based"
    rationale: str
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"agent": self.agent, "source": self.source,
                "rationale": self.rationale, "params": self.params}


@dataclass
class ValidatorReport:
    passed: bool
    rationale: str
    per_day: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"agent": "validator", "passed": self.passed,
                "rationale": self.rationale, "per_day": self.per_day}


# ---------------------------------------------------------------- Agen Gizi

_GIZI_TOOL = [{
    "type": "function",
    "function": {
        "name": "gizi_advice",
        "description": ("Decide nutrition-related adjustments to an MBG menu "
                        "plan from the operator's free-text notes."),
        "parameters": {
            "type": "object",
            "properties": {
                "stage": {"type": "string", "enum": list(SCHOOL_STAGES),
                          "description": "School stage the notes imply, if any."},
                "exclude_terms": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Concrete food/ingredient/flavor words to "
                                   "exclude, each one that could literally "
                                   "appear in a dish's name or ingredient "
                                   "list (e.g. ['udang','kacang','pedas']). "
                                   "Never an abstract reason like 'too "
                                   "expensive' — that matches no dish text "
                                   "and silently excludes nothing; cost "
                                   "concerns belong in the rationale, not here."},
                "enforce_micronutrients": {
                    "type": "boolean",
                    "description": "True only if the notes explicitly ask for "
                                   "micronutrient targets (calcium/iron/zinc/"
                                   "vitamin A/C) to be enforced, not just reported."},
                "rationale": {
                    "type": "string",
                    "description": "One or two sentences explaining the "
                                   "adjustments, in the requested language."},
            },
            "required": ["rationale"],
        },
    },
}]

# Reuses the same exclusion-marker vocabulary as orchestrator.parse_intent,
# scoped down to just notes-text -> exclude_terms since that's all this agent
# needs from the rule-based path.
_EXCLUSION_MARKERS = ("tanpa", "tidak boleh", "hindari", "alergi", "bebas",
                      "no", "avoid", "without", "reduce")
_EXCLUSION_PATTERN = re.compile(
    r"(?:" + "|".join(_EXCLUSION_MARKERS) + r")\s+([a-z\s,]{3,60})", re.IGNORECASE)
# A marker word can also start a later comma-separated clause inside the same
# match (e.g. "tanpa udang, alergi kacang" captures "udang, alergi kacang" in
# one go) — stripped per-term below so "alergi" doesn't end up part of the term.
_LEADING_MARKER = re.compile(
    r"^(?:" + "|".join(_EXCLUSION_MARKERS) + r")\s+", re.IGNORECASE)
_STAGE_PATTERN = [
    ("paud", r"\bpaud\b|\btk\b|taman kanak|balita|prasekolah"),
    ("sma", r"\bsma\b|\bsmk\b|\bma\b|menengah atas"),
    ("smp", r"\bsmp\b|\bmts\b|menengah pertama"),
    ("sd", r"\bsd\b|\bmi\b|sekolah dasar"),
]

# exclude_terms is a text filter matched against dish/ingredient text (G3 in
# optimizer.py's candidate funnel) — despite the system prompt telling it not
# to, the model has repeatedly turned a cost-driven rejection pattern into a
# literal price-word "exclude_term" ("too expensive", "mahal", "terlalu
# mahal", "harga mahal"). In English this is inert (the corpus is Indonesian,
# so it matches nothing); in Indonesian it's actively harmful, since "mahal"
# can genuinely appear in scraped recipe commentary and silently knock out
# unrelated dishes for a reason that has nothing to do with their content —
# confirmed live: adequacy dropped across every day of a run after this
# happened. Cost belongs to Agen Biaya, never to a content filter, so this is
# enforced in code rather than trusted to prompt wording alone.
_COST_TERM_PATTERN = re.compile(
    r"mahal|expensive|murah|cheap|harga|price|biaya|cost|budget|pagu",
    re.IGNORECASE)


def _drop_cost_terms(terms: list[str]) -> list[str]:
    return [t for t in terms if not _COST_TERM_PATTERN.search(t)]


def agent_gizi(client: SeaLionClient, *, stage_hint: Optional[str],
               notes_text: str, feedback_history: Optional[list[dict]] = None,
               lang: str = DEFAULT_LANG) -> AgentAdvice:
    lang = norm_lang(lang)
    feedback_history = feedback_history or []
    if client.configured:
        try:
            return _agent_gizi_llm(client, stage_hint, notes_text,
                                   feedback_history, lang)
        except SeaLionError:
            pass          # fall through to the rule-based path — never blocks a plan
    return _agent_gizi_rule(stage_hint, notes_text, feedback_history, lang)


def _describe_feedback(feedback_history: list[dict]) -> str:
    if not feedback_history:
        return "(none yet)"
    parts = []
    for entry in feedback_history[:8]:
        reasons = ", ".join(entry.get("reasons", [])) or "no reason given"
        parts.append(f"\"{entry['dish_name']}\" rejected ({reasons})")
    return "; ".join(parts)


def _agent_gizi_llm(client: SeaLionClient, stage_hint: Optional[str],
                    notes_text: str, feedback_history: list[dict],
                    lang: str) -> AgentAdvice:
    system = ("You are Agen Gizi, the nutrition-constraints agent in Racik's "
              "MBG menu planner. You never compute nutrient values yourself — "
              "you only translate the operator's notes into structured "
              "planning adjustments by calling gizi_advice. Reply in the "
              "language requested. The operator's past rejections are already "
              "hard-excluded by the pipeline before you run — you don't need "
              "to re-list them in exclude_terms — but look for a *pattern* "
              "across them (e.g. repeatedly rejecting spicy or fish dishes) "
              "and add a broader exclude_term for it if one is clear.\n\n"
              "exclude_terms is a text filter: each entry must be a concrete "
              "food/ingredient/flavor word that would literally appear in a "
              "dish's name or ingredient list (e.g. 'udang', 'pedas', 'kacang') "
              "— never an abstract reason like 'too expensive' or 'not "
              "popular', which can't match any dish text and would silently "
              "do nothing. If the pattern you notice is about cost rather "
              "than content (e.g. repeatedly rejected as too expensive), that "
              "is Agen Biaya's job, not exclude_terms — just name the pattern "
              "in your rationale instead.")
    stage_desc = stage_hint or "(none - infer it from the notes, default sd if unclear)"
    notes_desc = notes_text or "(none given)"
    user = (f"Requested language: {lang}\n"
            f"School stage already selected: {stage_desc}\n"
            f"Operator's free-text notes: {notes_desc}\n"
            f"Recently rejected dishes and reasons: {_describe_feedback(feedback_history)}")
    reply: ChatResult = client.chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        tools=_GIZI_TOOL, tool_choice="auto")
    if not reply.wants_tools:
        raise SeaLionError("agent_gizi: model returned no tool call")
    args = reply.tool_calls[0].arguments
    params = {
        "exclude_terms": _drop_cost_terms(
            [str(x) for x in args.get("exclude_terms", []) if str(x).strip()]),
        "enforce_micronutrients": bool(args.get("enforce_micronutrients", False)),
    }
    stage = args.get("stage")
    if stage in SCHOOL_STAGES:
        params["stage"] = stage
    rationale = str(args.get("rationale") or "").strip() or t(lang, "agent.gizi.rationale_default")
    return AgentAdvice("agen_gizi", "llm", rationale, params)


def _agent_gizi_rule(stage_hint: Optional[str], notes_text: str,
                     feedback_history: list[dict], lang: str) -> AgentAdvice:
    low = f" {notes_text.lower()} "
    excluded: list[str] = []
    for match in _EXCLUSION_PATTERN.finditer(low):
        for term in re.split(r"[,\s]+dan\s+|,|\band\b", match.group(1)):
            term = _LEADING_MARKER.sub("", term.strip()).strip()
            if 2 < len(term) < 25:
                excluded.append(term)
    # Filtered before use anywhere, so the rationale text never names a term
    # that isn't actually in exclude_terms — see _drop_cost_terms' docstring.
    excluded = _drop_cost_terms(excluded[:6])
    stage = stage_hint if stage_hint in SCHOOL_STAGES else None
    if not stage:
        for key, pattern in _STAGE_PATTERN:
            if re.search(pattern, low):
                stage = key
                break
    params: dict[str, Any] = {"exclude_terms": excluded,
                              "enforce_micronutrients": False}
    if stage:
        params["stage"] = stage
    stage_label = STAGE_LABELS.get(stage or "sd", STAGE_LABELS["sd"])
    if excluded:
        rationale = t(lang, "agent.gizi.rationale_rule_terms",
                      stage=stage_label, terms=", ".join(excluded[:6]))
    else:
        rationale = t(lang, "agent.gizi.rationale_rule_plain", stage=stage_label)
    if feedback_history:
        # The actual exclusion already happened deterministically in
        # generate() (feedback_history's dish ids merge into exclude_dish_ids)
        # — this is the visible acknowledgement that the pipeline used it.
        reasons = {r for e in feedback_history for r in e.get("reasons", [])}
        rationale += " " + t(lang, "agent.gizi.rationale_feedback",
                             count=len(feedback_history),
                             reasons=", ".join(sorted(reasons)) or "-")
    return AgentAdvice("agen_gizi", "rule-based", rationale, params)


# ---------------------------------------------------------------- Agen Biaya

_BIAYA_TOOL = [{
    "type": "function",
    "function": {
        "name": "biaya_advice",
        "description": ("Decide budget-allocation adjustments for an MBG menu "
                        "plan: how much of the per-portion pagu goes to raw "
                        "ingredients, and any regional price adjustment."),
        "parameters": {
            "type": "object",
            "properties": {
                "ingredient_budget_share": {
                    "type": "number", "minimum": 0.85, "maximum": 1.0,
                    "description": "Share of the per-portion budget available "
                                   "for raw ingredients. Default 1.0 — the "
                                   "operator's budget input is already scoped "
                                   "to food/ingredients, not an all-inclusive "
                                   "program pagu, so none of it is reserved "
                                   "for labour/operations by default. Go "
                                   "below 1.0 only if the notes explicitly "
                                   "call out a non-ingredient cost to cover "
                                   "from this same budget."},
                "regional_cost_index": {
                    "type": "number", "minimum": 0.7, "maximum": 1.6,
                    "description": "Multiplier for regional price differences; "
                                   "1.0 is the national baseline."},
                "rationale": {
                    "type": "string",
                    "description": "One or two sentences explaining the "
                                   "adjustments, in the requested language."},
            },
            "required": ["rationale"],
        },
    },
}]


def agent_biaya(client: SeaLionClient, *, budget_per_portion_idr: float,
                portions: int, province: str,
                price_table: Optional[PriceTable] = None,
                lang: str = DEFAULT_LANG) -> AgentAdvice:
    lang = norm_lang(lang)
    market_index = price_table.regional_index_for(province) if price_table else 1.0
    if client.configured:
        try:
            return _agent_biaya_llm(client, budget_per_portion_idr, portions,
                                    province, market_index, lang)
        except SeaLionError:
            pass
    return _agent_biaya_rule(budget_per_portion_idr, portions, province,
                             market_index, lang)


def _agent_biaya_llm(client: SeaLionClient, budget_per_portion_idr: float,
                     portions: int, province: str, market_index: float,
                     lang: str) -> AgentAdvice:
    system = ("You are Agen Biaya, the cost-constraints agent in Racik's MBG "
              "menu planner. You never compute rupiah totals yourself — you "
              "only translate the program's budget context into structured "
              "planning adjustments by calling biaya_advice. Reply in the "
              "language requested.")
    market_line = (
        f"Real market data (WFP retail price survey) puts this province's "
        f"regional cost index at {market_index:.3f}x the national average — "
        f"use this as your regional_cost_index unless the notes give a "
        f"specific reason to deviate."
        if market_index != 1.0 else
        "No province-specific market data is available; use 1.0 (national "
        "baseline) for regional_cost_index unless given a specific reason.")
    user = (f"Requested language: {lang}\n"
            f"Per-portion pagu already resolved: Rp{budget_per_portion_idr:,.0f}\n"
            f"Portions per day: {portions}\n"
            f"Province: {province or '(not specified)'}\n"
            f"{market_line}\n"
            "This budget is the operator's own food/ingredient budget for "
            "the plan, not an all-inclusive program pagu — it does not need "
            "a share held back for labour or operations. Use "
            "ingredient_budget_share=1.0 unless the notes explicitly say "
            "some of this same budget must also cover a non-ingredient cost.")
    reply: ChatResult = client.chat(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        tools=_BIAYA_TOOL, tool_choice="auto")
    if not reply.wants_tools:
        raise SeaLionError("agent_biaya: model returned no tool call")
    args = reply.tool_calls[0].arguments
    params = {
        "ingredient_budget_share": _clamp(
            float(args.get("ingredient_budget_share", 1.0)), 0.85, 1.0),
        "regional_cost_index": _clamp(
            float(args.get("regional_cost_index", market_index)), 0.7, 1.6),
    }
    rationale = str(args.get("rationale") or "").strip() or t(lang, "agent.biaya.rationale_default")
    return AgentAdvice("agen_biaya", "llm", rationale, params)


def _agent_biaya_rule(budget_per_portion_idr: float, portions: int,
                      province: str, market_index: float,
                      lang: str) -> AgentAdvice:
    # The operator's budget input is the food/ingredient budget itself, not
    # an all-inclusive program pagu — nothing held back for labour/ops by
    # default (see MBGConfig.ingredient_budget_share's docstring). The
    # regional index, though, comes straight from real WFP retail price data
    # when the province has coverage.
    params = {"ingredient_budget_share": 1.0, "regional_cost_index": market_index}
    key = ("agent.biaya.rationale_rule_market" if market_index != 1.0
          else "agent.biaya.rationale_rule")
    rationale = t(lang, key, budget=f"{budget_per_portion_idr:,.0f}",
                 province=province or "-", index=f"{market_index:.2f}")
    return AgentAdvice("agen_biaya", "rule-based", rationale, params)


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


# ---------------------------------------------------------------- Validator


def validate_plan(result: PlanResult, lang: str = DEFAULT_LANG) -> ValidatorReport:
    """Deterministic recheck of a solved plan against AKG targets and budget.

    No LLM involved — every field here is re-derived straight from
    `PlanResult`, the same object `/api/plan` serialises, so this can never
    tell the operator a different story than the rest of the app.
    """
    lang = norm_lang(lang)
    per_day = []
    for day in result.days:
        adequacy = result.adequacy(day)
        within_budget = day.cost_per_portion_idr <= (
            result.request.config.ingredient_budget_idr * 1.001)  # float slack
        per_day.append({
            "day": day.day_index + 1,
            "adequacy": round(adequacy, 3),
            "cost_per_portion_idr": round(day.cost_per_portion_idr),
            "within_budget": within_budget,
            "meets_akg": adequacy >= 0.999,
        })
    passed = (not result.relaxations) and result.budget_overrun_idr <= 0
    if passed:
        rationale = t(lang, "validator.pass", days=len(result.days))
    elif result.relaxations:
        worst = result.relaxations[0]
        rationale = t(lang, "validator.fail_nutrition",
                      count=len(result.relaxations),
                      nutrient=worst.nutrient, day=worst.day_index + 1,
                      share=f"{worst.share * 100:.0f}%")
    else:
        rationale = t(lang, "validator.fail_budget",
                      amount=f"{result.budget_overrun_idr:,.0f}")
    return ValidatorReport(passed=passed, rationale=rationale, per_day=per_day)
