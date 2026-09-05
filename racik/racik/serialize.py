"""Turn a `PlanResult` into the JSON shape both `api.py` and the agent
pipeline in `orchestrator.py` hand back to a client.

Split out of `api.py` so `orchestrator.py`'s `generate()` pipeline can reuse
the exact same serialisation `/api/plan` uses, without `api.py` and
`orchestrator.py` importing each other.
"""
from __future__ import annotations

from .akg import ADVISORY_NUTRIENTS, BINDING_NUTRIENTS, NUTRIENT_META
from .config import MBG, SLOTS
from .i18n import DEFAULT_LANG, norm_lang, t, unit
from .optimizer import PlanResult
from .report import plan_notes

DAY_LABELS = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]
DAY_LABELS_EN = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                 "Saturday", "Sunday"]


def serialise_plan(result: PlanResult, lang: str = DEFAULT_LANG) -> dict:
    lang = norm_lang(lang)
    portions = result.request.portions
    targets = result.targets

    days = []
    for day in result.days:
        slots = {}
        for slot in SLOTS:
            dish_ = day.dishes[slot]
            slots[slot] = None if dish_ is None else {
                **dish_.to_dict(),
                "main_food": dish_.main_food_name,
                "cost_total_idr": round(dish_.cost_per_portion_idr * portions),
                "steps": [s.strip() for s in (dish_.steps or "").split(".")
                          if s.strip()][:8],
            }
        days.append({
            "index": day.day_index,
            "label_id": DAY_LABELS[day.day_index % 7],
            "label_en": DAY_LABELS_EN[day.day_index % 7],
            "cost_per_portion_idr": round(day.cost_per_portion_idr),
            "cost_total_idr": round(day.cost_per_portion_idr * portions),
            "adequacy": round(result.adequacy(day), 3),
            "nutrients": {k: round(v, 2) for k, v in day.nutrients.items()},
            "compliance": compliance_rows(day.nutrients, targets, lang),
            "slots": slots,
        })

    return {
        "summary": result.summary(),
        "band": {"key": result.band.key, "label_id": result.band.label_id,
                 "label_en": result.band.label_en},
        "targets": {k: round(v, 1) for k, v in targets.items()},
        "days": days,
        "relaxations": [
            {"day": r.day_index, "nutrient": r.nutrient,
             "label_id": NUTRIENT_META[r.nutrient][0],
             "label_en": NUTRIENT_META[r.nutrient][1],
             "unit": unit(lang, NUTRIENT_META[r.nutrient][2]),
             "shortfall": round(r.shortfall, 2), "target": round(r.target, 2),
             "share": round(r.share, 3)}
            for r in result.relaxations
        ],
        "notes": plan_notes(result, lang),
        # The single authoritative explanation of how "meets the standard" is
        # decided — same text the printable report shows, so the UI and the
        # report can never tell two different stories about the same plan.
        "compliance_methodology": t(lang, "note.compliance_method",
                                    fraction=f"{MBG.akg_fraction * 100:.0f}%"),
        "lang": lang,
    }


def compliance_rows(nutrients: dict, targets: dict,
                    lang: str = DEFAULT_LANG) -> list[dict]:
    """Per-nutrient attainment, keeping verified and advisory values apart."""
    rows = []
    for key, (label_id, label_en, unit_symbol, status) in NUTRIENT_META.items():
        target = targets.get(key)
        if not target:
            continue
        value = nutrients.get(key, 0.0)
        rows.append({
            "key": key, "label_id": label_id, "label_en": label_en,
            "unit": unit(lang, unit_symbol), "value": round(value, 1),
            "target": round(target, 1),
            "pct": round(value / target * 100),
            "binding": key in BINDING_NUTRIENTS,
            "verified": status == "verified",
        })
    return rows
