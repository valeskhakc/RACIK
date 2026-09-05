"""Read access to racik.db.

Dishes load without their ingredient lines — 18k dishes carry ~260k lines, and
the planner only needs lines for the ~25 dishes it actually selects. Lines are
fetched on demand for costing and procurement.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from .config import DB_PATH
from .nutrition import Dish, DishQuality, IngredientLine
from .tkpi import NUTRIENT_KEYS
from .urt import Confidence, ParsedIngredient


@dataclass(frozen=True)
class StoredLine:
    """A dish ingredient as persisted — enough for procurement and audit."""

    seq: int
    raw: str
    name: str
    quantity: Optional[float]
    unit: Optional[str]
    gross_g: float
    edible_g: float
    cost_idr: float
    cost_basis: str
    tkpi_code: Optional[str]
    tkpi_name: Optional[str]
    match_method: Optional[str]
    urt_confidence: str
    urt_rule: str
    unquantified: bool

    @property
    def food_name(self) -> str:
        return self.tkpi_name or self.name


class RacikStore:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else DB_PATH
        if not self.path.exists():
            raise FileNotFoundError(
                f"{self.path} not found — run `python scripts/build_db.py` first")
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

    # ------------------------------------------------------------ dishes
    def load_dishes(self, min_quality: float = 0.0,
                    slots: Optional[Iterable[str]] = None) -> list[Dish]:
        sql = "SELECT * FROM dishes WHERE quality >= ?"
        params: list = [min_quality]
        if slots:
            slots = list(slots)
            sql += f" AND slot IN ({','.join('?' * len(slots))})"
            params += slots
        return [_row_to_dish(row) for row in self.conn.execute(sql, params)]

    def dish(self, dish_id: int) -> Optional[Dish]:
        row = self.conn.execute(
            "SELECT * FROM dishes WHERE dish_id = ?", (dish_id,)).fetchone()
        return _row_to_dish(row) if row else None

    def lines(self, dish_id: int) -> list[StoredLine]:
        rows = self.conn.execute(
            "SELECT * FROM dish_lines WHERE dish_id = ? ORDER BY seq", (dish_id,))
        return [
            StoredLine(
                seq=r["seq"], raw=r["raw"], name=r["name"],
                quantity=r["quantity"], unit=r["unit"], gross_g=r["gross_g"],
                edible_g=r["edible_g"], cost_idr=r["cost_idr"],
                cost_basis=r["cost_basis"], tkpi_code=r["tkpi_code"],
                tkpi_name=r["tkpi_name"], match_method=r["match_method"],
                urt_confidence=r["urt_confidence"], urt_rule=r["urt_rule"],
                unquantified=bool(r["unquantified"]),
            )
            for r in rows
        ]

    def hydrate(self, dish: Dish) -> Dish:
        """Attach persisted ingredient lines to a dish loaded without them."""
        if dish.lines:
            return dish
        dish.lines = [
            IngredientLine(
                parsed=ParsedIngredient(
                    raw=ln.raw, name=ln.name, quantity=ln.quantity,
                    unit=ln.unit, grams=ln.gross_g,
                    confidence=Confidence(ln.urt_confidence), rule=ln.urt_rule,
                    unquantified=ln.unquantified),
                match=None, gross_g=ln.gross_g, edible_g=ln.edible_g,
                cost_idr=ln.cost_idr, cost_basis=ln.cost_basis,
                food_name_override=ln.food_name,
            )
            for ln in self.lines(dish.dish_id)
        ]
        return dish

    # ------------------------------------------------------------ metadata
    def provinces(self) -> list[tuple[str, str, int]]:
        rows = self.conn.execute(
            "SELECT province, island, COUNT(*) n FROM dishes "
            "WHERE province NOT IN ('', 'Nasional') "
            "GROUP BY province ORDER BY n DESC")
        return [(r["province"], r["island"], r["n"]) for r in rows]

    def island_of(self, province: str) -> str:
        row = self.conn.execute(
            "SELECT island FROM dishes WHERE province = ? LIMIT 1",
            (province,)).fetchone()
        return row["island"] if row else ""

    def search_dishes(self, query: str = "", slot: Optional[str] = None,
                      province: Optional[str] = None,
                      max_cost_idr: Optional[float] = None,
                      min_protein_g: Optional[float] = None,
                      limit: int = 10) -> list[dict]:
        """Find candidate dishes. Ordered by protein per rupiah, then quality."""
        sql = ["SELECT dish_id, name, slot, province, cost_per_portion_idr,",
               "  energy_kcal, protein_g, quality_label, main_food_name",
               "FROM dishes WHERE 1=1"]
        params: list = []
        if query:
            sql.append("AND name LIKE ?")
            params.append(f"%{query}%")
        if slot:
            sql.append("AND slot = ?")
            params.append(slot)
        if province:
            sql.append("AND province = ?")
            params.append(province)
        if max_cost_idr is not None:
            sql.append("AND cost_per_portion_idr <= ?")
            params.append(float(max_cost_idr))
        if min_protein_g is not None:
            sql.append("AND protein_g >= ?")
            params.append(float(min_protein_g))
        sql.append("ORDER BY protein_g / (cost_per_portion_idr + 1) DESC,"
                   " quality DESC LIMIT ?")
        params.append(int(limit))

        return [
            {
                "dish_id": r["dish_id"], "name": r["name"], "slot": r["slot"],
                "province": r["province"],
                "cost_per_portion_idr": round(r["cost_per_portion_idr"]),
                "energy_kcal": round(r["energy_kcal"]),
                "protein_g": round(r["protein_g"], 1),
                "quality": r["quality_label"],
                "main_ingredient": r["main_food_name"],
            }
            for r in self.conn.execute(" ".join(sql), params)
        ]

    def stats(self) -> dict:
        row = self.conn.execute(
            "SELECT COUNT(*) dishes, AVG(quality) mean_quality, "
            "AVG(cost_per_portion_idr) mean_cost FROM dishes").fetchone()
        slots = {r["slot"]: r["n"] for r in self.conn.execute(
            "SELECT slot, COUNT(*) n FROM dishes GROUP BY slot")}
        foods = self.conn.execute("SELECT COUNT(*) n FROM foods").fetchone()["n"]
        lines = self.conn.execute(
            "SELECT COUNT(*) n FROM dish_lines").fetchone()["n"]
        return {
            "dishes": row["dishes"], "foods": foods, "ingredient_lines": lines,
            "mean_quality": round(row["mean_quality"] or 0, 3),
            "mean_cost_per_portion_idr": round(row["mean_cost"] or 0, 1),
            "by_slot": slots,
        }

    def close(self) -> None:
        self.conn.close()


def _row_to_dish(row: sqlite3.Row) -> Dish:
    quality = DishQuality(
        lines_total=row["lines_total"], lines_matched=row["lines_matched"],
        mass_matched_share=row["mass_matched_share"],
        unquantified_share=row["unquantified_share"],
        piece_default_share=0.0, proxy_matches=row["proxy_matches"],
        stored_score=row["quality"],
    )
    return Dish(
        dish_id=row["dish_id"], name=row["name"], province=row["province"],
        island=row["island"], slot=row["slot"], servings=row["servings"],
        method=row["method"], lines=[],
        per_portion={k: (row[k] or 0.0) for k in NUTRIENT_KEYS},
        cost_per_portion_idr=row["cost_per_portion_idr"],
        portion_mass_g=row["portion_mass_g"], quality=quality,
        steps=row["steps"] or "", confidence=row["confidence"] or "",
        ingredient_index=row["ingredient_index"] or "",
        main_food_code=row["main_food_code"] or "",
        main_food_name=row["main_food_name"] or "",
    )
