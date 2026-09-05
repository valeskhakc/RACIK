"""Derive real ingredient prices and per-province regional cost indices from
the WFP Indonesia food-price dataset, and write them into data/prices_id.json.

Run:  python scripts/build_prices.py

Why this exists
----------------
prices_id.json previously held hand-curated placeholder figures (its own
"meta.national_reference" claimed Bapanas/BI PIHPS sourcing, but nothing in
the codebase ever actually fetched from them — LivePriceClient in prices.py
is a live-refresh *adapter*, not a one-time importer, and it fails closed
onto the snapshot because Bapanas/PIHPS geo-block non-Indonesian egress).
data/wfp_food_prices_idn.csv is a real dataset: 315k retail price
observations across 34 provinces and 30 commodities, 2007-2026, from the
World Food Programme. This script is the one-time ETL that turns it into:

1. Updated `by_code` entries for the ~11 TKPI foods the WFP commodities map
   onto (rice, wheat flour, chicken, beef, eggs, garlic, shallot, two chili
   varieties, sugar, cooking oil) — real national retail averages over the
   most recent 12 months of data, replacing the placeholder numbers.
2. A new `regional_cost_index_by_province` table: for each province with
   enough overlapping commodity data, the ratio of that province's average
   price (across the same commodities) to the national average — a genuine
   "indeks kemahalan daerah" instead of the flat 1.0 every solve used before.
   `racik/prices.py`'s `PriceTable.regional_index_for()` reads this, and
   `racik/agents.py`'s Agen Biaya uses it as its grounded default rather than
   inventing a number.

The 19 other commodity types in prices_id.json (spices, proteins the WFP
survey doesn't track, etc.) are untouched — this script only overwrites what
it has real data for.
"""
from __future__ import annotations

import csv
import json
import statistics
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
WFP_CSV = DATA_DIR / "wfp_food_prices_idn.csv"
PRICES_JSON = DATA_DIR / "prices_id.json"

# WFP commodity name (as it appears in the CSV, prefix-matched) -> TKPI code.
# Built by hand against racik/data/raw/TKPI_2020_Table_4_All_Data.csv — see
# the module docstring in racik/tkpi.py for why this can't be automated
# (TKPI names are inverted and substring matching is unsafe).
COMMODITY_TO_TKPI = {
    "Rice": ("AR001", "Beras giling, mentah"),
    "Wheat flour": ("AP025", "Tepung terigu"),
    "Meat (chicken": ("FR005", "Ayam, daging, segar"),          # covers "chicken", "chicken, broiler"
    "Meat (beef": ("FR026", "Sapi, daging, lemak sedang, segar"),  # covers all beef grades
    "Eggs": ("HR002", "Telur ayam ras, segar"),                  # covers "Eggs", "Eggs (broiler)"
    "Garlic": ("NR008", "Bawang putih, segar"),
    "Onions (shallot": ("NR007", "Bawang merah, segar"),
    "Chili (red": ("NR014", "Cabai merah, segar"),
    "Chili (bird's eye": ("NR015", "Cabai rawit, segar"),
    "Sugar": ("MP007", "Gula putih"),
    "Oil (vegetable": ("KR012", "Minyak kelapa sawit"),
}

# WFP admin1 (upper-case) -> corpus province spelling (store.py's provinces()).
# .title()-casing the rest matches every other province except these, whose
# corpus spelling keeps an acronym or "Kepulauan"/"DI" capitalised specially.
ADMIN1_TO_CORPUS_PROVINCE = {
    "DAERAH ISTIMEWA YOGYAKARTA": "DI Yogyakarta",
    "DKI JAKARTA": "DKI Jakarta",
}

RECENT_WINDOW_DAYS = 365
MIN_OVERLAPPING_COMMODITIES = 3   # a province needs data on >= this many to get its own index


def commodity_family(name: str) -> str | None:
    for prefix, (code, _) in COMMODITY_TO_TKPI.items():
        if name.startswith(prefix):
            return prefix
    return None


def load_rows() -> tuple[list[dict], date]:
    with WFP_CSV.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    max_date = max(date.fromisoformat(r["date"]) for r in rows)
    cutoff = max_date - timedelta(days=RECENT_WINDOW_DAYS)
    print(f"latest observation: {max_date}, using data from {cutoff} onward")
    out = []
    for r in rows:
        if r["unit"] != "KG":
            continue
        family = commodity_family(r["commodity"])
        if family is None:
            continue
        if date.fromisoformat(r["date"]) < cutoff:
            continue
        try:
            price = float(r["price"])
        except ValueError:
            continue
        if price <= 0:
            continue
        out.append({"family": family, "admin1": r["admin1"].strip(),
                    "market": r["market"], "price": price})
    print(f"{len(out)} usable observations in the window across {len(COMMODITY_TO_TKPI)} commodity families")
    return out, max_date


def national_prices(rows: list[dict]) -> dict[str, float]:
    """Mean price per family, preferring 'National Average' market rows and
    falling back to the mean across all provincial rows if none exist."""
    by_family_national = defaultdict(list)
    by_family_all = defaultdict(list)
    for r in rows:
        by_family_all[r["family"]].append(r["price"])
        if r["market"] == "National Average":
            by_family_national[r["family"]].append(r["price"])
    out = {}
    for family in COMMODITY_TO_TKPI:
        if by_family_national[family]:
            out[family] = statistics.mean(by_family_national[family])
        elif by_family_all[family]:
            out[family] = statistics.mean(by_family_all[family])
    return out


def provincial_prices(rows: list[dict]) -> dict[str, dict[str, float]]:
    """{admin1: {family: mean_price}} — provincial (non-national-average) rows only."""
    by_province_family = defaultdict(list)
    for r in rows:
        if not r["admin1"] or r["market"] == "National Average":
            continue
        by_province_family[(r["admin1"], r["family"])].append(r["price"])
    out: dict[str, dict[str, float]] = defaultdict(dict)
    for (admin1, family), prices in by_province_family.items():
        out[admin1][family] = statistics.mean(prices)
    return out


def regional_index(provincial: dict[str, dict[str, float]],
                   national: dict[str, float]) -> dict[str, float]:
    out = {}
    for admin1, family_prices in provincial.items():
        ratios = [price / national[family] for family, price in family_prices.items()
                 if family in national and national[family] > 0]
        if len(ratios) < MIN_OVERLAPPING_COMMODITIES:
            continue
        corpus_name = ADMIN1_TO_CORPUS_PROVINCE.get(admin1, admin1.title())
        out[corpus_name] = round(statistics.mean(ratios), 3)
    return out


def main() -> None:
    rows, latest = load_rows()
    national = national_prices(rows)
    provincial = provincial_prices(rows)
    index = regional_index(provincial, national)

    payload = json.loads(PRICES_JSON.read_text(encoding="utf-8"))
    updated_codes = []
    for family, price in national.items():
        code, tkpi_name = COMMODITY_TO_TKPI[family]
        old = payload["by_code"].get(code)
        payload["by_code"][code] = round(price / 1000) * 1000   # round to nearest Rp1,000/kg
        updated_codes.append((code, tkpi_name, old, payload["by_code"][code]))

    payload["regional_cost_index_by_province"] = index
    payload["meta"]["wfp_source"] = (
        f"World Food Programme Indonesia retail price survey "
        f"(data/wfp_food_prices_idn.csv), {RECENT_WINDOW_DAYS}-day trailing "
        f"mean ending {latest.isoformat()}")
    payload["meta"]["wfp_updated_codes"] = [c for c, _, _, _ in updated_codes]
    payload["meta"]["regional_index_note"] = (
        "regional_cost_index_by_province is derived from the same WFP survey: "
        "for each province, the mean ratio of its retail price to the national "
        "average across the commodities both share data for. Provinces with "
        f"fewer than {MIN_OVERLAPPING_COMMODITIES} overlapping commodities are "
        "omitted (PriceTable.regional_index_for() falls back to 1.0 for those).")

    PRICES_JSON.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                           encoding="utf-8")

    print("\nUpdated by_code entries (code | food | old -> new Rp/kg):")
    for code, tkpi_name, old, new in sorted(updated_codes):
        print(f"  {code}  {tkpi_name:35s} {old!s:>8} -> {new}")

    print(f"\nRegional cost index for {len(index)} provinces:")
    for province, idx in sorted(index.items(), key=lambda kv: -kv[1]):
        print(f"  {province:28s} {idx}")
    skipped = set(ADMIN1_TO_CORPUS_PROVINCE.get(a, a.title()) for a in provincial) - set(index)
    if skipped:
        print(f"\nSkipped (fewer than {MIN_OVERLAPPING_COMMODITIES} overlapping commodities): "
              f"{', '.join(sorted(skipped))}")

    print(f"\nwrote {PRICES_JSON}")


if __name__ == "__main__":
    main()
