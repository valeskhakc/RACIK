"""Turn a menu into a shopping list.

Scaling is the point of this module. A dish is stored at its recipe scale with
an estimated serving count; a kitchen cooking 120 portions needs every line
multiplied by 120/servings. Quantities aggregate on the TKPI code, not the
recipe wording, so "bawang merah", "bamer" and "5 siung bawang merah" become
one line on the purchase order.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Optional

from .optimizer import PlanResult
from .store import RacikStore


@dataclass
class ProcurementLine:
    key: str                     # TKPI code, or the ingredient name if unmatched
    name: str
    gross_g: float               # as-purchased grams for the whole order
    cost_idr: float
    matched: bool
    used_in: set[str] = field(default_factory=set)

    @property
    def gross_kg(self) -> float:
        return self.gross_g / 1000.0

    def to_dict(self) -> dict:
        return {
            "key": self.key, "name": self.name,
            "gross_kg": round(self.gross_kg, 3),
            "gross_g": round(self.gross_g, 1),
            "cost_idr": round(self.cost_idr),
            "matched": self.matched,
            "used_in": sorted(self.used_in),
        }


def aggregate(result: PlanResult, store: RacikStore,
              day_indexes: Optional[Iterable[int]] = None) -> list[ProcurementLine]:
    """Ingredient totals for the whole order, largest cost first."""
    wanted = set(day_indexes) if day_indexes is not None else None
    portions = result.request.portions
    lines: dict[str, ProcurementLine] = {}

    for day in result.days:
        if wanted is not None and day.day_index not in wanted:
            continue
        for dish in day.selected:
            scale = portions / max(dish.servings, 1)
            for stored in store.lines(dish.dish_id):
                key = stored.tkpi_code or f"name:{stored.name}"
                entry = lines.get(key)
                if entry is None:
                    entry = ProcurementLine(
                        key=key, name=stored.food_name, gross_g=0.0,
                        cost_idr=0.0, matched=bool(stored.tkpi_code))
                    lines[key] = entry
                entry.gross_g += stored.gross_g * scale
                entry.cost_idr += stored.cost_idr * scale
                entry.used_in.add(dish.name)

    return sorted(lines.values(), key=lambda line: -line.cost_idr)


def totals(lines: Iterable[ProcurementLine]) -> dict:
    lines = list(lines)
    return {
        "items": len(lines),
        "total_cost_idr": round(sum(line.cost_idr for line in lines)),
        "total_mass_kg": round(sum(line.gross_kg for line in lines), 2),
        "unmatched_items": sum(1 for line in lines if not line.matched),
    }
