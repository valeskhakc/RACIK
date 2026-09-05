"""Tests for candidate-filter transparency, dish translation, and Bedrock.

These three share a theme: each is a place where the system could plausibly
hide something from the reader — an invisible filter, a translation passed off
as authoritative, or a model provider that silently is not what you think.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from racik.optimizer import (COST_CEILING_SHARE, PREFERENCE_EXACT_PROVINCE,
                             RANK_COST_FLOOR, VALUE_CAP, W_RANK_PREFERENCE,
                             W_RANK_QUALITY, MenuOptimizer, PlanRequest,
                             rank_breakdown)
from racik.store import RacikStore


@pytest.fixture(scope="module")
def store():
    return RacikStore()


@pytest.fixture(scope="module")
def optimizer(store):
    return MenuOptimizer(store.load_dishes())


# ---------------------------------------------------------------- the rubric

def test_explain_candidates_names_every_gate(optimizer):
    report = optimizer.explain_candidates(PlanRequest(stage="sd"))
    ids = [g["id"] for g in report["rubric"]["hard_gates"]]
    assert ids == ["G1", "G2", "G3", "G4", "G5"]
    for gate in report["rubric"]["hard_gates"]:
        assert gate["name"] and gate["rule"]


def test_rubric_publishes_the_actual_weights(optimizer):
    weights = optimizer.explain_candidates(
        PlanRequest(stage="sd"))["rubric"]["score"]["weights"]
    assert weights["W_PREFERENCE"] == W_RANK_PREFERENCE
    assert weights["W_QUALITY"] == W_RANK_QUALITY
    assert weights["VALUE_CAP"] == VALUE_CAP
    assert weights["COST_FLOOR"] == RANK_COST_FLOOR


def test_funnel_arithmetic_balances(optimizer):
    """Everything removed plus everything kept must equal what went in."""
    report = optimizer.explain_candidates(PlanRequest(stage="sd",
                                                      exclude_terms=["udang"]))
    for slot in report["funnel"]:
        removed = slot["removed"]
        gates = (removed["G2_source"] + removed["G3_exclusions"]
                 + removed["G4_dish_id"] + removed["G5_cost_ceiling"])
        assert slot["in_slot"] - gates == slot["eligible"], slot["slot"]
        assert slot["selected"] <= slot["eligible"] - removed["duplicate_name"]


def test_cost_ceiling_is_actually_applied(optimizer):
    request = PlanRequest(stage="sd")
    report = optimizer.explain_candidates(request)
    ceiling = request.config.ingredient_budget_idr * COST_CEILING_SHARE
    for slot in report["funnel"]:
        assert slot["cost_ceiling_idr"] == round(ceiling)
        for dish in slot["top"]:
            assert dish["cost_per_portion_idr"] <= ceiling


def test_exclusions_show_up_in_the_funnel(optimizer):
    clean = optimizer.explain_candidates(PlanRequest(stage="sd"))
    excluded = optimizer.explain_candidates(
        PlanRequest(stage="sd", exclude_terms=["telur"]))
    removed = sum(s["removed"]["G3_exclusions"] for s in excluded["funnel"])
    assert removed > 0
    assert sum(s["removed"]["G3_exclusions"] for s in clean["funnel"]) == 0


def test_binding_gate_identifies_the_biggest_cut(optimizer):
    report = optimizer.explain_candidates(PlanRequest(stage="sd"))
    by_slot = {s["slot"]: s for s in report["funnel"]}
    # Component-only slots are dominated by the source gate, by construction.
    assert by_slot["staple"]["binding_gate"].startswith("source")
    assert by_slot["buah"]["binding_gate"].startswith("source")


def test_rank_breakdown_sums_to_its_total(store, optimizer):
    request = PlanRequest(stage="sd", province="Jawa Tengah", island="Jawa")
    targets = {"protein_g": 16.1, "energy_kcal": 616.7}
    budget = request.config.ingredient_budget_idr
    dish = optimizer.by_slot["hewani"][0]
    b = rank_breakdown(dish, request, targets, budget)
    assert b.total == pytest.approx(
        b.efficiency + W_RANK_PREFERENCE * b.preference + W_RANK_QUALITY * b.quality)
    assert b.protein_value <= VALUE_CAP and b.energy_value <= VALUE_CAP


def test_regional_preference_ranks_the_home_province_highest(optimizer):
    request = PlanRequest(stage="sd", province="Jawa Tengah", island="Jawa")
    targets = {"protein_g": 16.1, "energy_kcal": 616.7}
    budget = request.config.ingredient_budget_idr
    local = next(d for d in optimizer.by_slot["hewani"]
                 if d.province == "Jawa Tengah")
    assert rank_breakdown(local, request, targets, budget).preference == \
        PREFERENCE_EXACT_PROVINCE


# ---------------------------------------------------------------- translation

def test_rule_gloss_reads_as_english():
    from racik.translate import rule_gloss
    assert rule_gloss("Tempe Mendoan").lower() == "battered fried tempeh"
    assert "egg" in rule_gloss("Telur Bacem").lower()
    assert "rice" in rule_gloss("Nasi putih (200 g)").lower()


def test_rule_gloss_strips_portions_and_chatter():
    from racik.translate import rule_gloss
    gloss = rule_gloss("Ayam Goreng Kremes simple anti gagal (150 g)")
    assert "150" not in gloss and "simple" not in gloss.lower()
    assert "chicken" in gloss.lower()


def test_rule_gloss_drops_generic_when_specific_present():
    """'Milkfish soy stew', never 'Milkfish soy stew fish'."""
    from racik.translate import rule_gloss
    words = rule_gloss("Semur Ikan Bandeng").lower().split()
    assert "milkfish" in words and "fish" not in words


def test_translator_reports_its_source(tmp_path):
    from racik.sealion import SeaLionClient
    from racik.translate import DishTranslator
    translator = DishTranslator(SeaLionClient(api_key=""),
                                cache_path=tmp_path / "cache.json")
    glosses = translator.translate(["Tempe Mendoan"])
    assert glosses[0].method == "rule"          # never claimed as a translation
    assert glosses[0].name == "Tempe Mendoan"   # the name survives intact


def test_translator_uses_and_persists_the_cache(tmp_path):
    from racik.sealion import ChatResult, SeaLionClient
    from racik.translate import DishTranslator

    class Scripted(SeaLionClient):
        def __init__(self):
            super().__init__(api_key="k", min_interval=0.0)
            self.calls_made = 0

        @property
        def configured(self):
            return True

        def chat(self, messages, tools=None, **kw):
            self.calls_made += 1
            return ChatResult(content=json.dumps(
                {"Tempe Mendoan": "Battered fried tempeh"}))

    cache = tmp_path / "cache.json"
    client = Scripted()
    first = DishTranslator(client, cache_path=cache).translate(["Tempe Mendoan"])
    assert first[0].english == "Battered fried tempeh"
    assert client.calls_made == 1
    assert cache.exists()

    # a fresh translator reads the cache and makes no further call
    second = DishTranslator(client, cache_path=cache).translate(["Tempe Mendoan"])
    assert second[0].english == "Battered fried tempeh"
    assert client.calls_made == 1


def test_translator_ignores_invented_keys(tmp_path):
    """A model returning names we never asked about must not pollute the cache."""
    from racik.sealion import ChatResult, SeaLionClient
    from racik.translate import DishTranslator

    class Inventive(SeaLionClient):
        @property
        def configured(self):
            return True

        def chat(self, messages, tools=None, **kw):
            return ChatResult(content=json.dumps(
                {"Tempe Mendoan": "Battered fried tempeh",
                 "Something Never Asked For": "nonsense"}))

    translator = DishTranslator(Inventive(api_key="k", min_interval=0.0),
                                cache_path=tmp_path / "c.json")
    translator.translate(["Tempe Mendoan"])
    assert "Something Never Asked For" not in translator.cache


def test_translator_survives_unusable_model_output(tmp_path):
    from racik.sealion import ChatResult, SeaLionClient
    from racik.translate import DishTranslator

    class Rambling(SeaLionClient):
        @property
        def configured(self):
            return True

        def chat(self, messages, tools=None, **kw):
            return ChatResult(content="Sure! Here are your translations:")

    translator = DishTranslator(Rambling(api_key="k", min_interval=0.0),
                                cache_path=tmp_path / "c.json")
    glosses = translator.translate(["Tempe Mendoan"])
    assert glosses[0].method == "rule"          # fell back, did not crash
    assert translator.last_error


def test_translator_reads_a_fenced_json_reply(tmp_path):
    from racik.sealion import ChatResult, SeaLionClient
    from racik.translate import DishTranslator

    class Fenced(SeaLionClient):
        @property
        def configured(self):
            return True

        def chat(self, messages, tools=None, **kw):
            return ChatResult(content='```json\n{"Telur Bacem": "Sweet soy egg"}\n```')

    translator = DishTranslator(Fenced(api_key="k", min_interval=0.0),
                                cache_path=tmp_path / "c.json")
    assert translator.translate(["Telur Bacem"])[0].english == "Sweet soy egg"


# ---------------------------------------------------------------- bedrock

def test_bedrock_client_is_unconfigured_without_an_arn():
    from racik.bedrock import BedrockSeaLionClient
    assert BedrockSeaLionClient(model_arn="").configured is False


def test_bedrock_reports_region_support():
    from racik.bedrock import SUPPORTED_REGIONS, BedrockSeaLionClient
    ok = BedrockSeaLionClient(model_arn="arn:x", region="us-east-1").describe()
    bad = BedrockSeaLionClient(model_arn="arn:x", region="ap-southeast-1").describe()
    assert ok["region_supported"] is True
    # Singapore is not a Custom Model Import region — the deployment doc says so
    assert bad["region_supported"] is False
    assert "ap-southeast-1" not in SUPPORTED_REGIONS


def test_bedrock_parses_openai_chat_completion_shape():
    from racik.bedrock import _to_chat_result
    result = _to_chat_result({
        "choices": [{"message": {"role": "assistant", "content": "hello"}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2}})
    assert result.content == "hello"
    assert result.usage["prompt_tokens"] == 5


def test_bedrock_parses_tool_calls():
    from racik.bedrock import _to_chat_result
    result = _to_chat_result({"choices": [{"message": {
        "content": None,
        "tool_calls": [{"id": "c1", "type": "function", "function": {
            "name": "plan_menu", "arguments": '{"stage":"sd"}'}}]}}]})
    assert result.wants_tools
    assert result.tool_calls[0].arguments == {"stage": "sd"}


def test_bedrock_parses_legacy_completion_shape():
    """Models imported before Nov 2025 return the BedrockCompletion shape."""
    from racik.bedrock import _to_chat_result
    result = _to_chat_result({"generation": " a rainbow ",
                              "prompt_token_count": 7,
                              "generation_token_count": 3})
    assert result.content == "a rainbow"
    assert result.usage["completion_tokens"] == 3


def test_bedrock_rejects_an_unknown_response_shape():
    from racik.bedrock import _to_chat_result
    from racik.sealion import SeaLionError
    with pytest.raises(SeaLionError):
        _to_chat_result({"unexpected": True})


def test_bedrock_verify_reports_rather_than_raises():
    from racik.bedrock import BedrockSeaLionClient
    report = BedrockSeaLionClient(model_arn="").verify_deployment()
    assert report["errors"] and report["chat"] is None


@pytest.mark.parametrize("env,expected", [
    ({}, "SEA-LION (AI Singapore)"),
    ({"BEDROCK_MODEL_ARN": "arn:aws:bedrock:us-east-1:1:imported-model/x"},
     "SEA-LION on Amazon Bedrock (Custom Model Import)"),
    ({"SEALION_BASE_URL": "http://localhost:8000/api/v1"},
     "Self-hosted OpenAI-compatible server (http://localhost:8000/api/v1)"),
    ({"BEDROCK_MODEL_ID": "anthropic.claude-haiku-4-5-v1:0"},
     "AWS Bedrock (Converse API)"),
    ({"RACIK_LLM_PROVIDER": "bedrock-sealion-import"},
     "SEA-LION on Amazon Bedrock (Custom Model Import)"),
    ({"ANTHROPIC_API_KEY": "sk-ant-test"}, "Anthropic (direct API)"),
    ({"OPENAI_API_KEY": "sk-proj-test"}, "OpenAI (direct API)"),
])
def test_provider_factory_selects_by_environment(monkeypatch, env, expected):
    for key in ("RACIK_LLM_PROVIDER", "BEDROCK_MODEL_ARN", "BEDROCK_MODEL_ID",
               "SEALION_BASE_URL", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    from racik.bedrock import make_client
    assert make_client().describe()["provider"] == expected


# ---------------------------------------------------------------- API

@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from racik.api import app
    return TestClient(app)


def test_candidates_endpoint(client):
    body = client.post("/api/candidates",
                       json={"stage": "sd", "province": "Jawa Tengah"}).json()
    assert len(body["funnel"]) == 5
    assert body["rubric"]["hard_gates"]


def test_translate_endpoint_keeps_the_original_name(client):
    body = client.post("/api/translate",
                       json={"names": ["Tempe Mendoan"], "use_model": False}).json()
    gloss = body["glosses"][0]
    assert gloss["name"] == "Tempe Mendoan"
    assert gloss["english"] != gloss["name"]
    assert gloss["method"] in {"rule", "cache", "sealion"}


def test_translate_endpoint_rejects_an_empty_request(client):
    assert client.post("/api/translate", json={"names": []}).status_code == 422


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


# ---------------------------------------------------------------- report

def test_report_renders_in_both_languages(client):
    for lang, marker in (("id", "Laporan Kepatuhan"),
                         ("en", "Compliance &amp; Procurement")):
        res = client.post("/api/report", json={
            "days": 2, "stage": "sd", "portions": 100, "lang": lang,
            "max_solve_seconds": 8})
        assert res.status_code == 200
        assert marker in res.text
        assert res.text.startswith("<!DOCTYPE html>")
        assert "@media print" in res.text          # must print to PDF


def test_report_marks_advisory_nutrients(client):
    """A compliance document must not present an unverified value as enforced."""
    res = client.post("/api/report", json={"days": 1, "stage": "sd",
                                           "max_solve_seconds": 8})
    assert 'class="tag"' in res.text
    assert "INFO" in res.text


def test_report_discloses_relaxations(client):
    """A starved budget must produce a report that says so."""
    res = client.post("/api/report", json={
        "days": 2, "stage": "sma", "budget_per_portion_idr": 3000,
        "ingredient_budget_share": 0.7, "max_solve_seconds": 8, "lang": "en"})
    assert res.status_code == 200
    assert "Relaxed floors" in res.text
    assert 'class="warn"' in res.text          # the shortfall is flagged, not buried


def test_report_carries_procurement_and_signatures(client):
    res = client.post("/api/report", json={
        "days": 2, "stage": "sd", "portions": 250, "lang": "id",
        "sppg_name": "SPPG Uji", "max_solve_seconds": 8})
    assert "SPPG Uji" in res.text
    assert "Kebutuhan bahan" in res.text
    assert "Disusun oleh" in res.text and "Disetujui oleh" in res.text
    assert " kg" in res.text                   # gross weights present


def test_report_escapes_dish_names(client):
    """Dish names come from a public corpus; they must never inject markup."""
    from racik.report import _e
    assert _e('<script>alert(1)</script>') == \
        "&lt;script&gt;alert(1)&lt;/script&gt;"


def test_report_notes_match_the_api_notes(client):
    """The report and the API must not tell two stories about the same plan."""
    from racik.serialize import serialise_plan
    from racik.report import plan_notes
    from racik.optimizer import MenuOptimizer, PlanRequest
    from racik.store import RacikStore
    result = MenuOptimizer(RacikStore().load_dishes()).solve(
        PlanRequest(days=1, stage="sd", max_solve_seconds=8))
    assert serialise_plan(result, "id")["notes"] == plan_notes(result, "id")
