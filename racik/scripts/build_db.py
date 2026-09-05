"""Build racik.db from the source TKPI table and the regional recipe corpus.

Run:  python scripts/build_db.py
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from racik.config import DB_PATH, RAW_DIR
from racik.nutrition import RecipeEngine, build_component_dishes
from racik.prices import PriceTable
from racik.tkpi import NUTRIENT_KEYS, load_default

RECIPES_XLSX = RAW_DIR / "Indonesian_Recipes_HF_Regional.xlsx"
SHEET = "Regional Dishes"

_NUTRIENT_COLS = ", ".join(f"{k} REAL" for k in NUTRIENT_KEYS)


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(f"""
    DROP TABLE IF EXISTS foods;
    DROP TABLE IF EXISTS dishes;
    DROP TABLE IF EXISTS dish_lines;

    CREATE TABLE foods (
        code TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        group_code TEXT, group_label TEXT, jenis TEXT, source TEXT,
        bdd REAL, bdd_imputed INTEGER,
        {_NUTRIENT_COLS}
    );

    CREATE TABLE dishes (
        dish_id INTEGER PRIMARY KEY,
        name TEXT NOT NULL, province TEXT, island TEXT, confidence TEXT,
        slot TEXT, servings INTEGER, method TEXT,
        cost_per_portion_idr REAL, portion_mass_g REAL,
        quality REAL, quality_label TEXT,
        lines_total INTEGER, lines_matched INTEGER, mass_matched_share REAL,
        unquantified_share REAL, proxy_matches INTEGER,
        steps TEXT, ingredient_index TEXT,
        main_food_code TEXT, main_food_name TEXT,
        {_NUTRIENT_COLS}
    );

    CREATE TABLE dish_lines (
        dish_id INTEGER, seq INTEGER,
        raw TEXT, name TEXT, quantity REAL, unit TEXT,
        gross_g REAL, edible_g REAL, cost_idr REAL, cost_basis TEXT,
        tkpi_code TEXT, tkpi_name TEXT, match_method TEXT,
        urt_confidence TEXT, urt_rule TEXT, unquantified INTEGER,
        PRIMARY KEY (dish_id, seq)
    );

    CREATE INDEX idx_dishes_slot ON dishes(slot);
    CREATE INDEX idx_dishes_province ON dishes(province);
    CREATE INDEX idx_dishes_island ON dishes(island);
    CREATE INDEX idx_lines_dish ON dish_lines(dish_id);
    CREATE INDEX idx_dishes_mainfood ON dishes(main_food_code);
    """)


def write_foods(conn: sqlite3.Connection, foods) -> None:
    cols = ["code", "name", "group_code", "group_label", "jenis", "source",
            "bdd", "bdd_imputed", *NUTRIENT_KEYS]
    placeholders = ",".join("?" * len(cols))
    conn.executemany(
        f"INSERT INTO foods ({','.join(cols)}) VALUES ({placeholders})",
        [(f.code, f.name, f.group_code, f.group_label, f.jenis, f.source,
          f.bdd, int(f.bdd_imputed), *[f.nutrients.get(k, 0.0) for k in NUTRIENT_KEYS])
         for f in foods],
    )


def write_dishes(conn: sqlite3.Connection, dishes) -> None:
    dish_cols = ["dish_id", "name", "province", "island", "confidence", "slot",
                 "servings", "method", "cost_per_portion_idr", "portion_mass_g",
                 "quality", "quality_label", "lines_total", "lines_matched",
                 "mass_matched_share", "unquantified_share", "proxy_matches",
                 "steps", "ingredient_index", "main_food_code", "main_food_name",
                 *NUTRIENT_KEYS]
    conn.executemany(
        f"INSERT INTO dishes ({','.join(dish_cols)}) "
        f"VALUES ({','.join('?' * len(dish_cols))})",
        [(d.dish_id, d.name, d.province, d.island, d.confidence, d.slot,
          d.servings, d.method, d.cost_per_portion_idr, d.portion_mass_g,
          d.quality.score, d.quality.label, d.quality.lines_total,
          d.quality.lines_matched, d.quality.mass_matched_share,
          d.quality.unquantified_share, d.quality.proxy_matches, d.steps,
          d.ingredient_index, d.main_food_code, d.main_food_name,
          *[d.per_portion.get(k, 0.0) for k in NUTRIENT_KEYS])
         for d in dishes],
    )

    rows = []
    for d in dishes:
        for seq, ln in enumerate(d.lines):
            rows.append((
                d.dish_id, seq, ln.parsed.raw, ln.parsed.name,
                ln.parsed.quantity, ln.parsed.unit, ln.gross_g, ln.edible_g,
                ln.cost_idr, ln.cost_basis,
                ln.code, ln.match.food.name if ln.match else None,
                ln.match.method if ln.match else None,
                ln.parsed.confidence.value, ln.parsed.rule,
                int(ln.parsed.unquantified),
            ))
    conn.executemany(
        "INSERT INTO dish_lines VALUES (" + ",".join("?" * 16) + ")", rows)


def main() -> None:
    started = time.time()
    print("loading TKPI ...")
    foods = load_default()
    prices = PriceTable.load()
    engine = RecipeEngine(foods, prices)

    print(f"loading recipes from {RECIPES_XLSX.name} ...")
    df = pd.read_excel(RECIPES_XLSX, sheet_name=SHEET)
    rows = [
        {
            "dish_id": int(r["No"]),
            "name": str(r["Dish Name (cleaned)"]),
            "ingredients": str(r["Ingredients"]),
            "steps": str(r["Steps"]) if pd.notna(r["Steps"]) else "",
            "province": str(r["Primary Province"]),
            "island": str(r["Island Group"]),
            "confidence": str(r["Confidence"]),
        }
        for _, r in df.iterrows()
    ]

    print(f"building {len(rows)} dishes ...")
    dishes = engine.build_many(rows)
    print(f"  -> {len(dishes)} usable dishes "
          f"({len(rows) - len(dishes)} unclassifiable)")

    components = build_component_dishes(foods, prices)
    dishes.extend(components)
    print(f"  -> {len(components)} served components (rice, tubers, fruit, milk)")

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        create_schema(conn)
        write_foods(conn, foods.foods)
        write_dishes(conn, dishes)
        conn.commit()
    finally:
        conn.close()

    print(f"wrote {DB_PATH} in {time.time() - started:.1f}s")

    by_slot = {}
    for d in dishes:
        by_slot[d.slot] = by_slot.get(d.slot, 0) + 1
    for slot, n in sorted(by_slot.items(), key=lambda kv: -kv[1]):
        print(f"  {slot:8s} {n:6d}")


if __name__ == "__main__":
    main()
