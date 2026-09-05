"""Tests for the operator-state persistence layer (statedb.py).

Everything here uses a temp-file StateStore, never the real
racik_state.db, and never touches racik.db (the read-only ETL artefact).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from racik.statedb import StateStore


@pytest.fixture()
def state(tmp_path):
    return StateStore(path=tmp_path / "state.db")


def test_second_file_is_created_on_first_use(tmp_path):
    path = tmp_path / "nested" / "state.db"
    assert not path.exists()
    StateStore(path=path)
    assert path.exists()


def test_run_round_trips(state):
    run_id = state.record_run(
        stage="sd", days=3, portions=100, province="Jawa Tengah",
        resolved={"stage": "sd", "budget_per_portion_idr": 7000.0},
        trace=[{"agent": "agen_gizi", "rationale": "test"}])
    run = state.run(run_id)
    assert run["stage"] == "sd"
    assert run["days"] == 3
    assert run["resolved"]["budget_per_portion_idr"] == 7000.0
    assert run["trace"][0]["agent"] == "agen_gizi"


def test_unknown_run_returns_none(state):
    assert state.run(999) is None


def test_served_log_feeds_recent_dish_ids(state):
    run_id = state.record_run(stage="sd", days=1, portions=10, province="",
                              resolved={}, trace=[])
    state.record_served(run_id, [(0, "hewani", 101, "Ayam Goreng"),
                                 (0, "nabati", 202, "Tempe Bacem")])
    recent = state.recent_served_dish_ids(within_days=14)
    assert recent == {101, 202}


def test_served_log_decays_outside_the_window(state, monkeypatch):
    import racik.statedb as statedb_mod
    from datetime import datetime, timedelta, timezone

    run_id = state.record_run(stage="sd", days=1, portions=10, province="",
                              resolved={}, trace=[])
    old_timestamp = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(
        timespec="seconds")
    state.conn.execute(
        "INSERT INTO served_log (run_id, day_index, slot, dish_id, dish_name, served_at) "
        "VALUES (?, 0, 'hewani', 555, 'Old Dish', ?)", (run_id, old_timestamp))
    state.conn.commit()
    assert 555 not in state.recent_served_dish_ids(within_days=14)
    assert 555 in state.recent_served_dish_ids(within_days=60)


def test_review_records_and_reads_back(state):
    run_id = state.record_run(stage="sd", days=1, portions=10, province="",
                              resolved={}, trace=[])
    state.record_review(run_id, 0, "accept")
    state.record_review(run_id, 1, "reject", reason="Too expensive")
    reviews = state.reviews_for_run(run_id)
    assert reviews[0]["decision"] == "accept"
    assert reviews[1]["decision"] == "reject"
    assert reviews[1]["reason"] == "Too expensive"


def test_review_rejects_bad_decision(state):
    run_id = state.record_run(stage="sd", days=1, portions=10, province="",
                              resolved={}, trace=[])
    with pytest.raises(ValueError):
        state.record_review(run_id, 0, "maybe")


def test_rejection_log_only_includes_reasoned_rejections(state):
    run_id = state.record_run(stage="sd", days=1, portions=10, province="",
                              resolved={}, trace=[])
    state.record_review(run_id, 0, "accept")
    state.record_review(run_id, 1, "reject")               # no reason given
    state.record_review(run_id, 2, "reject", reason="Kids won't like it")
    log = state.rejection_log()
    assert len(log) == 1
    assert log[0]["reason"] == "Kids won't like it"


def test_stock_upsert_merges_partial_updates(state):
    run_id = state.record_run(stage="sd", days=1, portions=10, province="",
                              resolved={}, trace=[])
    state.upsert_stock(run_id, 0, "Beras", received_pct=100.0, used_pct=90.0,
                       actual_cost_idr=50000.0, note="")
    state.upsert_stock(run_id, 0, "Beras", used_pct=85.0)   # partial update
    records = state.stock_for_run(run_id)
    assert len(records) == 1
    assert records[0]["received_pct"] == 100.0     # preserved from first call
    assert records[0]["used_pct"] == 85.0           # overwritten by second call
    assert records[0]["actual_cost_idr"] == 50000.0  # preserved


def test_rejected_dishes_joins_reviews_to_served_log(state):
    run_id = state.record_run(stage="sd", days=2, portions=10, province="",
                              resolved={}, trace=[])
    state.record_served(run_id, [(0, "hewani", 101, "Ayam Goreng"),
                                 (0, "nabati", 202, "Tempe Bacem"),
                                 (1, "hewani", 303, "Ikan Bakar")])
    state.record_review(run_id, 0, "reject", reason="Too expensive")
    state.record_review(run_id, 1, "accept")   # accepted day must not appear

    rejected = state.rejected_dishes()
    ids = {d["dish_id"] for d in rejected}
    assert ids == {101, 202}
    assert 303 not in ids
    entry = next(d for d in rejected if d["dish_id"] == 101)
    assert entry["dish_name"] == "Ayam Goreng"
    assert entry["reasons"] == ["Too expensive"]


def test_rejected_dishes_accumulates_multiple_reasons_for_one_dish(state):
    run_a = state.record_run(stage="sd", days=1, portions=10, province="",
                             resolved={}, trace=[])
    run_b = state.record_run(stage="sd", days=1, portions=10, province="",
                             resolved={}, trace=[])
    state.record_served(run_a, [(0, "hewani", 555, "Rendang")])
    state.record_served(run_b, [(0, "hewani", 555, "Rendang")])
    state.record_review(run_a, 0, "reject", reason="Too expensive")
    state.record_review(run_b, 0, "reject", reason="Kids won't like it")

    entry = next(d for d in state.rejected_dishes() if d["dish_id"] == 555)
    assert set(entry["reasons"]) == {"Too expensive", "Kids won't like it"}


def test_stock_scoped_per_run(state):
    run_a = state.record_run(stage="sd", days=1, portions=10, province="",
                             resolved={}, trace=[])
    run_b = state.record_run(stage="sd", days=1, portions=10, province="",
                             resolved={}, trace=[])
    state.upsert_stock(run_a, 0, "Beras", received_pct=100.0)
    state.upsert_stock(run_b, 0, "Beras", received_pct=50.0)
    assert state.stock_for_run(run_a)[0]["received_pct"] == 100.0
    assert state.stock_for_run(run_b)[0]["received_pct"] == 50.0
