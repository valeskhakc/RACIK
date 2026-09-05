"""Tests for the bilingual layer.

Racik ships one codebase in two languages. What has to hold: no string is
missing from either language, the server's prose actually changes, units follow
the language, and an unknown code degrades to the default rather than leaking
raw keys into the interface.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from racik.i18n import (DEFAULT_LANG, LANG_NAMES, LANGS, STRINGS, norm_lang, t,
                        unit)

UI_HTML = Path(__file__).resolve().parents[1] / "ui" / "index.html"


# ---------------------------------------------------------------- catalogue

def test_both_languages_are_declared():
    assert set(LANGS) == {"id", "en"}
    assert set(LANG_NAMES) == set(LANGS)
    assert DEFAULT_LANG in LANGS


def test_server_catalogues_have_identical_keys():
    """A key present in one language and missing in the other is a silent bug."""
    id_keys, en_keys = set(STRINGS["id"]), set(STRINGS["en"])
    assert id_keys == en_keys, (
        f"only in id: {sorted(id_keys - en_keys)} | "
        f"only in en: {sorted(en_keys - id_keys)}")


def test_no_server_string_is_empty():
    for lang in LANGS:
        for key, value in STRINGS[lang].items():
            assert value.strip(), f"{lang}.{key} is empty"


def test_placeholders_match_across_languages():
    """{amount} in one language must exist in the other, or formatting breaks."""
    pattern = re.compile(r"\{(\w+)\}")
    for key in STRINGS["id"]:
        assert set(pattern.findall(STRINGS["id"][key])) == \
               set(pattern.findall(STRINGS["en"][key])), \
               f"placeholder mismatch on '{key}'"


def test_translations_actually_differ():
    """Guards against a language block copy-pasted and never translated."""
    same = [k for k in STRINGS["id"] if STRINGS["id"][k] == STRINGS["en"][k]]
    assert not same, f"untranslated keys: {same}"


# ---------------------------------------------------------------- lookup

@pytest.mark.parametrize("value,expected", [
    ("id", "id"), ("en", "en"), ("EN", "en"), ("en-GB", "en"),
    ("fr", DEFAULT_LANG), ("", DEFAULT_LANG), (None, DEFAULT_LANG),
])
def test_norm_lang(value, expected):
    assert norm_lang(value) == expected


def test_t_formats_placeholders():
    assert "42" in t("en", "note.overrun", amount="42")


def test_t_falls_back_rather_than_raising():
    assert t("en", "no.such.key") == "no.such.key"
    # a missing placeholder must not blow up a live request
    assert t("en", "note.overrun")


def test_unit_localisation():
    assert unit("id", "kkal") == "kkal"
    assert unit("en", "kkal") == "kcal"
    assert unit("en", "g") == "g"          # unchanged where identical
    assert unit("en", "mcg") == "mcg"


# ---------------------------------------------------------------- API surface

@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from racik.api import app
    return TestClient(app)


def test_meta_advertises_both_languages(client):
    meta = client.get("/api/meta").json()
    assert {l["code"] for l in meta["languages"]} == {"id", "en"}
    assert meta["default_lang"] == DEFAULT_LANG


def test_plan_notes_follow_the_language(client):
    base = {"days": 2, "stage": "sd", "max_solve_seconds": 8}
    id_plan = client.post("/api/plan", json={**base, "lang": "id"}).json()
    en_plan = client.post("/api/plan", json={**base, "lang": "en"}).json()
    assert id_plan["lang"] == "id" and en_plan["lang"] == "en"
    # notes[0] is now the compliance-methodology explanation (see
    # note.compliance_method); the TKPI data-source note follows it.
    assert "TKPI" in id_plan["notes"][1] and "TKPI" in en_plan["notes"][1]
    assert id_plan["notes"][1] != en_plan["notes"][1]
    assert "berat mentah" in id_plan["notes"][1]
    assert "raw-weight" in en_plan["notes"][1]
    assert id_plan["notes"][0] != en_plan["notes"][0]
    assert "AKG" in id_plan["notes"][0] and "AKG" in en_plan["notes"][0]


def test_energy_unit_follows_the_language(client):
    base = {"days": 1, "stage": "sd", "max_solve_seconds": 8}
    for lang, expected in (("id", "kkal"), ("en", "kcal")):
        plan = client.post("/api/plan", json={**base, "lang": lang}).json()
        energy = [r for r in plan["days"][0]["compliance"]
                  if r["key"] == "energy_kcal"][0]
        assert energy["unit"] == expected


def test_compliance_rows_carry_both_labels(client):
    plan = client.post("/api/plan",
                       json={"days": 1, "stage": "sd", "max_solve_seconds": 8}).json()
    row = plan["days"][0]["compliance"][0]
    assert row["label_id"] and row["label_en"]
    assert row["label_id"] != row["label_en"]


def test_ask_answers_in_the_requested_language(client):
    question = "Menu 2 hari untuk SD, 100 porsi"
    id_answer = client.post("/api/ask", json={"question": question,
                                              "lang": "id"}).json()["answer"]
    en_answer = client.post("/api/ask", json={"question": question,
                                              "lang": "en"}).json()["answer"]
    assert id_answer.startswith("Menu")
    assert en_answer.startswith("A ") and "menu for" in en_answer
    assert "portions/day" in en_answer


def test_unknown_language_falls_back_without_error(client):
    plan = client.post("/api/plan", json={"days": 1, "stage": "sd",
                                          "lang": "zz",
                                          "max_solve_seconds": 8}).json()
    assert plan["lang"] == DEFAULT_LANG


# ---------------------------------------------------------------- UI catalogue

def _ui_catalogue(lang: str) -> set[str]:
    """Pull the key set for one language block out of the single-file UI."""
    html = UI_HTML.read_text(encoding="utf-8")
    block = re.search(rf"\n  {lang}: \{{(.*?)\n  }}", html, re.S)
    assert block, f"could not locate the '{lang}' string block in the UI"
    return set(re.findall(r'"([\w.]+)":\s*"', block.group(1)))


def test_ui_language_blocks_have_identical_keys():
    id_keys, en_keys = _ui_catalogue("id"), _ui_catalogue("en")
    assert id_keys == en_keys, (
        f"only in id: {sorted(id_keys - en_keys)} | "
        f"only in en: {sorted(en_keys - id_keys)}")


def test_ui_references_only_defined_keys():
    """Every literal t("…") call and data-i18n attribute must resolve.

    The lookbehind keeps `createElement("a")` out, and requiring a closing
    `,`/`)` right after the literal skips composed keys like t("slot." + slot),
    which are covered by the family test below.
    """
    html = UI_HTML.read_text(encoding="utf-8")
    body = html[html.index("const EXAMPLES"):]
    used = set(re.findall(r'(?<![A-Za-z0-9_.])t\("([\w.]+)"\s*[,)]', body))
    used |= set(re.findall(r'data-i18n(?:-html|-ph|-aria)?="([\w.]+)"', html))
    missing = sorted(used - _ui_catalogue("id"))
    assert not missing, f"UI uses undefined string keys: {missing}"


def test_ui_dynamic_key_families_are_complete():
    """Keys built at runtime — t("slot." + slot), t("n." + nutrient)."""
    from racik.akg import MACRO_NUTRIENTS
    from racik.config import SLOTS

    catalogue = _ui_catalogue("id") & _ui_catalogue("en")
    for slot in SLOTS:
        assert f"slot.{slot}" in catalogue, f"missing UI label for slot '{slot}'"
    for nutrient in MACRO_NUTRIENTS:
        assert f"n.{nutrient}" in catalogue, f"missing UI label for '{nutrient}'"


def test_ui_carries_the_build_marker():
    """The packaging script rewrites this line to set each build's default."""
    html = UI_HTML.read_text(encoding="utf-8")
    assert re.search(r'const DEFAULT_LANG = "(id|en)";\s*/\* BUILD:DEFAULT_LANG \*/',
                     html), "the DEFAULT_LANG build marker is missing or malformed"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
