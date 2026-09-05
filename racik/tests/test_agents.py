"""Tests for Agen Gizi, Agen Biaya, and the Validator (agents.py).

The rule-based fallback path is exercised with an unconfigured SeaLionClient
so these tests need no network and no API key. A scripted stand-in client
(same pattern as test_orchestrator.py's ScriptedSeaLion) exercises the LLM
path's tool-call parsing without a live model.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from racik.agents import agent_biaya, agent_gizi, validate_plan
from racik.optimizer import MenuOptimizer, PlanRequest, Relaxation
from racik.sealion import ChatResult, SeaLionClient, SeaLionError, ToolCall
from racik.store import RacikStore


@pytest.fixture(scope="module")
def store():
    return RacikStore()


@pytest.fixture(scope="module")
def optimizer(store):
    return MenuOptimizer(store.load_dishes())


class _Unconfigured(SeaLionClient):
    """The default state: no key, no base_url override -> not configured."""

    def __init__(self):
        super().__init__(api_key="")


class _Scripted(SeaLionClient):
    def __init__(self, reply: ChatResult):
        super().__init__(api_key="test-key", min_interval=0.0)
        self.reply = reply

    @property
    def configured(self) -> bool:
        return True

    def chat(self, messages, tools=None, **kwargs) -> ChatResult:
        return self.reply


class _Broken(SeaLionClient):
    """Configured, but every call fails — the pipeline must not blow up."""

    def __init__(self):
        super().__init__(api_key="test-key", min_interval=0.0)

    @property
    def configured(self) -> bool:
        return True

    def chat(self, messages, tools=None, **kwargs) -> ChatResult:
        raise SeaLionError("simulated network failure")


# ---------------------------------------------------------------- Agen Gizi

def test_gizi_rule_based_extracts_exclusions_without_marker_words():
    advice = agent_gizi(_Unconfigured(), stage_hint=None,
                        notes_text="tanpa udang, alergi kacang", lang="id")
    assert advice.source == "rule-based"
    assert advice.params["exclude_terms"] == ["udang", "kacang"]
    assert advice.params["enforce_micronutrients"] is False


def test_gizi_rule_based_infers_stage_from_notes():
    advice = agent_gizi(_Unconfigured(), stage_hint=None,
                        notes_text="untuk siswa SMP", lang="id")
    assert advice.params["stage"] == "smp"


def test_gizi_explicit_stage_hint_wins_over_notes():
    advice = agent_gizi(_Unconfigured(), stage_hint="sma",
                        notes_text="untuk SD", lang="id")
    assert advice.params["stage"] == "sma"


def test_gizi_llm_path_parses_tool_call():
    reply = ChatResult(content="", tool_calls=[ToolCall(
        id="c1", name="gizi_advice",
        arguments={"exclude_terms": ["telur"], "enforce_micronutrients": True,
                   "rationale": "Operator noted an egg allergy."})])
    advice = agent_gizi(_Scripted(reply), stage_hint="sd", notes_text="", lang="en")
    assert advice.source == "llm"
    assert advice.params["exclude_terms"] == ["telur"]
    assert advice.params["enforce_micronutrients"] is True
    assert "egg" in advice.rationale.lower()


def test_gizi_falls_back_when_llm_errors():
    advice = agent_gizi(_Broken(), stage_hint="sd", notes_text="tanpa udang",
                        lang="id")
    assert advice.source == "rule-based"
    assert "udang" in advice.params["exclude_terms"]


# ---------------------------------------------------------------- Agen Biaya

def test_biaya_rule_based_uses_full_operator_budget():
    advice = agent_biaya(_Unconfigured(), budget_per_portion_idr=10_000,
                         portions=100, province="Jawa Tengah", lang="id")
    assert advice.source == "rule-based"
    # The operator's budget input is Racik's own food/ingredient budget, not
    # an all-inclusive program pagu, so none of it is held back by default —
    # see MBGConfig.ingredient_budget_share's docstring.
    assert advice.params["ingredient_budget_share"] == pytest.approx(1.0)
    assert advice.params["regional_cost_index"] == pytest.approx(1.0)


def test_biaya_llm_path_clamps_out_of_range_values():
    reply = ChatResult(content="", tool_calls=[ToolCall(
        id="c1", name="biaya_advice",
        arguments={"ingredient_budget_share": 1.5,   # out of range, must clamp
                   "regional_cost_index": 0.1,        # out of range, must clamp
                   "rationale": "Testing clamping."})])
    advice = agent_biaya(_Scripted(reply), budget_per_portion_idr=8000,
                         portions=50, province="Bali", lang="en")
    assert advice.source == "llm"
    assert advice.params["ingredient_budget_share"] <= 1.0
    assert advice.params["regional_cost_index"] >= 0.7


def test_biaya_falls_back_when_llm_errors():
    advice = agent_biaya(_Broken(), budget_per_portion_idr=10_000, portions=100,
                         province="", lang="id")
    assert advice.source == "rule-based"


# ---------------------------------------------------------------- Validator

def test_validator_passes_a_clean_plan(optimizer):
    result = optimizer.solve(PlanRequest(
        days=1, stage="sd", portions=50, province="Jawa Tengah",
        max_solve_seconds=10))
    report = validate_plan(result, lang="en")
    if not result.relaxations and result.budget_overrun_idr <= 0:
        assert report.passed is True
        assert "validated" in report.rationale.lower()
    assert len(report.per_day) == len(result.days)
    assert set(report.per_day[0]) == {"day", "adequacy", "cost_per_portion_idr",
                                      "within_budget", "meets_akg"}


def test_validator_fails_and_explains_a_relaxed_plan(optimizer, monkeypatch):
    """Force a razor-thin budget so the solver has to relax a nutrient floor,
    then check the Validator names it rather than papering over it."""
    result = optimizer.solve(PlanRequest(
        days=1, stage="sma", portions=50, province="",
        config=__import__("racik.config", fromlist=["MBGConfig"]).MBGConfig(
            budget_per_portion_idr=2000.0, ingredient_budget_share=1.0),
        max_solve_seconds=10))
    assert result.feasible
    report = validate_plan(result, lang="id")
    assert report.passed is False
    assert report.per_day[0]["meets_akg"] is False
