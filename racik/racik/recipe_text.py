"""Recipe steps: scraped Indonesian home-cook notes -> professional English.

The corpus's step-by-step text is scraped from a recipe-sharing site, written
for a home cook chatting with followers, not a school kitchen following an
SOP: stray ">>" paragraph markers, asides like "Tadaaa" or "Lumayan bisa buat
sarapan", inconsistent punctuation. This module rewrites it into clear,
numbered, professional English instructions for the "How to Prepare" panel —
the one place in the app a kitchen operator actually reads prose, as opposed
to a translated label.

Same shape as translate.py's DishTranslator: cached per dish (a second view
costs nothing), degrades to a same-language cleanup with no model configured,
and always reports which of the two produced the result so the UI can be
honest about it. A later pass can reuse this system prompt's structure for a
professional *Indonesian* rewrite once the English version is settled — see
the module docstring note in translate.py for the same design rationale.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .config import DATA_DIR
from .sealion import SeaLionClient, SeaLionError

CACHE_PATH = DATA_DIR / "recipe_steps_en.json"
MAX_STEPS = 10

SYSTEM_PROMPT = """\
You rewrite Indonesian home-cook recipe notes into professional English \
cooking instructions for kitchen staff preparing school meals at scale.

Rules:
- Return a JSON object only: {"steps": ["...", "..."]}. No prose, no markdown fence.
- Each array entry is one clear, imperative instruction ("Beat the eggs until \
smooth", not "Then you beat the eggs").
- Preserve every real action, ingredient, quantity, and order from the source. \
Do not invent steps, ingredients, or quantities that are not in the source text.
- Remove social-media chatter that carries no cooking instruction: asides like \
"Tadaaa", "Lumayan buat sarapan", exclamation runs, emoji, ">>" paragraph \
markers, notes to followers. If a line is pure chatter with no instruction, \
drop it entirely rather than translating it.
- Merge fragments that describe one action into one step; split a run-on line \
that covers two actions into two steps.
- Use standard cooking terminology (saute, simmer, drain, marinate) instead of \
a literal word-for-word translation where an English cook would use a \
different verb for the same action.
- At most 10 steps. If the source has more, combine minor ones rather than \
truncating real instructions.
- The output is 100% English. Never leave an Indonesian word (including \
first-person asides like "saya"/"aku", or address terms like "bu"/"mba") \
untranslated in the final text — a professional kitchen SOP has no casual \
first-person voice at all.
"""

_NOISE_LINE = re.compile(
    r"^\W*$|tadaaa+|lumayan|selamat (mencoba|makan)|semoga (suka|berhasil)|"
    r"jangan lupa (like|subscribe|follow)",
    re.IGNORECASE)


def rule_clean(raw: str) -> list[str]:
    """Same-language cleanup with no model available: strips scrape noise and
    splits into sentences. Still Indonesian — never claims to be a translation."""
    text = re.sub(r">>\s*", "", str(raw or ""))
    text = re.sub(r"\s+", " ", text).strip()
    parts = re.split(r"(?<=[.!?])\s+|\n+", text)
    steps = []
    for part in parts:
        part = part.strip(" .!?")
        if not part or _NOISE_LINE.search(part):
            continue
        steps.append(part[:1].upper() + part[1:])
    return steps[:MAX_STEPS]


@dataclass
class RecipeSteps:
    dish_id: int
    steps: list[str] = field(default_factory=list)
    source: str = "rule"          # "llm" | "rule" | "cache"

    def to_dict(self) -> dict:
        return {"steps": self.steps, "steps_source": self.source}


class StepsTranslator:
    """Batched-per-call (one dish at a time — steps are long enough that
    batching several dishes risks the model conflating them), cached to disk."""

    def __init__(self, client: Optional[SeaLionClient] = None,
                 cache_path: Optional[Path] = None):
        self.client = client or SeaLionClient()
        self.cache_path = Path(cache_path) if cache_path else CACHE_PATH
        self.cache: dict[str, list[str]] = self._load_cache()
        self.calls = 0
        self.last_error: Optional[str] = None

    def _load_cache(self) -> dict[str, list[str]]:
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            return {k: v for k, v in data.get("steps", {}).items()
                    if isinstance(v, list)}
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(
                {"note": "LLM-rewritten professional English recipe steps. "
                         "Delete an entry (or the file) to re-translate it.",
                 "steps": self.cache}, ensure_ascii=False, indent=1),
                encoding="utf-8")
        except OSError:
            pass

    def translate(self, dish_id: int, raw_steps: str,
                  use_model: bool = True) -> RecipeSteps:
        key = str(dish_id)
        if key in self.cache:
            return RecipeSteps(dish_id, self.cache[key], "cache")

        if use_model and self.client.configured and str(raw_steps or "").strip():
            fetched = self._ask_model(raw_steps)
            if fetched:
                self.cache[key] = fetched
                self._save()
                return RecipeSteps(dish_id, fetched, "llm")

        return RecipeSteps(dish_id, rule_clean(raw_steps), "rule")

    def _ask_model(self, raw_steps: str) -> list[str]:
        try:
            reply = self.client.chat(
                [{"role": "system", "content": SYSTEM_PROMPT},
                 {"role": "user", "content":
                     f"Rewrite these recipe notes:\n{raw_steps}"}],
                # SEA-LION's hosted model (Qwen-SEA-LION-v4.5-27B-IT) is a
                # reasoning model: by default it spends tokens on a
                # "reasoning_content" scratchpad before the final answer, and
                # that scratchpad counts against max_tokens — a longer/messier
                # source recipe (several sub-recipes bundled in one) made it
                # think long enough to exhaust even a 3000-token budget and
                # return empty content. This task needs a direct rewrite, not
                # visible reasoning, so turn thinking off entirely rather than
                # chase an ever-larger token ceiling — confirmed on the exact
                # recipe that failed: 234 tokens, correct output, no
                # truncation risk left.
                temperature=0.0, max_tokens=1500,
                chat_template_kwargs={"enable_thinking": False})
            self.calls += 1
        except SeaLionError as exc:
            self.last_error = str(exc)
            return []

        parsed = _parse_steps_object(reply.content)
        return [str(s).strip() for s in parsed if str(s).strip()][:MAX_STEPS]


def _parse_steps_object(text: str) -> list:
    if not text:
        return []
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(),
                     flags=re.MULTILINE).strip()
    try:
        value = json.loads(cleaned)
    except ValueError:
        match = re.search(r"\{.*\}", cleaned, re.S)
        value = None
        if match:
            try:
                value = json.loads(match.group(0))
            except ValueError:
                pass
        if value is None:
            # The model has occasionally dropped just the wrapping object's
            # closing "}" (confirmed live: valid steps array, `{"steps": [...]`
            # with no final brace) — the array itself is still well-formed, so
            # extract and parse that directly rather than losing the whole
            # reply to one missing character.
            arr = re.search(r"\[.*\]", cleaned, re.S)
            if not arr:
                return []
            try:
                value = json.loads(arr.group(0))
            except ValueError:
                return []
    if isinstance(value, dict):
        return value.get("steps") or []
    return value if isinstance(value, list) else []
