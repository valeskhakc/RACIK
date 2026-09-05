"""AKG 2019 — Angka Kecukupan Gizi (Permenkes 28/2019).

Two tiers of trust, deliberately kept separate:

* ``MACRO``     — energy/protein/fat/carb/fibre. Corroborated across sources.
                  Safe to use as binding optimiser constraints.
* ``MICRO``     — Ca/Fe/Zn/vit A/vit C/Na. Secondary sources DISAGREE on iron
                  and zinc (e.g. boys 10-12 Fe reported as both 8 and 13 mg;
                  boys 13-15 as both 11 and 19 mg). Until a team member
                  transcribes these from the official Permenkes 28/2019 PDF,
                  they are ADVISORY: reported in the compliance panel, but not
                  binding constraints unless explicitly enabled.

Population-level averages (Pasal 3): 2100 kkal and 57 g protein per capita/day.
Pasal 5 authorises AKG use for "menghitung kebutuhan pangan bergizi pada
penyelenggaraan makanan institusi" — institutional catering, i.e. MBG.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

Sex = Literal["male", "female", "any"]

NATIONAL_AVERAGE_ENERGY_KCAL = 2100.0
NATIONAL_AVERAGE_PROTEIN_G = 57.0

MACRO_NUTRIENTS = ("energy_kcal", "protein_g", "fat_g", "carb_g", "fibre_g")
MICRO_NUTRIENTS = ("calcium_mg", "iron_mg", "zinc_mg", "vit_a_mcg", "vit_c_mg", "sodium_mg")


@dataclass(frozen=True)
class AgeBand:
    key: str
    label_id: str
    label_en: str
    age_min: int
    age_max: int
    sex: Sex
    # macro (verified)
    energy_kcal: float
    protein_g: float
    fat_g: float
    carb_g: float
    fibre_g: float
    # micro (UNVERIFIED — see module docstring)
    calcium_mg: float
    iron_mg: float
    zinc_mg: float
    vit_a_mcg: float
    vit_c_mg: float
    sodium_mg: float

    def daily(self) -> dict[str, float]:
        return {n: getattr(self, n) for n in MACRO_NUTRIENTS + MICRO_NUTRIENTS}

    def per_meal(self, fraction: float) -> dict[str, float]:
        """Nutrient target for a single MBG meal (default ~1/3 of daily AKG)."""
        return {k: v * fraction for k, v in self.daily().items()}


# Lampiran I, Tabel 1 (macro corroborated; micro pending PDF verification).
AGE_BANDS: dict[str, AgeBand] = {
    b.key: b
    for b in [
        AgeBand("4-6",      "Anak 4-6 tahun",     "Children 4-6",   4, 6,   "any",
                1400, 25, 50, 220, 20,  1000,  9,  5, 450, 45,  900),
        AgeBand("7-9",      "Anak 7-9 tahun",     "Children 7-9",   7, 9,   "any",
                1650, 40, 55, 250, 23,  1000, 10, 11, 500, 45, 1000),
        AgeBand("m10-12",   "Laki-laki 10-12 th", "Boys 10-12",    10, 12,  "male",
                2000, 50, 65, 300, 28,  1200,  8, 14, 600, 50, 1300),
        AgeBand("m13-15",   "Laki-laki 13-15 th", "Boys 13-15",    13, 15,  "male",
                2400, 70, 80, 350, 34,  1200, 11, 18, 600, 75, 1500),
        AgeBand("m16-18",   "Laki-laki 16-18 th", "Boys 16-18",    16, 18,  "male",
                2650, 75, 85, 400, 37,  1200, 11, 17, 700, 90, 1700),
        AgeBand("f10-12",   "Perempuan 10-12 th", "Girls 10-12",   10, 12,  "female",
                1900, 55, 65, 280, 27,  1200,  8,  8, 600, 50, 1400),
        AgeBand("f13-15",   "Perempuan 13-15 th", "Girls 13-15",   13, 15,  "female",
                2050, 65, 70, 300, 29,  1200, 15, 16, 600, 65, 1500),
        AgeBand("f16-18",   "Perempuan 16-18 th", "Girls 16-18",   16, 18,  "female",
                2100, 65, 70, 300, 29,  1200, 15, 14, 700, 75, 1600),
    ]
}

# School-stage aliases used by MBG operators (SPPG serve by school, not by band).
SCHOOL_STAGES: dict[str, tuple[str, ...]] = {
    "paud":  ("4-6",),
    "sd":    ("7-9", "m10-12", "f10-12"),
    "smp":   ("m13-15", "f13-15"),
    "sma":   ("m16-18", "f16-18"),
}
STAGE_LABELS = {
    "paud": "PAUD / TK (4-6)",
    "sd": "SD (7-12)",
    "smp": "SMP (13-15)",
    "sma": "SMA (16-18)",
}


def blended_band(keys: list[str], key: str = "blend", label: str = "Blended cohort") -> AgeBand:
    """Mean AKG across several bands — for a kitchen serving a mixed cohort.

    Averaging is the right operation for a *shared* tray: one menu is served to
    everyone, so the floor should reflect the cohort mean, with the caveat that
    the highest-need band is under-served. ``binding_band`` exposes that band.
    """
    if not keys:
        raise ValueError("blended_band requires at least one age-band key")
    bands = [AGE_BANDS[k] for k in keys]
    n = len(bands)
    agg = {f: sum(getattr(b, f) for b in bands) / n
           for f in MACRO_NUTRIENTS + MICRO_NUTRIENTS}
    return replace(bands[0], key=key, label_id=label, label_en=label,
                   age_min=min(b.age_min for b in bands),
                   age_max=max(b.age_max for b in bands),
                   sex="any", **agg)


def binding_band(keys: list[str], nutrient: str = "protein_g") -> AgeBand:
    """The hungriest band in the cohort for ``nutrient`` — who a blend under-serves."""
    return max((AGE_BANDS[k] for k in keys), key=lambda b: getattr(b, nutrient))


def stage_band(stage: str) -> AgeBand:
    """AKG for a school stage, blending sub-bands where the stage spans several."""
    keys = list(SCHOOL_STAGES[stage])
    if len(keys) == 1:
        return AGE_BANDS[keys[0]]
    return blended_band(keys, key=stage, label=STAGE_LABELS[stage])


# Which nutrients are trustworthy enough to constrain on, by default.
BINDING_NUTRIENTS = MACRO_NUTRIENTS
ADVISORY_NUTRIENTS = MICRO_NUTRIENTS

NUTRIENT_META = {
    "energy_kcal": ("Energi", "Energy", "kkal", "verified"),
    "protein_g":   ("Protein", "Protein", "g", "verified"),
    "fat_g":       ("Lemak", "Fat", "g", "verified"),
    "carb_g":      ("Karbohidrat", "Carbohydrate", "g", "verified"),
    "fibre_g":     ("Serat", "Fibre", "g", "verified"),
    "calcium_mg":  ("Kalsium", "Calcium", "mg", "unverified"),
    "iron_mg":     ("Besi", "Iron", "mg", "unverified"),
    "zinc_mg":     ("Seng", "Zinc", "mg", "unverified"),
    "vit_a_mcg":   ("Vitamin A", "Vitamin A", "mcg", "unverified"),
    "vit_c_mg":    ("Vitamin C", "Vitamin C", "mg", "unverified"),
    "sodium_mg":   ("Natrium", "Sodium", "mg", "unverified"),
}
