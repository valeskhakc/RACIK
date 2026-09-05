"""Program-level constants and tunables for Racik.

Everything here is *policy*, not physics: values are editable per SPPG / region.
Sources are cited inline so a reviewer can trace every number to a document.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------- paths
PKG_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PKG_DIR.parent

# Load racik/.env (gitignored, see .env.example) into the process environment
# before anything reads os.environ for a provider key/model id — bedrock.py's
# make_client() and friends read lazily at call time, but config.py is the
# first module every other module imports, so this always runs first.
try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_DIR / ".env")
except ImportError:                # dotenv is optional; real env vars still work
    pass

DATA_DIR = PROJECT_DIR / "data"
RAW_DIR = DATA_DIR / "raw"
DB_PATH = DATA_DIR / "racik.db"
# Separate from DB_PATH deliberately: racik.db is a read-only artefact rebuilt
# wholesale by scripts/build_db.py; this one accumulates operator reviews,
# served-dish history and stock/audit records across the app's lifetime, and
# must never be touched by the ETL.
STATE_DB_PATH = DATA_DIR / "racik_state.db"
# A bank of pre-verified single-day trays for a handful of well-covered
# regions (scripts/precompute_trays.py) — lets the solver skip straight to
# an already-compliant plan on CPU-constrained hosts, for those regions
# only. Optional: the app runs the normal live solve everywhere else, or
# everywhere if this file doesn't exist.
TRAY_CACHE_PATH = DATA_DIR / "precomputed_trays.json"
UI_DIR = PROJECT_DIR / "ui"
# The styled operator frontend (mockup-derived) lives at the repo root, one
# level above the racik/ package — sibling to, not inside, this project.
REPO_ROOT = PROJECT_DIR.parent
FRONTEND_DIR = REPO_ROOT


# ---------------------------------------------------------------- MBG program
@dataclass(frozen=True)
class MBGConfig:
    """Makan Bergizi Gratis program parameters.

    budget_per_portion_idr
        Rp10,000 is the standing national figure for 2025-2026 (cut from an
        original Rp15,000 target by Presidential decision, 29 Nov 2024). At
        the national/BGN level this figure covers ingredients + preparation +
        distribution combined — but the budget Racik's own operator enters at
        onboarding is scoped narrower: it's specifically the food/ingredient
        budget for the plan, not an all-inclusive program pagu with labour
        and logistics baked in. Racik has no visibility into a kitchen's
        separate operational costs, so it can't meaningfully split them out.
    ingredient_budget_share
        1.0: the operator's input is already the ingredient budget, so all of
        it is available for raw ingredients by default. BGN's own per-SPPG
        guidance separately splits ~70% ingredients / 20% operational / 10%
        incentives out of the *national* pagu (Dadan Hindayana, Feb 2026) —
        real context for why that split exists elsewhere, but not a reason to
        reserve part of Racik's own operator-entered budget unless the notes
        say so explicitly (see agents.py's Agen Biaya).
    akg_fraction
        One MBG meal is designed to cover ~1/3 of daily AKG (BGN Deputy Head
        Nanik Sudaryati Deyang, Mar 2026).
    fat_energy_ceiling
        Fat capped at 30% of meal energy — standard Indonesian dietary guidance.
    """

    budget_per_portion_idr: float = 10_000.0
    ingredient_budget_share: float = 1.0
    akg_fraction: float = 1.0 / 3.0
    fat_energy_ceiling: float = 0.30
    # Regional cost index: ingredient budget is "at cost tergantung indeks
    # kemahalan daerah". 1.0 = national baseline.
    regional_cost_index: float = 1.0

    @property
    def ingredient_budget_idr(self) -> float:
        """Rupiah available per portion for raw ingredients."""
        return (
            self.budget_per_portion_idr
            * self.ingredient_budget_share
            * self.regional_cost_index
        )


# ---------------------------------------------------------------- Isi Piringku
# Kemenkos "Isi Piringku" (Permenkes 41/2014) tray composition. The MBG tray is
# staple + animal protein + plant protein + vegetable + fruit, with milk where
# budget and cold chain allow.
SLOTS = ("staple", "hewani", "nabati", "sayur", "buah")
SLOT_LABELS_ID = {
    "staple": "Makanan pokok",
    "hewani": "Lauk hewani",
    "nabati": "Lauk nabati",
    "sayur": "Sayur",
    "buah": "Buah",
}
SLOT_LABELS_EN = {
    "staple": "Staple",
    "hewani": "Animal protein",
    "nabati": "Plant protein",
    "sayur": "Vegetable",
    "buah": "Fruit",
}

# Reference served weight per slot, grams of edible food per portion.
# Anchors: "Isi Piringku" reference portions (~150 g nasi, 75 g animal protein
# or 100 g tahu) plus MBG kitchen practice.
REFERENCE_PORTION_G = {
    "staple": 150.0,
    "hewani": 75.0,
    "nabati": 60.0,
    "sayur": 100.0,
    "buah": 100.0,
}

# How many dishes may fill each slot on a single day.
SLOT_CARDINALITY = {
    "staple": (1, 1),
    "hewani": (1, 1),
    "nabati": (1, 1),
    "sayur": (1, 1),
    "buah": (1, 1),
}

# Slots the optimiser may leave empty if budget is binding. Deliberately empty:
# the full MBG tray is nasi + lauk hewani + lauk nabati + sayur + buah, and a
# fruit-less tray is a non-compliant tray, not a cheaper one. Making fruit
# optional simply taught the solver to drop it every day to save ~Rp1,500.
OPTIONAL_SLOTS: tuple[str, ...] = ()

# Slots filled from the standard served components rather than from the recipe
# corpus. An SPPG cooks three items a day; rice and fruit are standing issue,
# plated as-is. Restricting these slots also removes a whole class of
# misclassification: a battered tempe dish is mostly wheat flour by mass and
# would otherwise present itself as a credible "staple".
COMPONENT_ONLY_SLOTS = ("staple", "buah")

MBG = MBGConfig()
