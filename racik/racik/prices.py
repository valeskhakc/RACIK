"""Ingredient prices in rupiah per kilogram.

Cost is charged on **as-purchased (gross) weight**, not edible weight. For a
food with BDD 58% (bone-in chicken), 75 g on the tray costs 129 g of purchase:

    purchase_g = edible_g / (BDD/100)
    cost_idr   = purchase_g / 1000 * price_idr_per_kg

Data source policy
------------------
Bapanas Panel Harga and BI PIHPS publish daily national/provincial prices, but
only through undocumented internal JSON endpoints that geo-block non-Indonesian
egress. They are scraping targets, not a stable API contract. This module
therefore treats a versioned local snapshot as the source of truth and exposes
``LivePriceClient`` as an optional refresher that must fail closed onto the
snapshot. Nothing in the planner depends on the network.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import DATA_DIR
from .urt import normalize


@dataclass(frozen=True)
class Price:
    idr_per_kg: float
    basis: str      # "commodity" | "group" | "name" | "default"


class PriceTable:
    """Rupiah/kg lookups with an explicit fallback ladder.

    Resolution order: exact TKPI code -> unmatched-ingredient name -> food
    group median -> table default. The basis is returned alongside the number
    so a costing can report how much of it rests on real commodity prices.
    """

    def __init__(self, payload: dict, cost_index: float = 1.0):
        self.meta = payload.get("meta", {})
        self.by_code = {k: float(v) for k, v in payload.get("by_code", {}).items()}
        self.by_group = {k: float(v) for k, v in payload.get("by_group", {}).items()}
        self.by_name = {normalize(k): float(v)
                        for k, v in payload.get("unmatched_by_name", {}).items()}
        self.default = float(payload.get("default_idr_per_kg", 25_000))
        self.cost_index = cost_index
        # Derived from data/wfp_food_prices_idn.csv by scripts/build_prices.py:
        # {province -> ratio of that province's retail price to the national
        # mean, for the commodities the WFP survey tracks}. Real market data,
        # not a placeholder — see regional_index_for() and that script's docstring.
        self._regional_index = {
            k: float(v) for k, v in payload.get("regional_cost_index_by_province", {}).items()}

    @classmethod
    def load(cls, path: str | Path | None = None, cost_index: float = 1.0) -> "PriceTable":
        path = Path(path) if path else DATA_DIR / "prices_id.json"
        return cls(json.loads(Path(path).read_text(encoding="utf-8")), cost_index)

    def lookup(self, code: Optional[str] = None, name: str = "",
               group_code: Optional[str] = None) -> Price:
        if code and code in self.by_code:
            return Price(self.by_code[code] * self.cost_index, "commodity")
        key = normalize(name)
        if key in self.by_name:
            return Price(self.by_name[key] * self.cost_index, "name")
        if group_code and group_code in self.by_group:
            return Price(self.by_group[group_code] * self.cost_index, "group")
        return Price(self.default * self.cost_index, "default")

    def cost_idr(self, purchase_grams: float, code: Optional[str] = None,
                 name: str = "", group_code: Optional[str] = None) -> tuple[float, Price]:
        price = self.lookup(code, name, group_code)
        return purchase_grams / 1000.0 * price.idr_per_kg, price

    def with_cost_index(self, cost_index: float) -> "PriceTable":
        """A view of the same table scaled by a regional cost index."""
        clone = PriceTable.__new__(PriceTable)
        clone.__dict__.update(self.__dict__)
        clone.cost_index = cost_index
        return clone

    def regional_index_for(self, province: str) -> float:
        """Real "indeks kemahalan daerah" for `province`, or 1.0 (national
        baseline) if it isn't covered by the WFP survey behind this table.

        Case-insensitive; the province string is expected in the recipe
        corpus's spelling (racik.store.RacikStore.provinces()).
        """
        if not province:
            return 1.0
        target = province.strip().lower()
        for name, index in self._regional_index.items():
            if name.lower() == target:
                return index
        return 1.0

    def has_regional_data(self) -> bool:
        return bool(self._regional_index)


class LivePriceClient:
    """Optional refresher for the snapshot from Bapanas Panel Harga.

    Deliberately conservative: it never raises into the planner and never
    silently substitutes a partial fetch for the snapshot. Callers get an
    updated table only when a fetch fully succeeds.

    The endpoint path and parameters (``komoditas_id``, ``province_id``,
    ``kotakab_id``, ``level_harga`` where 1=produsen, 2=grosir, 3=eceran) must
    be confirmed live in browser DevTools; they are not a published contract.
    """

    BASE_URL = "https://api-panelhargav2.badanpangan.go.id/api/front/harga-pangan-informasi"

    def __init__(self, table: PriceTable, timeout: float = 8.0):
        self.table = table
        self.timeout = timeout
        self.last_error: Optional[str] = None

    def refresh(self, province_id: Optional[int] = None,
                level_harga: int = 3) -> PriceTable:
        """Try to refresh; return the existing table unchanged on any failure."""
        try:
            import urllib.request

            params = f"?level_harga={level_harga}"
            if province_id:
                params += f"&province_id={province_id}"
            request = urllib.request.Request(
                self.BASE_URL + params,
                headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:                      # network, geo-block, schema
            self.last_error = f"{type(exc).__name__}: {exc}"
            return self.table

        updates = self._extract(payload)
        if not updates:
            self.last_error = "response carried no recognisable price rows"
            return self.table

        merged = PriceTable.__new__(PriceTable)
        merged.__dict__.update(self.table.__dict__)
        merged.by_name = {**self.table.by_name, **updates}
        self.last_error = None
        return merged

    @staticmethod
    def _extract(payload) -> dict[str, float]:
        """Pull {commodity_name: price} out of whatever shape came back."""
        rows = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            return {}
        out: dict[str, float] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = row.get("name") or row.get("nama") or row.get("komoditas")
            value = row.get("today") or row.get("harga") or row.get("price")
            try:
                if name and value is not None and float(value) > 0:
                    out[normalize(str(name))] = float(value)
            except (TypeError, ValueError):
                continue
        return out
