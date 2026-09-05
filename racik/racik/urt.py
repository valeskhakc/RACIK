"""URT (Ukuran Rumah Tangga) -> grams.

Deterministic, rule-based conversion of Indonesian recipe ingredient lines into
grams. Chosen over an LLM extractor for the numeric step because the result must
be reproducible and auditable: every gram this module returns can be traced to a
named rule, and the rule set is unit-tested.

Conversion anchors (Penuntun Diit / DBMP, Permenkes 41/2014, Kemenkes food
exchange lists):
    1 sdm = 3 sdt = 10 ml      1 gelas = 24 sdm = 240 ml
    1 ons = 100 g  (Indonesian culinary usage - a hectogram, NOT 28 g)
    1 kg  = 1000 g             1 liter = 1000 ml

Countable units (butir, siung, ikat, papan, ...) have no global constant; they
resolve through a per-ingredient lookup with a per-unit fallback, and the result
carries the confidence level that was used.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

# ---------------------------------------------------------------- confidence


class Confidence(str, Enum):
    """How the gram figure was obtained, most to least trustworthy."""

    EXACT = "exact"                   # explicit mass unit in the text (g, kg, ons)
    VOLUME = "volume"                 # volume unit + ingredient density
    PIECE_KNOWN = "piece"             # countable unit, ingredient weight known
    PIECE_DEFAULT = "piece_default"   # countable unit, category fallback used
    NOMINAL = "nominal"               # "secukupnya" - nominal amount assigned
    UNRESOLVED = "unresolved"         # no quantity could be established


# ---------------------------------------------------------------- numbers

_VULGAR = {
    "½": 0.5, "⅓": 1 / 3, "⅔": 2 / 3, "¼": 0.25,
    "¾": 0.75, "⅕": 0.2, "⅖": 0.4, "⅗": 0.6,
    "⅘": 0.8, "⅙": 1 / 6, "⅚": 5 / 6, "⅛": 0.125,
    "⅜": 0.375, "⅝": 0.625, "⅞": 0.875,
}

# Indonesian number words that appear in place of digits.
_WORD_NUMBERS = {
    "satu": 1, "dua": 2, "tiga": 3, "empat": 4, "lima": 5,
    "enam": 6, "tujuh": 7, "delapan": 8, "sembilan": 9, "sepuluh": 10,
    "seperempat": 0.25, "setengah": 0.5, "seperdua": 0.5, "sepertiga": 1 / 3,
}

_NUM = r"\d+(?:[.,]\d+)?"
# mixed (1 1/2) | fraction (1/2) | range (10-15) | plain (1.5)
_QTY_RE = re.compile(
    r"^\s*(?P<qty>"
    r"(?:" + _NUM + r"\s+" + _NUM + r"\s*/\s*" + _NUM + r")"
    r"|(?:" + _NUM + r"\s*/\s*" + _NUM + r")"
    r"|(?:" + _NUM + r"\s*(?:-|–|s/d|sd|sampai)\s*" + _NUM + r")"
    r"|(?:" + _NUM + r")"
    r")\s*",
    re.IGNORECASE,
)

_RANGE_SPLIT = re.compile(r"\s*(?:-|–|s/d|sd|sampai)\s*", re.IGNORECASE)


def _fraction(text: str) -> Optional[float]:
    """Evaluate "3/4" -> 0.75. Returns None if it is not a usable fraction."""
    num, _, den = text.partition("/")
    try:
        numerator, denominator = float(num), float(den)
    except ValueError:
        return None
    return numerator / denominator if denominator else None


def _to_float(raw: str) -> float:
    """Evaluate a quantity token: plain, fraction, mixed number, or range."""
    raw = raw.strip().replace(",", ".")
    raw = re.sub(r"\s*/\s*", "/", raw)      # "1 / 2" -> "1/2"

    parts = [p for p in _RANGE_SPLIT.split(raw) if p.strip()]
    if len(parts) == 2:                      # range -> midpoint
        lo, hi = (_to_float(p) for p in parts)
        return (lo + hi) / 2

    chunks = raw.split()
    if len(chunks) == 2:                     # mixed number "1 1/2"
        frac = _fraction(chunks[1])
        if frac is not None:
            try:
                return float(chunks[0]) + frac
            except ValueError:
                return frac
    if "/" in raw:
        frac = _fraction(raw)
        if frac is not None:
            return frac
        raw = raw.split("/", 1)[0]           # "2/" -> 2
    try:
        return float(raw)
    except ValueError:
        return float(chunks[0]) if chunks and chunks[0].isdigit() else 1.0


def parse_quantity(text: str) -> tuple[Optional[float], str]:
    """Read a leading quantity. Returns (value, remaining_text).

    Ranges collapse to their midpoint; mixed numbers and fractions are honoured;
    a leading vulgar fraction is handled before the regex sees it.
    """
    s = str(text).lstrip()
    if s and s[0] in _VULGAR:
        return _VULGAR[s[0]], s[1:].lstrip()

    m = _QTY_RE.match(s)
    if m:
        rest = s[m.end():]
        value = _to_float(m.group("qty"))
        if rest and rest[0] in _VULGAR:  # "1 1/2" written as digit + glyph
            return value + _VULGAR[rest[0]], rest[1:].lstrip()
        return value, rest

    # word numbers: "dua siung", "setengah sdt"
    head, _, tail = s.partition(" ")
    if head.lower().strip(".,") in _WORD_NUMBERS:
        return _WORD_NUMBERS[head.lower().strip(".,")], tail.lstrip()
    return None, s


# ---------------------------------------------------------------- units

MASS_UNITS_G = {
    "g": 1.0, "gr": 1.0, "gram": 1.0, "grm": 1.0, "grams": 1.0,
    "kg": 1000.0, "kilo": 1000.0, "kilogram": 1000.0, "kgs": 1000.0,
    "ons": 100.0,          # Indonesian usage: 1 ons = 1 hektogram = 100 g
    "hg": 100.0,
    "kwintal": 100_000.0,
    "mg": 0.001,
    "pon": 500.0,          # Indonesian "pon" = half a kilo
}

VOLUME_UNITS_ML = {
    "ml": 1.0, "cc": 1.0, "mililiter": 1.0,
    "l": 1000.0, "liter": 1000.0, "ltr": 1000.0, "litre": 1000.0,
    "sdm": 10.0, "sendok makan": 10.0, "tbsp": 10.0,
    "sdt": 10.0 / 3.0, "sendok teh": 10.0 / 3.0, "tsp": 10.0 / 3.0,
    "sdk": 10.0, "sendok": 10.0,        # bare "sendok" -> assume makan
    "gelas": 240.0, "gls": 240.0, "cangkir": 240.0, "cup": 240.0,
    "mangkok": 400.0, "mangkuk": 400.0,
}

# Countable / descriptive units. Value = fallback grams when the ingredient is
# unknown; per-ingredient overrides live in PIECE_WEIGHTS below.
COUNT_UNITS_DEFAULT_G = {
    "buah": 60.0, "bh": 60.0, "biji": 15.0, "bji": 15.0,
    "butir": 55.0, "btr": 55.0,
    "siung": 4.0, "ekor": 300.0,
    "ptg": 50.0, "potong": 50.0, "iris": 15.0,
    "lembar": 3.0, "lbr": 3.0, "lmbr": 3.0, "helai": 3.0, "daun": 3.0,
    "batang": 20.0, "btg": 20.0, "tangkai": 10.0,
    "ruas": 10.0, "jempol": 12.0, "jari": 10.0,
    "ikat": 100.0, "genggam": 40.0, "gepok": 100.0,
    "papan": 200.0, "tepak": 200.0, "kotak": 100.0, "keping": 20.0, "lonjor": 150.0,
    "bungkus": 50.0, "bks": 50.0, "sachet": 8.0, "saset": 8.0, "renteng": 60.0,
    "kaleng": 400.0, "botol": 300.0, "pcs": 40.0, "pc": 40.0,
    "kepala": 250.0, "bonggol": 300.0, "pack": 200.0, "paket": 200.0,
    "cm": 2.5,             # rhizome length: ~2.5 g per cm (jahe/lengkuas/kunyit)
    "jumput": 1.0, "pinch": 0.5,
}

UNIT_ALIASES = {
    "sendok-makan": "sendok makan", "sendok_makan": "sendok makan",
    "sendok-teh": "sendok teh", "sdmk": "sdm", "lt": "liter",
}

# Abbreviations collapse onto one canonical spelling so that PIECE_WEIGHTS and
# the density table only ever need a single key per unit.
CANONICAL_UNIT = {
    "bh": "buah", "bji": "biji", "btr": "butir", "btg": "batang",
    "ptg": "potong", "lbr": "lembar", "lmbr": "lembar", "helai": "lembar",
    "daun": "lembar", "bks": "bungkus", "gls": "gelas", "saset": "sachet",
    "pc": "pcs", "paket": "pack", "jari": "ruas", "jempol": "ruas", "tepak": "papan",
    "sendok makan": "sdm", "sendok teh": "sdt", "sendok": "sdm", "sdk": "sdm",
    "tbsp": "sdm", "tsp": "sdt", "cup": "gelas", "cangkir": "gelas",
    "mangkuk": "mangkok", "gram": "g", "gr": "g", "grm": "g", "grams": "g",
    "kilo": "kg", "kilogram": "kg", "kgs": "kg", "ltr": "liter",
    "litre": "liter", "l": "liter", "mililiter": "ml", "cc": "ml",
    "hg": "ons", "gepok": "ikat",
}

_ALL_UNITS = set(MASS_UNITS_G) | set(VOLUME_UNITS_ML) | set(COUNT_UNITS_DEFAULT_G)
# Longest-first so "sendok makan" wins over "sendok".
_UNIT_PATTERN = re.compile(
    r"^(" + "|".join(sorted((re.escape(u) for u in _ALL_UNITS),
                            key=len, reverse=True)) + r")\b\.?",
    re.IGNORECASE,
)


def parse_unit(text: str) -> tuple[Optional[str], str]:
    """Read a leading unit token. Returns (canonical_unit, remaining_text)."""
    s = str(text).lstrip()
    low = s.lower()
    for alias, canon in UNIT_ALIASES.items():
        if low.startswith(alias):
            return canon, s[len(alias):].lstrip()
    m = _UNIT_PATTERN.match(s)
    if m:
        unit = m.group(1).lower()
        return CANONICAL_UNIT.get(unit, unit), s[m.end():].lstrip()
    return None, s


# ---------------------------------------------------------------- densities

# Grams per millilitre, by ingredient keyword. Applied when a volume unit is
# used. Default 1.0 (water-like) is correct for the dominant cases (air, kaldu,
# santan cair) and close enough for the rest.
DENSITY_G_PER_ML = {
    "minyak": 0.92, "margarin": 0.95, "mentega": 0.95, "santan": 1.00,
    "kecap": 1.15, "saus": 1.10, "saos": 1.10, "madu": 1.42, "sirup": 1.30,
    "gula pasir": 0.85, "gula": 0.85, "gula merah": 0.90, "garam": 1.20,
    "tepung terigu": 0.53, "terigu": 0.53, "maizena": 0.55, "tepung beras": 0.60,
    "tepung sagu": 0.60, "tepung tapioka": 0.60, "tepung": 0.55,
    "susu bubuk": 0.50, "susu": 1.03, "beras": 0.85, "air": 1.00,
    "merica": 0.45, "lada": 0.45, "ketumbar": 0.40, "kunyit bubuk": 0.45,
    "kaldu bubuk": 0.60, "penyedap": 0.70, "ragi": 0.55, "baking": 0.90,
    "cuka": 1.01, "kecap asin": 1.20, "kecap manis": 1.25, "wijen": 0.60,
}

# Weight of one piece, keyed by (unit, ingredient keyword). These are the
# lookups the dossier flags as unavoidable: countable units vary by item.
PIECE_WEIGHTS: dict[tuple[str, str], float] = {
    # eggs
    ("butir", "telur ayam"): 60.0, ("butir", "telur"): 60.0,
    ("butir", "telur bebek"): 70.0, ("butir", "telur puyuh"): 10.0,
    ("buah", "telur"): 60.0,
    # aromatics
    ("siung", "bawang putih"): 4.0, ("siung", "bawang merah"): 8.0,
    ("buah", "bawang merah"): 8.0, ("buah", "bawang putih"): 4.0,
    ("butir", "bawang merah"): 8.0, ("butir", "bawang putih"): 4.0,
    ("butir", "kemiri"): 4.0, ("buah", "kemiri"): 4.0,
    ("batang", "serai"): 15.0, ("batang", "sereh"): 15.0,
    ("batang", "daun bawang"): 15.0, ("batang", "seledri"): 8.0,
    ("lembar", "daun salam"): 0.5, ("lembar", "daun jeruk"): 0.3,
    ("lembar", "daun pandan"): 2.0, ("lembar", "daun pisang"): 20.0,
    ("lembar", "daun kol"): 20.0, ("lembar", "daun kubis"): 20.0,
    ("ruas", "jahe"): 10.0, ("ruas", "lengkuas"): 12.0, ("ruas", "kunyit"): 8.0,
    ("ruas", "kencur"): 6.0, ("cm", "jahe"): 2.5, ("cm", "lengkuas"): 3.0,
    ("cm", "kunyit"): 2.0, ("cm", "kencur"): 1.8,
    # chillies
    ("buah", "cabe"): 5.0, ("buah", "cabai"): 5.0,
    ("buah", "cabe rawit"): 1.5, ("buah", "cabai rawit"): 1.5,
    ("buah", "cabe merah"): 8.0, ("buah", "cabai merah"): 8.0,
    ("buah", "cabe keriting"): 5.0, ("buah", "cabai keriting"): 5.0,
    ("biji", "cabe"): 5.0, ("biji", "cabai"): 5.0,
    ("biji", "cabe rawit"): 1.5, ("biji", "cabai rawit"): 1.5,
    # vegetables & fruit
    ("buah", "tomat"): 90.0, ("buah", "wortel"): 70.0,
    ("buah", "kentang"): 110.0, ("buah", "jagung"): 200.0,
    ("buah", "timun"): 120.0, ("buah", "mentimun"): 120.0,
    ("buah", "terong"): 90.0, ("buah", "labu siam"): 200.0,
    ("buah", "jeruk nipis"): 30.0, ("buah", "jeruk limau"): 15.0,
    ("buah", "jeruk"): 100.0, ("buah", "pisang"): 80.0,
    ("buah", "kelapa"): 400.0, ("buah", "alpukat"): 200.0,
    ("buah", "apel"): 150.0, ("buah", "mangga"): 200.0,
    ("buah", "pepaya"): 800.0, ("buah", "semangka"): 2000.0,
    ("ikat", "bayam"): 100.0, ("ikat", "kangkung"): 120.0,
    ("ikat", "sawi"): 150.0, ("ikat", "kemangi"): 30.0,
    ("ikat", "daun singkong"): 150.0,
    ("bonggol", "kol"): 800.0, ("buah", "kol"): 800.0,
    ("buah", "brokoli"): 300.0, ("buah", "sawi"): 150.0,
    # soy products
    ("papan", "tempe"): 200.0, ("papan", "tahu"): 200.0, ("potong", "tempe"): 25.0,
    ("buah", "tempe"): 25.0, ("keping", "tempe"): 50.0,
    ("buah", "tahu"): 100.0, ("kotak", "tahu"): 100.0,
    ("potong", "tahu"): 50.0, ("biji", "tahu"): 50.0,
    ("bungkus", "tempe"): 200.0,
    # protein
    ("ekor", "ayam"): 1000.0, ("ekor", "ikan"): 300.0,
    ("ekor", "ikan bandeng"): 400.0, ("ekor", "ikan kembung"): 100.0,
    ("ekor", "lele"): 200.0, ("ekor", "udang"): 15.0,
    ("ekor", "cumi"): 120.0, ("ekor", "bebek"): 1300.0,
    ("potong", "ayam"): 90.0, ("potong", "ikan"): 80.0,
    ("potong", "daging"): 50.0, ("buah", "sosis"): 40.0,
    ("buah", "bakso"): 15.0, ("biji", "bakso"): 15.0,
    ("kepala", "ikan"): 250.0,
    # dry goods
    ("bungkus", "mie"): 70.0, ("bungkus", "mie instan"): 70.0,
    ("sachet", "kaldu"): 8.0, ("sachet", "santan"): 65.0,
    ("bungkus", "santan"): 65.0, ("buah", "santan"): 65.0,
    ("lembar", "kulit lumpia"): 8.0, ("lembar", "roti"): 25.0,
    ("lonjor", "lontong"): 150.0,
}

# Nominal grams for "secukupnya" lines, by ingredient class. Deliberately small:
# the dossier's rule is that an unquantified line must never be allowed to
# invent a large mass.
NOMINAL_G = {
    "garam": 2.0, "gula": 3.0, "merica": 0.5, "lada": 0.5, "penyedap": 1.0,
    "kaldu": 2.0, "minyak": 5.0, "air": 100.0, "kecap": 5.0, "saus": 5.0,
    "saos": 5.0, "bumbu": 2.0, "daun": 1.0, "tepung": 5.0, "santan": 20.0,
    "bawang goreng": 2.0, "cabe": 3.0, "cabai": 3.0, "ketumbar": 0.5,
    "kunyit": 1.0, "jahe": 2.0,
}
NOMINAL_DEFAULT_G = 3.0

# Markers meaning "no measurable quantity".
_UNQUANTIFIED = (
    "secukupnya", "sesuai selera", "sesuai kebutuhan", "seperlunya", "sckpnya",
    "secukup nya", "sesukanya", "kalau suka", "jika suka", "bila suka",
    "to taste", "seadanya", "sesuai kebutuhan",
)
# Markers meaning the cook explicitly skipped the item.
_OMITTED = (
    "saya tidak pakai", "tidak pakai", "gak pakai", "ga pakai", "nggak pakai",
    "saya skip", "boleh diskip", "di skip",
)

_PREP_WORDS = {
    "halus", "iris", "cincang", "geprek", "rajang", "parut", "kupas",
    "rebus", "goreng", "kukus", "haluskan", "diiris", "dicincang", "digeprek",
    "dipotong", "diparut", "dikupas", "direbus", "digoreng", "memarkan",
    "dimemarkan", "serut", "diserut", "buang", "dibuang", "cuci", "dicuci",
    "bersihkan", "dibersihkan", "sesuai", "selera", "secukupnya", "opsional",
    "optional", "matang", "mentah", "segar", "kering", "basah", "utuh",
    "sedang", "sdg", "besar", "bsr", "kecil", "kcl", "muda", "tua",
    "bulat", "panjang", "tipis",
    "tebal", "kasar", "saja", "aja", "yang", "sudah", "untuk", "bisa", "juga",
    "atau", "dan", "dengan", "pakai", "tanpa", "biar", "agar", "supaya",
    "sckpnya", "seadanya", "skip", "saya", "kalau", "jika", "bila", "suka",
    "buat", "dari", "pada", "ini", "itu", "nya", "beri", "tambahkan",
}


def normalize(text: str) -> str:
    """Lowercase, strip accents and punctuation noise, collapse whitespace."""
    s = unicodedata.normalize("NFKD", str(text))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"[^\w\s/.,-]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def clean_name(text: str) -> str:
    """Reduce an ingredient phrase to its head noun phrase.

    "bawang merah, iris tipis (opsional)" -> "bawang merah"
    """
    s = re.sub(r"\([^)]*\)", " ", str(text))
    s = re.split(r"[,;:]|\byg\b|\byang\b|\buntuk\b|\bbuat\b", s, maxsplit=1)[0]
    s = normalize(s)
    s = re.sub(r"\b\d+(?:[.,]\d+)?\b", " ", s)
    s = re.sub(r"[/.-]+", " ", s)
    tokens = [t for t in s.split() if len(t) > 1 and t not in _PREP_WORDS]
    cleaned = " ".join(tokens).strip(" .,-")
    return cleaned or normalize(text).strip(" .,-")


def _lookup(table: dict, name: str, unit: Optional[str] = None):
    """Longest-keyword-wins lookup against an ingredient name."""
    best_key, best_len = None, 0
    for key in table:
        kw = key[1] if isinstance(key, tuple) else key
        if unit is not None and isinstance(key, tuple) and key[0] != unit:
            continue
        if kw in name and len(kw) > best_len:
            best_key, best_len = key, len(kw)
    return table[best_key] if best_key is not None else None


# ---------------------------------------------------------------- result


@dataclass
class ParsedIngredient:
    """One ingredient line resolved to grams, with its provenance."""

    raw: str
    name: str                     # cleaned head noun phrase
    quantity: Optional[float]
    unit: Optional[str]
    grams: float                  # gross / as-purchased grams, before BDD
    confidence: Confidence
    rule: str                     # which rule produced `grams` (auditable)
    unquantified: bool = False
    omitted: bool = False
    notes: list[str] = field(default_factory=list)

    def as_row(self) -> dict:
        return {
            "raw": self.raw, "name": self.name, "quantity": self.quantity,
            "unit": self.unit, "grams": round(self.grams, 2),
            "confidence": self.confidence.value, "rule": self.rule,
            "unquantified": self.unquantified, "omitted": self.omitted,
        }


def parse_line(line: str) -> ParsedIngredient:
    """Convert one raw recipe ingredient line into grams.

    The resolution order is strictly most-certain-first: explicit mass, then
    volume x density, then countable-unit lookup, then a nominal amount for
    "secukupnya", and finally an unresolved marker.
    """
    raw = str(line).strip()
    low = normalize(raw)

    if not low:
        return ParsedIngredient(raw, "", None, None, 0.0,
                                Confidence.UNRESOLVED, "empty")

    omitted = any(m in low for m in _OMITTED)
    qty, rest = parse_quantity(raw)
    unit, rest = parse_unit(rest)

    # "2 bungkus (65 g) santan" - prefer an explicit mass in parentheses.
    paren_mass = re.search(r"\((\d+(?:[.,]\d+)?)\s*(gr|gram|g|ml|kg)\b\)",
                           raw, re.IGNORECASE)

    name = clean_name(rest if (qty is not None or unit) else raw)
    unquantified = any(m in low for m in _UNQUANTIFIED)

    # 1. explicit mass unit
    if unit in MASS_UNITS_G and qty is not None:
        grams = qty * MASS_UNITS_G[unit]
        return ParsedIngredient(raw, name, qty, unit, grams,
                                Confidence.EXACT, f"mass:{unit}", omitted=omitted)

    # 2. volume unit x density
    if unit in VOLUME_UNITS_ML and qty is not None:
        ml = qty * VOLUME_UNITS_ML[unit]
        density = _lookup(DENSITY_G_PER_ML, name) or 1.0
        return ParsedIngredient(raw, name, qty, unit, ml * density,
                                Confidence.VOLUME, f"volume:{unit}x{density}",
                                omitted=omitted)

    # 3. countable unit
    if unit in COUNT_UNITS_DEFAULT_G and qty is not None:
        if paren_mass:
            grams = float(paren_mass.group(1).replace(",", ".")) * qty
            return ParsedIngredient(raw, name, qty, unit, grams,
                                    Confidence.EXACT, "mass:parenthetical",
                                    omitted=omitted)
        per_piece = _lookup(PIECE_WEIGHTS, name, unit)
        if per_piece is not None:
            return ParsedIngredient(raw, name, qty, unit, qty * per_piece,
                                    Confidence.PIECE_KNOWN, f"piece:{unit}",
                                    omitted=omitted)
        per_piece = COUNT_UNITS_DEFAULT_G[unit]
        return ParsedIngredient(raw, name, qty, unit, qty * per_piece,
                                Confidence.PIECE_DEFAULT, f"piece_default:{unit}",
                                omitted=omitted)

    # 4. a bare number with no unit: "2 tahu", "5 cabe" - implied count
    if qty is not None and unit is None:
        per_piece = _lookup(PIECE_WEIGHTS, name, "buah")
        if per_piece is not None:
            return ParsedIngredient(raw, name, qty, "buah", qty * per_piece,
                                    Confidence.PIECE_KNOWN, "piece:implied_buah",
                                    omitted=omitted)
        return ParsedIngredient(raw, name, qty, None,
                                qty * COUNT_UNITS_DEFAULT_G["buah"],
                                Confidence.PIECE_DEFAULT, "piece_default:implied",
                                omitted=omitted)

    # 5. unquantified -> small nominal amount, flagged
    nominal = _lookup(NOMINAL_G, name)
    grams = nominal if nominal is not None else NOMINAL_DEFAULT_G
    return ParsedIngredient(raw, name, None, unit, grams, Confidence.NOMINAL,
                            "nominal", unquantified=True, omitted=omitted)


def parse_ingredient_block(block: str, sep: str = "|") -> list[ParsedIngredient]:
    """Parse a full recipe ingredient string (pipe-separated in the corpus)."""
    out: list[ParsedIngredient] = []
    for chunk in str(block).split(sep):
        chunk = chunk.strip()
        if not chunk:
            continue
        # section headers like "bumbu halus :" carry no ingredient
        if re.fullmatch(r"(bumbu|bahan|pelengkap|saus|isian)[\s\w]*:?", chunk.lower()):
            continue
        out.append(parse_line(chunk))
    return out
