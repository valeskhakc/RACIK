"""Persistence for everything a solve does not own: operator review, the
served-dish history that drives rotation/decay, and stock/actual-cost audit
records.

Deliberately a second SQLite file (``STATE_DB_PATH``), not new tables bolted
onto ``racik.db``: that database is a read-only artefact wholesale-rebuilt by
``scripts/build_db.py`` — mixing appendable operator state into it would mean
a rebuild silently wipes review history. This module owns its own file,
created on first use, and is never touched by the ETL.

Three tables map directly onto the parts of the planning workflow that had no
memory before this module existed:

* ``runs``         one row per ``/api/generate`` call — the resolved request
                    and the full agent/validator trace, so "why did Racik plan
                    this" is answerable after the fact, not just in the moment.
* ``day_reviews``   operator accept/reject + reason per day — "Operator
                    review" in the workflow diagram.
* ``served_log``    which dish filled which slot on which day — "Menu
                    history": the next ``generate()`` call reads this back to
                    keep recently-served dishes off the tray (rotation), and
                    it decays on its own because the read is windowed by date,
                    not a hard permanent ban.
* ``stock_records`` received/used/actual-cost entries against a planned day —
                    the Inventory & Audit tab's data of record.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from .config import STATE_DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    stage TEXT NOT NULL,
    days INTEGER NOT NULL,
    portions INTEGER NOT NULL,
    province TEXT NOT NULL DEFAULT '',
    resolved_json TEXT NOT NULL,   -- PlanRequest-shaped dict, replayable for a re-solve
    trace_json TEXT NOT NULL       -- agent/validator pipeline trace
);

CREATE TABLE IF NOT EXISTS day_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    day_index INTEGER NOT NULL,
    decision TEXT NOT NULL,        -- 'accept' | 'reject'
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS served_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    day_index INTEGER NOT NULL,
    slot TEXT NOT NULL,
    dish_id INTEGER NOT NULL,
    dish_name TEXT NOT NULL,
    served_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stock_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    day_index INTEGER NOT NULL,
    ingredient_name TEXT NOT NULL,
    received_pct REAL NOT NULL DEFAULT 100.0,
    used_pct REAL NOT NULL DEFAULT 100.0,
    actual_cost_idr REAL,
    note TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    UNIQUE(run_id, day_index, ingredient_name)
);

CREATE INDEX IF NOT EXISTS idx_served_log_served_at ON served_log(served_at);
CREATE INDEX IF NOT EXISTS idx_day_reviews_run ON day_reviews(run_id, day_index);
CREATE INDEX IF NOT EXISTS idx_stock_run ON stock_records(run_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StateStore:
    """Operator-facing state that accumulates across the app's lifetime."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else STATE_DB_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    # ------------------------------------------------------------ runs
    def record_run(self, *, stage: str, days: int, portions: int, province: str,
                   resolved: dict, trace: list[dict]) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs (created_at, stage, days, portions, province, "
            "resolved_json, trace_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_now(), stage, days, portions, province,
             json.dumps(resolved, ensure_ascii=False),
             json.dumps(trace, ensure_ascii=False)))
        self.conn.commit()
        return int(cur.lastrowid)

    def run(self, run_id: int) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        return {
            "run_id": row["run_id"], "created_at": row["created_at"],
            "stage": row["stage"], "days": row["days"], "portions": row["portions"],
            "province": row["province"],
            "resolved": json.loads(row["resolved_json"]),
            "trace": json.loads(row["trace_json"]),
        }

    # ------------------------------------------------------------ served log (menu history)
    def record_served(self, run_id: int, entries: list[tuple[int, str, int, str]]) -> None:
        """entries: (day_index, slot, dish_id, dish_name) — component dishes
        (rice/fruit, synthetic ids) are skipped by the caller; only real
        recipe-corpus dishes are worth remembering for rotation."""
        served_at = _now()
        self.conn.executemany(
            "INSERT INTO served_log (run_id, day_index, slot, dish_id, dish_name, served_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(run_id, day_index, slot, dish_id, dish_name, served_at)
             for day_index, slot, dish_id, dish_name in entries])
        self.conn.commit()

    def recent_served_dish_ids(self, within_days: int = 14) -> set[int]:
        """Dish ids served in the last `within_days` — the decay window.

        A hard hash-set exclude, not a soft penalty: simple, auditable, and
        the window itself is what makes it decay rather than a permanent ban.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=within_days)).isoformat(
            timespec="seconds")
        rows = self.conn.execute(
            "SELECT DISTINCT dish_id FROM served_log WHERE served_at >= ?", (cutoff,))
        return {int(r["dish_id"]) for r in rows}

    def recent_served(self, limit: int = 30) -> list[dict]:
        rows = self.conn.execute(
            "SELECT run_id, day_index, slot, dish_id, dish_name, served_at "
            "FROM served_log ORDER BY served_at DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ operator review
    def record_review(self, run_id: int, day_index: int, decision: str,
                      reason: str = "") -> None:
        if decision not in ("accept", "reject"):
            raise ValueError(f"decision must be 'accept' or 'reject', got {decision!r}")
        self.conn.execute(
            "INSERT INTO day_reviews (run_id, day_index, decision, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (run_id, day_index, decision, reason, _now()))
        self.conn.commit()

    def reviews_for_run(self, run_id: int) -> dict[int, dict]:
        """Latest review per day_index for one run."""
        rows = self.conn.execute(
            "SELECT day_index, decision, reason, created_at FROM day_reviews "
            "WHERE run_id = ? ORDER BY created_at ASC", (run_id,))
        out: dict[int, dict] = {}
        for r in rows:
            out[r["day_index"]] = dict(r)          # later rows overwrite earlier ones
        return out

    def rejection_log(self, limit: int = 50) -> list[dict]:
        """Reasons the operator has given for rejecting a day, most recent first.

        Feeds the Settings "feedback preferences learned so far" panel and,
        via `generate()`, becomes context Agen Gizi sees on the next run.
        """
        rows = self.conn.execute(
            "SELECT run_id, day_index, reason, created_at FROM day_reviews "
            "WHERE decision = 'reject' AND reason != '' "
            "ORDER BY created_at DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def rejected_dishes(self, limit: int = 20) -> list[dict]:
        """Which real dishes were on a rejected day, and why — this is the
        "feedback for the AI model to learn from": generate() reads this back
        and Agen Gizi excludes these dishes from future full regenerations,
        not just the immediate one-day replacement `replace_day()` already
        makes. Joins day_reviews to served_log on (run_id, day_index); a
        rejected day's synthetic staple/fruit servings never appear here
        (served_log never logs them — see orchestrator._persist_served).
        """
        rows = self.conn.execute(
            "SELECT dr.run_id, dr.day_index, dr.reason, dr.created_at, "
            "sl.dish_id, sl.dish_name, sl.slot "
            "FROM day_reviews dr JOIN served_log sl "
            "ON sl.run_id = dr.run_id AND sl.day_index = dr.day_index "
            "WHERE dr.decision = 'reject' "
            "ORDER BY dr.created_at DESC LIMIT ?", (limit * 4,))
        # One row per (dish, reject event); cap to `limit` distinct dishes,
        # most recent rejection first, keeping every reason a dish collected.
        by_dish: dict[int, dict] = {}
        for r in rows:
            entry = by_dish.setdefault(r["dish_id"], {
                "dish_id": r["dish_id"], "dish_name": r["dish_name"],
                "slot": r["slot"], "reasons": []})
            if r["reason"] and r["reason"] not in entry["reasons"]:
                entry["reasons"].append(r["reason"])
            if len(by_dish) >= limit:
                break
        return list(by_dish.values())

    # ------------------------------------------------------------ stock / audit
    def upsert_stock(self, run_id: int, day_index: int, ingredient_name: str,
                     received_pct: Optional[float] = None,
                     used_pct: Optional[float] = None,
                     actual_cost_idr: Optional[float] = None,
                     note: Optional[str] = None) -> None:
        existing = self.conn.execute(
            "SELECT received_pct, used_pct, actual_cost_idr, note FROM stock_records "
            "WHERE run_id = ? AND day_index = ? AND ingredient_name = ?",
            (run_id, day_index, ingredient_name)).fetchone()
        merged = {
            "received_pct": received_pct if received_pct is not None
                            else (existing["received_pct"] if existing else 100.0),
            "used_pct": used_pct if used_pct is not None
                       else (existing["used_pct"] if existing else 100.0),
            "actual_cost_idr": actual_cost_idr if actual_cost_idr is not None
                               else (existing["actual_cost_idr"] if existing else None),
            "note": note if note is not None else (existing["note"] if existing else ""),
        }
        self.conn.execute(
            "INSERT INTO stock_records (run_id, day_index, ingredient_name, "
            "received_pct, used_pct, actual_cost_idr, note, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(run_id, day_index, ingredient_name) DO UPDATE SET "
            "received_pct=excluded.received_pct, used_pct=excluded.used_pct, "
            "actual_cost_idr=excluded.actual_cost_idr, note=excluded.note, "
            "updated_at=excluded.updated_at",
            (run_id, day_index, ingredient_name, merged["received_pct"],
             merged["used_pct"], merged["actual_cost_idr"], merged["note"], _now()))
        self.conn.commit()

    def stock_for_run(self, run_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT day_index, ingredient_name, received_pct, used_pct, "
            "actual_cost_idr, note, updated_at FROM stock_records "
            "WHERE run_id = ? ORDER BY day_index, ingredient_name", (run_id,))
        return [dict(r) for r in rows]

    def close(self) -> None:
        self.conn.close()
