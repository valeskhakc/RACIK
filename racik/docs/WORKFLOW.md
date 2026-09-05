# RACIK — how the pipeline works

RACIK plans weekly menus for MBG (Makan Bergizi Gratis) school kitchens: cost,
nutrition, and regional flavor solved together, with two AI agents advising a
deterministic optimizer, and every operator decision feeding back into future
plans. This is the technical reference; `CODE_MAP.md` covers file-by-file
layout.

## Pipeline, in order

1. **Database** — `racik/data/racik.db`, ~18,000 Indonesian dishes, built
   once by `scripts/build_db.py`. Each dish's ingredient lines are matched
   (where possible) to TKPI 2020 (Tabel Komposisi Pangan Indonesia) for
   nutrition, and priced from a WFP regional price survey via a fallback
   chain (exact commodity → name → ingredient group → generic default).
2. **Preprocessing** — `optimizer.py`'s G1–G5 gates filter by AKG stage
   band, region, tray slot, and excluded dish IDs/terms (from operator
   history), narrowing ~18,000 dishes to a few hundred real candidates
   before the solver ever runs.
3. **Agen Gizi** (nutrition agent) and **4. Agen Biaya** (budget agent) run
   **in parallel** (`ThreadPoolExecutor`, `orchestrator.py`), each a single
   structured tool call to AWS Bedrock (`anthropic.claude-3-haiku`):
   - *Agen Gizi* reads the school stage, free-text allergy/preference notes,
     and the operator's last 20 rejections (dish + reason). It never
     computes nutrition — it outputs nutrient-floor emphasis and
     `exclude_terms` (concrete ingredient words only, e.g. `"udang"`, never
     an abstract reason like "too expensive").
   - *Agen Biaya* resolves the true per-portion ingredient budget and a real
     regional cost index from the WFP survey table (not guessed).
   - Both degrade to a deterministic rule-based path with zero downtime if
     Bedrock is unreachable — the pipeline never breaks for lack of a key.
5. **CP-SAT solver** (OR-Tools, `optimizer.py`) is the only component that
   computes real numbers — grams, Rupiah, kcal — across 8 parallel search
   workers. Five macro nutrients (energy, protein, fat, carbohydrate,
   fibre) are binding: each must reach 100% of target or the shortfall is
   reported as a relaxation, never hidden. Spend is pulled toward (not past)
   the budget ceiling via symmetric slack variables. Six micronutrients
   (calcium, iron, zinc, vitamin A, vitamin C, sodium) are computed and
   shown for information only — AKG 2019 disagrees on their values across
   sources, so they're never enforced as constraints.
6. **Validator** — a deterministic recheck (not the LLM) of the solved plan
   against AKG 2019 and the budget, producing the plain-language pass/fail
   note attached to every run's trace.
7. **Menu plan** — serialized per day: dish assignments, nutrient/cost
   breakdown, aggregated procurement. English dish-name glosses are fetched
   on demand and cached, never replacing the Indonesian name.
8. **Operator review** — accept or reject each day, with a reason chip on
   reject. A reject immediately triggers `replace_day()`: a single-day
   re-solve excluding that day's dishes, so a real alternative appears in
   seconds without regenerating the week.
9. **Menu history** — every serve and every review is persisted to SQLite
   (`served_log`, `day_reviews`), and feeds two things back into every
   *future* full generate:
   - **Deterministic**: rejected dish IDs, plus a 14-day rotation window of
     recently-served dishes, are hard-excluded from the candidate pool —
     this guarantee holds even when Bedrock is down.
   - **LLM pattern-detection**: Agen Gizi's prompt includes the last 20
     rejections with their reasons and is explicitly instructed to look for
     a *pattern* across them (e.g. repeatedly-rejected spicy dishes) and
     propose a broader `exclude_term`, not just repeat the individual
     dish-level exclusion the deterministic layer already guarantees.

## Design principles

- **The model orchestrates, never calculates.** Every number an operator
  sees comes from the CP-SAT solver or TKPI/price data, never an LLM guess.
  Agents only emit configuration knobs (exclude terms, budget shares) and
  rationale text.
- **Never hide a shortfall.** A relaxed nutrient floor, an unmatched
  ingredient's rougher cost estimate, a rule-based fallback in place of a
  live model — all surface in the UI rather than being smoothed over.
- **Graceful degradation.** The whole pipeline runs with zero API keys
  (rule-based agents throughout) and degrades silently, per-call, from LLM
  to rule-based on any provider error — never a broken plan for lack of a
  key or a expired token.

## Frontend surfaces

- **Onboarding** — location (resolved to one of 33 real corpus provinces),
  duration, budget, people, notes.
- **Meal Plan tab** — the weekly tray, day detail with full recipe steps,
  accept/reject, week-by-week pagination for multi-week plans.
- **Procurement tab** — aggregated weekly/daily shopping list, CSV export.
- **Settings tab** — program parameters (edit and regenerate), the real
  persisted feedback log.
- **Exports** — PDF and image capture the meal plan itself; CSV (cost data)
  lives on the Procurement tab.

## Stack

FastAPI + SQLite backend · vanilla JS frontend (no framework) · AWS Bedrock
(Claude Haiku) for Agen Gizi/Agen Biaya · a separate SEA-LION client for
recipe-step rewriting · OR-Tools CP-SAT for the solver · html2canvas + jsPDF
for exports. 202 backend tests cover the optimizer, agents, and API surface.
