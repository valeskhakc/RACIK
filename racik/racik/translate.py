"""Indonesian dish names -> English glosses, via SEA-LION.

Dish names are the one place in Racik where translation is genuinely wanted:
an international reader cannot tell whether "Bacem Tahu Tempe Telur" is a soup
or a dessert. Everything else in the interface is a label with a fixed
translation; a dish name is prose.

Three rules shape this module:

**A gloss never replaces the name.** The kitchen cooks "Semur Ikan Bandeng"; the
English text is an aid to the reader, carried alongside, never instead. Losing
the Indonesian name would make the procurement list unusable in the kitchen it
was written for.

**Translation is cached and batched.** The hosted SEA-LION API allows 10
requests per minute, so names are batched (40 per call) and every result is
persisted. A second run of the same menu costs nothing.

**It degrades, like everything else here.** With no API key a rule-based glosser
built from Indonesian culinary vocabulary produces a rough gloss, and the source
is always reported as `sealion` or `rule` so a reader knows which they are
looking at. A wrong gloss attributed to a model would be worse than no gloss.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from .config import DATA_DIR
from .sealion import SeaLionClient, SeaLionError

BATCH_SIZE = 40
CACHE_PATH = DATA_DIR / "dish_translations.json"

SYSTEM_PROMPT = """\
You translate Indonesian dish names into short English glosses for an \
international audience reading a school-meal menu.

Rules:
- Return a JSON object only. No prose, no markdown fence.
- Each key is the exact Indonesian name given to you; each value is the English \
gloss.
- A gloss is short (2-6 words) and descriptive: say what the food IS.
  "Semur Ikan Bandeng" -> "Milkfish in sweet soy stew"
  "Tempe Mendoan" -> "Battered fried tempeh"
  "Sayur Bening Bayam" -> "Clear spinach soup"
- Keep well-known Indonesian food words that English menus already use \
(tempeh, tofu, satay, rendang).
- Do not translate place names; keep them ("khas Solo" -> "Solo style").
- Strip cook chatter such as "simple", "anti gagal", "ala saya".
- If a name is already English, return it unchanged.
"""

# ---------------------------------------------------------------- rule glosser

# Indonesian culinary vocabulary, longest phrase first at match time. This is a
# fallback, not a translator: it produces a readable approximation offline and
# is always labelled as such.
_GLOSSARY: dict[str, str] = {
    # methods
    "goreng": "fried", "digoreng": "fried", "bakar": "grilled",
    "panggang": "roasted", "kukus": "steamed", "rebus": "boiled",
    "tumis": "stir-fried", "bacem": "sweet-braised", "semur": "soy stew",
    "opor": "coconut curry", "gulai": "curry", "rendang": "rendang",
    "balado": "chilli-fried", "penyet": "smashed", "kremes": "crispy",
    "mendoan": "battered fried", "pepes": "banana-leaf steamed",
    "asam manis": "sweet and sour", "sambal": "chilli relish",
    "soto": "soup", "sop": "soup", "sup": "soup", "kuah": "in broth",
    "kari": "curry", "woku": "spiced stew", "rica": "spicy",
    "tongseng": "spiced stew", "asem": "tamarind", "asam": "tamarind",
    "crispy": "crispy", "krispi": "crispy", "suwir": "shredded",
    "geprek": "smashed", "dadar": "omelette", "orak arik": "scrambled",
    # proteins
    "ayam": "chicken", "daging": "beef", "sapi": "beef", "kambing": "goat",
    "bebek": "duck", "ikan": "fish", "bandeng": "milkfish",
    "tongkol": "tuna", "cakalang": "skipjack", "teri": "anchovy",
    "lele": "catfish", "nila": "tilapia", "patin": "catfish",
    "kembung": "mackerel", "udang": "prawn", "cumi": "squid",
    "telur": "egg", "telor": "egg", "tempe": "tempeh", "tahu": "tofu",
    "hati": "liver", "ati": "liver", "bakso": "meatball", "ampela": "gizzard",
    # vegetables
    "sayur": "vegetable", "bayam": "spinach", "kangkung": "water spinach",
    "buncis": "green bean", "wortel": "carrot", "kol": "cabbage",
    "kubis": "cabbage", "sawi": "mustard greens", "terong": "aubergine",
    "labu siam": "chayote", "labu": "squash", "jagung": "corn",
    "kacang panjang": "long bean", "tauge": "bean sprout",
    "toge": "bean sprout", "jamur": "mushroom", "kentang": "potato",
    "singkong": "cassava", "daun singkong": "cassava leaf",
    "nangka": "jackfruit", "rebung": "bamboo shoot", "pare": "bitter melon",
    "timun": "cucumber", "tomat": "tomato",
    # staples / other
    "nasi": "rice", "beras": "rice", "mie": "noodles", "mi": "noodles",
    "bihun": "rice vermicelli", "soun": "glass noodles",
    "santan": "coconut milk", "kelapa": "coconut", "serundeng": "coconut floss",
    "kecap": "sweet soy", "bumbu": "spiced", "pedas": "spicy",
    "manis": "sweet", "kuning": "yellow", "merah": "red", "hijau": "green",
    "putih": "white", "khas": "style", "spesial": "special",
    # fruit (the buah slot is served, so these appear on every tray)
    "pepaya": "papaya", "pisang": "banana", "jeruk": "orange",
    "semangka": "watermelon", "melon": "melon", "salak": "snake fruit",
    "rambutan": "rambutan", "mangga": "mango", "nanas": "pineapple",
    "apel": "apple", "jambu biji": "guava", "jambu": "guava",
    "belimbing": "starfruit", "sawo": "sapodilla", "alpukat": "avocado",
    "buah": "fruit", "susu": "milk", "ubi": "sweet potato", "talas": "taro",
}

# Generic terms suppressed when something more specific already matched: a
# gloss should read "Milkfish soy stew", not "Milkfish soy stew fish".
_GENERIC_IF_SPECIFIC = {
    "fish": {"milkfish", "tuna", "skipjack", "anchovy", "catfish",
             "tilapia", "mackerel"},
    "beef": {"goat"},
    "vegetable": {"spinach", "water spinach", "green bean", "carrot",
                  "cabbage", "mustard greens", "aubergine", "chayote",
                  "squash", "corn", "long bean", "bean sprout", "mushroom"},
    "fruit": {"papaya", "banana", "orange", "watermelon", "melon",
              "snake fruit", "rambutan", "mango", "pineapple", "apple",
              "guava", "starfruit", "sapodilla", "avocado"},
}

# Cook chatter that adds nothing for a reader.
_NOISE = re.compile(
    r"\b(simple|simpel|sederhana|enak|mudah|praktis|ala|homemade|kekinian|"
    r"anti gagal|super|no ribet|ekonomis|rumahan|lezat|gurih|mantap|favorit|"
    r"kesukaan|resep|by|untuk|banget|mantul|endes|jadi|yang|saya|aku|bu|mba)\b",
    re.IGNORECASE)

_PHRASES = sorted(_GLOSSARY, key=len, reverse=True)


def rule_gloss(name: str) -> str:
    """Offline approximation of an English gloss. Never claims to be a translation."""
    text = _NOISE.sub(" ", str(name).lower())
    text = re.sub(r"\(.*?\)", " ", text)                 # "(150 g)", "(kids friendly)"
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\b\d+[\d.,]*\s*(?:g|gr|gram|kg|ml|l)?\b", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    matched, consumed = [], text
    for phrase in _PHRASES:                              # longest phrase first
        if re.search(rf"\b{re.escape(phrase)}\b", consumed):
            matched.append(_GLOSSARY[phrase])
            consumed = re.sub(rf"\b{re.escape(phrase)}\b", " ", consumed)

    # Drop a generic term when a specific one from its family is present.
    specific = set(matched)
    matched = [w for w in matched
               if not (w in _GENERIC_IF_SPECIFIC
                       and specific & _GENERIC_IF_SPECIFIC[w])]

    leftover = [w for w in consumed.split() if len(w) > 3][:1]

    seen, words = set(), []
    for word in matched + leftover:
        if word not in seen:
            seen.add(word)
            words.append(word)
    gloss = " ".join(words[:5]).strip()
    return gloss[:1].upper() + gloss[1:] if gloss else str(name)


# ---------------------------------------------------------------- translator


@dataclass
class Gloss:
    name: str          # the Indonesian name, unchanged
    english: str
    method: str        # "sealion" | "rule" | "cache"

    def to_dict(self) -> dict:
        return {"name": self.name, "english": self.english, "method": self.method}


class DishTranslator:
    """Batched, cached Indonesian -> English glossing."""

    def __init__(self, client: Optional[SeaLionClient] = None,
                 cache_path: Optional[Path] = None):
        self.client = client or SeaLionClient()
        self.cache_path = Path(cache_path) if cache_path else CACHE_PATH
        self.cache: dict[str, str] = self._load_cache()
        self.calls = 0
        self.last_error: Optional[str] = None

    def _load_cache(self) -> dict[str, str]:
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            return {k: v for k, v in data.get("glosses", {}).items()
                    if isinstance(v, str)}
        except (OSError, ValueError):
            return {}

    def save(self) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(
                {"note": "SEA-LION dish-name glosses. Delete to re-translate.",
                 "glosses": self.cache}, ensure_ascii=False, indent=1),
                encoding="utf-8")
        except OSError:
            pass                       # a cache that cannot be written is not fatal

    # ------------------------------------------------------------ public
    def translate(self, names: Iterable[str], use_model: bool = True) -> list[Gloss]:
        """Gloss each name, reporting where each gloss came from."""
        wanted = [n for n in dict.fromkeys(str(x) for x in names) if n.strip()]
        missing = [n for n in wanted if n not in self.cache]

        if missing and use_model and self.client.configured:
            for start in range(0, len(missing), BATCH_SIZE):
                batch = missing[start:start + BATCH_SIZE]
                fetched = self._ask_model(batch)
                self.cache.update(fetched)
            self.save()

        out: list[Gloss] = []
        for name in wanted:
            if name in self.cache:
                out.append(Gloss(name, self.cache[name], "cache"))
            else:
                out.append(Gloss(name, rule_gloss(name), "rule"))
        return out

    def _ask_model(self, batch: list[str]) -> dict[str, str]:
        """One batch. Returns {} on any failure — the caller falls back."""
        payload = json.dumps(batch, ensure_ascii=False)
        try:
            reply = self.client.chat(
                [{"role": "system", "content": SYSTEM_PROMPT},
                 {"role": "user", "content":
                     f"Gloss these {len(batch)} dish names:\n{payload}"}],
                temperature=0.0, max_tokens=2000)
            self.calls += 1
        except SeaLionError as exc:
            self.last_error = str(exc)
            return {}

        parsed = _parse_json_object(reply.content)
        if not parsed:
            self.last_error = "model did not return a usable JSON object"
            return {}
        # Only keep keys we actually asked for: a model that invents entries
        # must not pollute the cache.
        return {k: str(v).strip() for k, v in parsed.items()
                if k in set(batch) and str(v).strip()}


def _parse_json_object(text: str) -> dict:
    """Read a JSON object out of a reply, tolerating a markdown fence."""
    if not text:
        return {}
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(),
                     flags=re.MULTILINE).strip()
    try:
        value = json.loads(cleaned)
    except ValueError:
        match = re.search(r"\{.*\}", cleaned, re.S)
        if not match:
            return {}
        try:
            value = json.loads(match.group(0))
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}
