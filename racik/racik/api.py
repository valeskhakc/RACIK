"""HTTP API and static host for the Racik planner.

Run:  python -m uvicorn racik.api:app --reload --port 8000
Then: http://127.0.0.1:8000
"""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .akg import (ADVISORY_NUTRIENTS, BINDING_NUTRIENTS, NUTRIENT_META,
                  SCHOOL_STAGES, STAGE_LABELS, stage_band)
from .config import (FRONTEND_DIR, MBG, PROJECT_DIR, SLOT_LABELS_EN,
                     SLOT_LABELS_ID, SLOTS, UI_DIR, MBGConfig)
from .i18n import DEFAULT_LANG, LANG_NAMES, LANGS, norm_lang
from .optimizer import MenuOptimizer, PlanRequest, _tray_cache
from .orchestrator import TOOL_SCHEMAS, RacikGenerateError, RacikOrchestrator
from .procurement import aggregate, totals
from .report import build_report
from .serialize import serialise_plan
from .bedrock import make_client
from .sealion import SeaLionClient
from .statedb import StateStore
from .store import RacikStore
from .translate import DishTranslator
from .recipe_text import StepsTranslator

app = FastAPI(title="Racik", version="0.1.0",
              description="AKG-grounded MBG menu planning for Indonesia")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])


@lru_cache(maxsize=1)
def _store() -> RacikStore:
    return RacikStore()


@lru_cache(maxsize=1)
def _optimizer() -> MenuOptimizer:
    return MenuOptimizer(_store().load_dishes())


@lru_cache(maxsize=1)
def _state() -> StateStore:
    return StateStore()


# ---------------------------------------------------------------- schemas


class PlanBody(BaseModel):
    days: int = Field(5, ge=1, le=7)
    stage: str = "sd"
    portions: int = Field(100, ge=1, le=100_000)
    province: str = ""
    island: str = ""
    budget_per_portion_idr: float = Field(MBG.budget_per_portion_idr, gt=0)
    ingredient_budget_share: float = Field(MBG.ingredient_budget_share, gt=0, le=1)
    regional_cost_index: float = Field(1.0, gt=0, le=5)
    exclude_terms: list[str] = []
    exclude_dish_ids: list[int] = []
    enforce_micronutrients: bool = False
    max_solve_seconds: float = Field(12.0, gt=0, le=60)
    random_seed: int = 0
    lang: str = DEFAULT_LANG

    def to_request(self) -> PlanRequest:
        config = MBGConfig(
            budget_per_portion_idr=self.budget_per_portion_idr,
            ingredient_budget_share=self.ingredient_budget_share,
            regional_cost_index=self.regional_cost_index,
        )
        if self.stage not in SCHOOL_STAGES:
            raise HTTPException(400, f"unknown stage '{self.stage}'")
        return PlanRequest(
            days=self.days, stage=self.stage, portions=self.portions,
            province=self.province, island=self.island, config=config,
            exclude_terms=self.exclude_terms,
            exclude_dish_ids=self.exclude_dish_ids,
            enforce_micronutrients=self.enforce_micronutrients,
            max_solve_seconds=self.max_solve_seconds,
            random_seed=self.random_seed,
        )


# ---------------------------------------------------------------- endpoints


@app.get("/api/meta")
def meta() -> dict:
    store = _store()
    provinces = store.provinces()
    # On a CPU-constrained host, scope the demo to only the provinces with
    # a precomputed tray bank — every one of them then generates instantly
    # with guaranteed full AKG compliance, instead of occasionally landing
    # on a province that falls through to a live solve the host is too slow
    # to finish well. Off by default; local dev always sees every province.
    if os.environ.get("RACIK_LIMIT_PROVINCES_TO_CACHE"):
        bank = _tray_cache()
        if bank and bank.get("regions"):
            allowed = set(bank["regions"])
            provinces = [p for p in provinces if p[0] in allowed]
    return {
        "stats": store.stats(),
        "stages": [
            {
                "key": key, "label": STAGE_LABELS[key],
                "targets": {
                    n: round(v, 1)
                    for n, v in stage_band(key).per_meal(MBG.akg_fraction).items()
                },
            }
            for key in SCHOOL_STAGES
        ],
        "provinces": [
            {"province": p, "island": i, "dishes": n}
            for p, i, n in provinces
        ],
        "slots": [
            {"key": s, "label_id": SLOT_LABELS_ID[s], "label_en": SLOT_LABELS_EN[s]}
            for s in SLOTS
        ],
        "nutrients": [
            {
                "key": key, "label_id": meta_[0], "label_en": meta_[1],
                "unit": meta_[2], "status": meta_[3],
                "binding": key in BINDING_NUTRIENTS,
                "advisory": key in ADVISORY_NUTRIENTS,
            }
            for key, meta_ in NUTRIENT_META.items()
        ],
        "languages": [{"code": c, "name": LANG_NAMES[c]} for c in LANGS],
        "default_lang": DEFAULT_LANG,
        "budget": {
            "per_portion_idr": MBG.budget_per_portion_idr,
            "ingredient_share": MBG.ingredient_budget_share,
            "ingredient_idr": MBG.ingredient_budget_idr,
            "akg_fraction": MBG.akg_fraction,
        },
    }


@app.post("/api/plan")
def plan(body: PlanBody) -> dict:
    result = _optimizer().solve(body.to_request())
    if not result.feasible:
        raise HTTPException(
            422, f"no menu could be built (solver status: {result.status})")
    return serialise_plan(result, norm_lang(body.lang))


@app.get("/api/dish/{dish_id}")
def dish(dish_id: int) -> dict:
    store = _store()
    found = store.dish(dish_id)
    if found is None:
        raise HTTPException(404, f"dish {dish_id} not found")
    payload = found.to_dict()
    rewritten = _steps_translator().translate(dish_id, found.steps)
    payload.update(rewritten.to_dict())
    payload["main_food"] = found.main_food_name
    payload["ingredients"] = [
        {
            "raw": line.raw, "name": line.name, "gross_g": round(line.gross_g, 1),
            "edible_g": round(line.edible_g, 1),
            "cost_idr": round(line.cost_idr), "tkpi_code": line.tkpi_code,
            "tkpi_name": line.tkpi_name, "match_method": line.match_method,
            "urt_confidence": line.urt_confidence, "urt_rule": line.urt_rule,
            "unquantified": line.unquantified,
        }
        for line in store.lines(dish_id)
    ]
    return payload


@app.post("/api/procurement")
def procurement(body: PlanBody, day: Optional[int] = None) -> dict:
    result = _optimizer().solve(body.to_request())
    if not result.feasible:
        raise HTTPException(422, "no menu could be built")
    lines = aggregate(result, _store(),
                      day_indexes=[day] if day is not None else None)
    return {
        "portions": body.portions,
        "days": len(result.days) if day is None else 1,
        "totals": totals(lines),
        "lines": [line.to_dict() for line in lines],
    }


@app.post("/api/candidates")
def candidates(body: PlanBody) -> dict:
    """The pre-filter rubric and its effect, without running a solve.

    Answers "why was this dish never considered?" — the gates in order, the
    score formula with its weights, and how many dishes each gate removed.
    """
    return _optimizer().explain_candidates(body.to_request())


class TranslateBody(BaseModel):
    names: list[str] = Field(..., min_length=1, max_length=200)
    use_model: bool = True


@app.post("/api/translate")
def translate_names(body: TranslateBody) -> dict:
    """Gloss Indonesian dish names into English with SEA-LION.

    The gloss is carried alongside the name, never instead of it, and each
    result reports whether it came from the model or the offline glosser.
    """
    glosses = _translator().translate(body.names, use_model=body.use_model)
    return {
        "count": len(glosses),
        "model": _translator().client.model,
        "configured": _translator().client.configured,
        "glosses": [g.to_dict() for g in glosses],
    }


class ReportBody(PlanBody):
    sppg_name: str = ""


@app.post("/api/report")
def report(body: ReportBody) -> HTMLResponse:
    """The printable compliance + procurement document.

    One page an SPPG hands to a supervisor or auditor: the menu, its AKG
    compliance, every relaxed floor, the shopping list, and the methodology
    caveats. Prints to PDF from any browser.
    """
    result = _optimizer().solve(body.to_request())
    if not result.feasible:
        raise HTTPException(422, "no menu could be built")
    lines = aggregate(result, _store())
    html_doc = build_report(result, lines, lang=norm_lang(body.lang),
                            sppg_name=body.sppg_name)
    return HTMLResponse(html_doc)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "dishes": _store().stats()["dishes"]}


# ---------------------------------------------------------------- orchestrator


@lru_cache(maxsize=1)
def _translator() -> DishTranslator:
    return DishTranslator(make_client())


@lru_cache(maxsize=1)
def _steps_translator() -> StepsTranslator:
    # Deliberately hosted SEA-LION (SeaLionClient's own default), not
    # make_client()/Bedrock: the source text is Bahasa Indonesia home-cook
    # notes, and SEA-LION is trained specifically on Southeast Asian
    # languages — a better fit for this rewrite than the general-purpose
    # model driving Agen Gizi/Agen Biaya. Needs SEALION_API_KEY in .env;
    # falls back to the same-language rule-based cleanup without one.
    return StepsTranslator()


@lru_cache(maxsize=1)
def _orchestrator() -> RacikOrchestrator:
    return RacikOrchestrator(_store(), _optimizer(), make_client())


class AskBody(BaseModel):
    question: str = Field(..., min_length=2, max_length=2000)
    history: list[dict] = Field(default_factory=list, max_length=20)
    lang: str = DEFAULT_LANG


@app.get("/api/orchestrator")
def orchestrator_status() -> dict:
    """Which model is driving, and whether it is reachable.

    `describe()`'s own "provider" field already names which of the five
    supported providers (openai/anthropic/bedrock/bedrock-sealion-import/
    sealion) is live — "mode" here is deliberately provider-agnostic rather
    than a second, easily-stale label for the same fact.
    """
    client = _orchestrator().client
    return {
        **client.describe(),
        "tools": [t["function"]["name"] for t in TOOL_SCHEMAS],
        "max_steps": _orchestrator().max_steps,
        "mode": "llm" if client.configured else "fallback",
        "hint": ("Racik is answering with a rule-based planner, not an LLM. "
                 "Set RACIK_LLM_PROVIDER plus the matching credentials "
                 "(see racik/README.md's 'Choosing an LLM provider') to "
                 "enable one." if not client.configured else
                 f"Live via {client.describe()['provider']}."),
    }


@app.post("/api/ask")
def ask(body: AskBody) -> dict:
    """Natural-language planning, orchestrated by SEA-LION.

    Numbers in the answer are produced by the deterministic engine; the returned
    `trace` lists every tool call that produced them.
    """
    result = _orchestrator().ask(body.question, body.history,
                                 lang=norm_lang(body.lang))
    return result.to_dict()


# ---------------------------------------------------------------- generate pipeline
#
# The workflow the operator actually drives from the styled frontend:
# preprocessing -> Agen Gizi -> Agen Biaya -> CP-SAT -> Validator -> menu plan,
# with the result persisted so Operator review and Menu history (below) have
# something to act on. See orchestrator.RacikOrchestrator.generate().


class GenerateBody(BaseModel):
    days: int = Field(5, ge=1, le=7)
    portions: int = Field(100, ge=1, le=100_000)
    province: str = ""
    stage: str = ""                     # "" = let Agen Gizi infer (defaults to "sd")
    budget_mode: str = "total"          # "total" | "per_portion"
    budget_value: float = Field(MBG.budget_per_portion_idr, gt=0)
    notes: str = ""                     # free-text allergy/preference notes
    exclude_dish_ids: list[int] = []    # operator vetoes carried from a prior run
    history_days: int = Field(14, ge=0, le=60)   # rotation/decay window
    lang: str = DEFAULT_LANG


@app.post("/api/generate")
def generate(body: GenerateBody) -> dict:
    if body.budget_mode not in ("total", "per_portion"):
        raise HTTPException(400, "budget_mode must be 'total' or 'per_portion'")
    try:
        result = _orchestrator().generate(
            days=body.days, portions=body.portions, province=body.province,
            stage=body.stage or None, budget_mode=body.budget_mode,
            budget_value=body.budget_value, notes_text=body.notes,
            exclude_dish_ids=body.exclude_dish_ids,
            history_days=body.history_days, lang=norm_lang(body.lang),
            state=_state())
    except RacikGenerateError as exc:
        raise HTTPException(422, str(exc)) from exc
    return result.to_dict()


# ---------------------------------------------------------------- operator review


class ReviewBody(BaseModel):
    run_id: int
    day_index: int
    decision: str            # "accept" | "reject"
    reason: str = ""
    replace: bool = False    # solve a one-day replacement for a rejected day
    lang: str = DEFAULT_LANG


@app.post("/api/review")
def review(body: ReviewBody) -> dict:
    """Persist an accept/reject decision. A reject can ask for a same-day
    replacement, solved with the rejected dish excluded — "operator review"
    feeding straight back into the CP-SAT solver, per the workflow diagram."""
    if body.decision not in ("accept", "reject"):
        raise HTTPException(400, "decision must be 'accept' or 'reject'")
    state = _state()
    state.record_review(body.run_id, body.day_index, body.decision, body.reason)
    response: dict = {"ok": True}
    if body.decision == "reject" and body.replace:
        try:
            response["replacement_day"] = _orchestrator().replace_day(
                run_id=body.run_id, day_index=body.day_index,
                lang=norm_lang(body.lang), state=state)
        except RacikGenerateError as exc:
            raise HTTPException(422, str(exc)) from exc
    return response


@app.get("/api/history")
def history(limit: int = 30) -> dict:
    """Recently served dishes (menu history / rotation) and rejection reasons
    (feedback the Settings tab shows, and Agen Gizi reads on the next run)."""
    state = _state()
    return {
        "recent_served": state.recent_served(limit=limit),
        "rejections": state.rejection_log(limit=limit),
    }


# ---------------------------------------------------------------- stock & audit


class StockBody(BaseModel):
    run_id: int
    day_index: int
    ingredient_name: str
    received_pct: Optional[float] = Field(None, ge=0, le=200)
    used_pct: Optional[float] = Field(None, ge=0, le=200)
    actual_cost_idr: Optional[float] = Field(None, ge=0)
    note: Optional[str] = None


@app.post("/api/stock")
def stock_upsert(body: StockBody) -> dict:
    _state().upsert_stock(
        body.run_id, body.day_index, body.ingredient_name,
        received_pct=body.received_pct, used_pct=body.used_pct,
        actual_cost_idr=body.actual_cost_idr, note=body.note)
    return {"ok": True}


@app.get("/api/stock")
def stock_list(run_id: int) -> dict:
    return {"run_id": run_id, "records": _state().stock_for_run(run_id)}


# ---------------------------------------------------------------- static UI
#
# The styled operator frontend (root index.html/css/js) is the primary UI;
# the plainer Indonesian reference implementation stays reachable at /legacy.


class _NoCacheStaticFiles(StaticFiles):
    """Serves ETag/Last-Modified as usual but forces revalidation on every
    request. Without this, browsers apply heuristic caching to JS/CSS with
    no Cache-Control header and can keep serving a stale frontend build
    indefinitely after a redeploy or local edit."""

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


if FRONTEND_DIR.exists() and (FRONTEND_DIR / "index.html").exists():
    if (FRONTEND_DIR / "css").exists():
        app.mount("/css", _NoCacheStaticFiles(directory=FRONTEND_DIR / "css"),
                 name="frontend-css")
    if (FRONTEND_DIR / "js").exists():
        app.mount("/js", _NoCacheStaticFiles(directory=FRONTEND_DIR / "js"),
                 name="frontend-js")
    if (FRONTEND_DIR / "assets").exists():
        app.mount("/assets", _NoCacheStaticFiles(directory=FRONTEND_DIR / "assets"),
                 name="frontend-assets")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(FRONTEND_DIR / "index.html",
                            headers={"Cache-Control": "no-cache"})

elif UI_DIR.exists():
    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(UI_DIR / "index.html")

if UI_DIR.exists():
    app.mount("/legacy", StaticFiles(directory=UI_DIR, html=True), name="legacy-ui")


_ARCHITECTURE_SVG = PROJECT_DIR / "docs" / "architecture.svg"

if _ARCHITECTURE_SVG.exists():
    @app.get("/architecture.svg", include_in_schema=False)
    def architecture() -> FileResponse:
        """The system diagram, served from docs/ so there is one copy of it."""
        return FileResponse(_ARCHITECTURE_SVG, media_type="image/svg+xml")
