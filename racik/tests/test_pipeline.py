"""End-to-end tests for the /api/generate pipeline and the operator-review /
menu-history / stock-audit endpoints it feeds.

Uses the real CP-SAT solver against the built racik.db (like the rest of the
suite) with small days/portions to keep solves fast, and a temp-file
StateStore per test module so runs never leak between test files or into the
real racik_state.db.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """A fresh app per test: swaps the module-level StateStore singleton for
    one backed by a temp file, so runs from different tests never collide."""
    from fastapi.testclient import TestClient
    import racik.api as api_module
    from racik.statedb import StateStore

    fresh_state = StateStore(path=tmp_path / "state.db")
    api_module._state.cache_clear()
    monkeypatch.setattr(api_module, "_state", lambda: fresh_state)
    return TestClient(api_module.app)


# ---------------------------------------------------------------- /api/generate

def test_generate_runs_every_pipeline_stage_in_order(client):
    body = client.post("/api/generate", json={
        "days": 1, "portions": 30, "province": "Jawa Tengah",
        "budget_mode": "total", "budget_value": 300_000,
        "notes": "tanpa udang", "lang": "id"}).json()

    assert body["run_id"] > 0
    stages = [t["agent"] for t in body["trace"]]
    assert stages == ["preprocessing", "agen_gizi", "agen_biaya", "cp_sat", "validator"]
    assert body["resolved"]["exclude_terms"] == ["udang"]
    # 300,000 total / (30 portions x 1 day) = 10,000/portion
    assert body["resolved"]["budget_per_portion_idr"] == pytest.approx(10_000.0)
    assert len(body["plan"]["days"]) == 1
    assert body["plan"]["run_id"] == body["run_id"]


def test_generate_embeds_procurement_matching_the_solved_plan(client):
    body = client.post("/api/generate", json={
        "days": 2, "portions": 25, "budget_mode": "per_portion",
        "budget_value": 10_000}).json()
    proc = body["plan"]["procurement"]
    assert proc["totals"]["items"] > 0
    assert len(proc["by_day"]) == 2
    # the whole-run total must be at least as large as any single day's total —
    # a cheap sanity check that by_day is really a subset, not duplicated data
    day_costs = [d["totals"]["total_cost_idr"] for d in proc["by_day"]]
    assert proc["totals"]["total_cost_idr"] >= max(day_costs)


def test_generate_per_portion_mode_uses_the_value_directly(client):
    body = client.post("/api/generate", json={
        "days": 2, "portions": 40, "budget_mode": "per_portion",
        "budget_value": 8500, "lang": "en"}).json()
    assert body["resolved"]["budget_per_portion_idr"] == pytest.approx(8500.0)


def test_generate_explains_how_compliance_is_determined(client):
    body = client.post("/api/generate", json={
        "days": 1, "portions": 20, "budget_mode": "per_portion",
        "budget_value": 10_000, "lang": "en"}).json()
    methodology = body["plan"]["compliance_methodology"]
    assert "AKG" in methodology and "1/3" in methodology
    assert "binding" in methodology.lower() or "macro" in methodology.lower()


def test_generate_rejects_an_unknown_budget_mode(client):
    res = client.post("/api/generate", json={
        "days": 1, "portions": 10, "budget_mode": "bogus", "budget_value": 10_000})
    assert res.status_code == 400


def test_generate_returns_422_when_no_candidate_survives_the_budget(client):
    """A near-zero budget should empty the candidate pool for every slot."""
    res = client.post("/api/generate", json={
        "days": 1, "portions": 10, "budget_mode": "per_portion",
        "budget_value": 1, "lang": "en"})
    assert res.status_code == 422


# ---------------------------------------------------------------- /api/review

def test_review_persists_and_replace_solves_a_new_day(client):
    from racik.config import COMPONENT_ONLY_SLOTS

    gen = client.post("/api/generate", json={
        "days": 2, "portions": 30, "province": "Jawa Tengah",
        "budget_mode": "per_portion", "budget_value": 10_000, "lang": "id"}).json()
    run_id = gen["run_id"]
    original = {slot: (v["dish_id"] if v else None)
               for slot, v in gen["plan"]["days"][0]["slots"].items()}

    res = client.post("/api/review", json={
        "run_id": run_id, "day_index": 0, "decision": "reject",
        "reason": "Too expensive", "replace": True, "lang": "id"}).json()

    assert res["ok"] is True
    replacement = res["replacement_day"]
    assert replacement["index"] == 0
    # staple/buah are served components (rice/fruit), not corpus recipes, and
    # are deliberately outside the served-log rotation mechanism — same
    # distinction optimizer.py's COMPONENT_ONLY_SLOTS already draws.
    for slot, dish_id in original.items():
        if slot in COMPONENT_ONLY_SLOTS:
            continue
        new_dish = replacement["slots"].get(slot)
        if dish_id is not None and new_dish is not None:
            assert new_dish["dish_id"] != dish_id, (
                f"slot {slot} repeated the rejected dish instead of replacing it")


def test_review_without_replace_just_records_the_decision(client):
    gen = client.post("/api/generate", json={
        "days": 1, "portions": 20, "budget_mode": "per_portion",
        "budget_value": 10_000}).json()
    res = client.post("/api/review", json={
        "run_id": gen["run_id"], "day_index": 0, "decision": "accept"}).json()
    assert res == {"ok": True}


def test_review_rejects_an_invalid_decision(client):
    gen = client.post("/api/generate", json={
        "days": 1, "portions": 20, "budget_mode": "per_portion",
        "budget_value": 10_000}).json()
    res = client.post("/api/review", json={
        "run_id": gen["run_id"], "day_index": 0, "decision": "maybe"})
    assert res.status_code == 400


# ---------------------------------------------------------------- /api/history

def test_history_surfaces_rejection_reasons(client):
    gen = client.post("/api/generate", json={
        "days": 1, "portions": 20, "budget_mode": "per_portion",
        "budget_value": 10_000}).json()
    client.post("/api/review", json={
        "run_id": gen["run_id"], "day_index": 0, "decision": "reject",
        "reason": "Kids won't like it"})

    history = client.get("/api/history").json()
    assert any(r["reason"] == "Kids won't like it" for r in history["rejections"])
    assert history["recent_served"]           # the generate() call logged dishes


# ---------------------------------------------------------------- /api/stock

def test_stock_round_trips_and_merges_partial_updates(client):
    gen = client.post("/api/generate", json={
        "days": 1, "portions": 20, "budget_mode": "per_portion",
        "budget_value": 10_000}).json()
    run_id = gen["run_id"]

    client.post("/api/stock", json={
        "run_id": run_id, "day_index": 0, "ingredient_name": "Beras",
        "received_pct": 100, "used_pct": 90, "actual_cost_idr": 50_000})
    client.post("/api/stock", json={
        "run_id": run_id, "day_index": 0, "ingredient_name": "Beras",
        "used_pct": 85})       # partial update — received/actual_cost preserved

    records = client.get("/api/stock", params={"run_id": run_id}).json()["records"]
    assert len(records) == 1
    assert records[0]["received_pct"] == 100
    assert records[0]["used_pct"] == 85
    assert records[0]["actual_cost_idr"] == 50_000


# ---------------------------------------------------------------- reject -> learn -> regenerate

def test_rejected_dish_never_resurfaces_in_a_later_full_regenerate(client):
    """The tick/cross feedback loop: a cross with a reason is "feedback for
    the AI model to learn from" — the rejected dish must not just be swapped
    for that one day (replace_day already covers that) but excluded from
    every later /api/generate call too, and Agen Gizi's rationale must show
    it actually used the feedback."""
    params = {"days": 1, "portions": 25, "province": "Jawa Tengah",
             "budget_mode": "per_portion", "budget_value": 10_000, "lang": "en"}
    first = client.post("/api/generate", json=params).json()
    hewani = first["plan"]["days"][0]["slots"]["hewani"]
    assert hewani is not None
    rejected_dish_id = hewani["dish_id"]

    client.post("/api/review", json={
        "run_id": first["run_id"], "day_index": 0, "decision": "reject",
        "reason": "Kids won't like it"})

    second = client.post("/api/generate", json=params).json()
    gizi_trace = next(t for t in second["trace"] if t["agent"] == "agen_gizi")
    assert "1" in gizi_trace["rationale"] or "previously-rejected" in gizi_trace["rationale"]

    new_hewani = second["plan"]["days"][0]["slots"]["hewani"]
    if new_hewani is not None:
        assert new_hewani["dish_id"] != rejected_dish_id


def test_menu_history_cannot_make_a_plan_permanently_infeasible(tmp_path, monkeypatch):
    """If the rotation window's decay exclusion alone would empty the
    candidate pool, generate() must retry without it rather than fail —
    rotation is a preference, never a reason a kitchen gets no menu at all.

    Forces the scenario deterministically: monkeypatch recent_served_dish_ids
    to return "every dish in the corpus" for exactly one call (the first
    solve attempt), which the real menu-history mechanism could only produce
    after an enormous number of regenerations — here it's simulated directly
    so the test doesn't depend on however many candidates a given
    province/budget happens to have.
    """
    from racik.orchestrator import RacikOrchestrator
    from racik.optimizer import MenuOptimizer
    from racik.statedb import StateStore
    from racik.store import RacikStore
    from racik.bedrock import make_client

    store = RacikStore()
    optimizer = MenuOptimizer(store.load_dishes())
    orch = RacikOrchestrator(store, optimizer, make_client())
    state = StateStore(path=tmp_path / "state.db")

    all_dish_ids = {d.dish_id for d in optimizer.dishes}
    calls = {"n": 0}
    real_method = StateStore.recent_served_dish_ids

    def poisoned_once(self, within_days=14):
        calls["n"] += 1
        return all_dish_ids if calls["n"] == 1 else real_method(self, within_days)
    monkeypatch.setattr(StateStore, "recent_served_dish_ids", poisoned_once)

    result = orch.generate(
        days=1, portions=25, province="Jawa Tengah", stage=None,
        budget_mode="per_portion", budget_value=8000, notes_text="",
        lang="en", history_days=14, state=state)

    assert result.plan["days"]      # a real menu came back, not an exception
    cp_sat_trace = next(t for t in result.trace if t["agent"] == "cp_sat")
    assert "Retried without" in cp_sat_trace["rationale"]
    assert calls["n"] == 1   # the retry path drops recent_ids rather than re-querying it


# ---------------------------------------------------------------- menu history / decay

def test_repeated_generate_avoids_recently_served_recipe_dishes(client):
    from racik.config import COMPONENT_ONLY_SLOTS

    params = {"days": 1, "portions": 30, "province": "Jawa Tengah",
             "budget_mode": "per_portion", "budget_value": 10_000, "lang": "id"}
    first = client.post("/api/generate", json=params).json()
    second = client.post("/api/generate", json=params).json()

    def recipe_dish_ids(payload):
        slots = payload["plan"]["days"][0]["slots"]
        return {v["dish_id"] for slot, v in slots.items()
               if v and slot not in COMPONENT_ONLY_SLOTS}

    overlap = recipe_dish_ids(first) & recipe_dish_ids(second)
    assert not overlap, f"dishes repeated despite menu history: {overlap}"
