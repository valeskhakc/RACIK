# Code map — how to read Racik in an hour

For a new team member, or a judge who wants to check that the claims are real.
Read in this order; each file depends only on the ones above it.

**The one idea to hold onto:** the deterministic engine owns every number, and
the language model owns none of them. Most design decisions below follow from
that, and the codebase is annotated to say so at each point.

---

## Annotation conventions

Comments in this codebase answer **why**, not what — the code already says what.
Three kinds recur, and they are worth recognising:

| Marker | Meaning | Example |
|---|---|---|
| Module docstring | The contract and the trade-off the file makes | `urt.py`: why parsing is rule-based, not an LLM |
| `# ---- section` | A named region inside a long file | `# ---------------- the rubric` in `optimizer.py` |
| Inline rationale | A decision a reader would otherwise undo | "Order matters. A sayur bening with a spoon of teri in it is still the vegetable" |

Where a number is not physics, the comment names its source document — AKG 2019,
Permenkes 41/2014, a BGN statement, FAO/INFOODS. If you find a constant without
a source, that is a bug in the annotation.

---

## Reading order

### 1. `racik/config.py` — what the programme requires *(113 lines)*
Budget, tray composition, reference portions. Everything here is **policy, not
physics**: editable per SPPG and per region. Start here because every later file
reads these.

Look at: `MBGConfig.ingredient_budget_idr` — Rp10,000 × 100% by default (the
operator's budget input is Racik's own food/ingredient budget, not an
all-inclusive program pagu with labour and logistics folded in), and the
comment explaining why that's a distinct thing from BGN's separate ~70/20/10
national-pagu split.

### 2. `racik/akg.py` — what a child needs *(148 lines)*
AKG 2019 age bands. The important structure is the **two tiers of trust**:
`BINDING_NUTRIENTS` (macros, corroborated) versus `ADVISORY_NUTRIENTS` (micros,
where sources disagree). The micros are computed and displayed but never
constrain a solve. This split is the single most load-bearing honesty decision
in the project.

### 3. `racik/urt.py` — household measures to grams *(490 lines)*
The hardest data problem. "2 ons ikan" is 200 g, not 56 g. "1 papan tempe" is
200 g. "secukupnya garam" is 2 g and flagged.

Read `parse_line()` and note the **resolution order**: explicit mass → volume ×
density → per-ingredient piece weight → category fallback → nominal → unresolved.
Every result carries the rule that produced it, which is why the numbers can be
audited. 99.98% of 260,734 lines resolve.

### 4. `racik/tkpi.py` — grams to nutrients *(510 lines)*
Loads TKPI 2020 and resolves free-text ingredient names onto food codes.

Two traps the code exists to avoid, both in the module docstring: TKPI names are
**inverted** (`Sapi, daging, lemak sedang, segar`), and substring matching maps
`ayam` onto `Bayam`. Hence token-set matching plus `IDENTITY_MODIFIERS`, which
stops `daun jeruk` (lime leaf) resolving to orange.

`DISCARDED_AROMATICS` is worth understanding: bay leaf and lemongrass are
costed and appear on the shopping list, but contribute no nutrition, because
they are lifted out before serving.

### 5. `racik/prices.py` — grams to rupiah *(147 lines)*
Cost is charged on **gross purchased weight**, not edible weight. Chicken at
BDD 58% means 75 g on the tray is 129 g bought. `Price.basis` reports whether a
figure came from a real commodity price or a fallback.

### 6. `racik/nutrition.py` — a recipe becomes a dish *(513 lines)*
Where the pipeline joins up. Two approximations are declared openly here and
surfaced through `DishQuality`: **servings are estimated** (the corpus states no
yield) and **retention factors are category-level**.

Read `_apply_frying_absorption()` — a recipe listing 500 ml of deep-frying oil
must not report 460 g of dietary fat. Oil is capped at 10% absorption for
nutrition and costed in full, because the kitchen buys all of it.

### 7. `racik/store.py` — the read layer *(200+ lines)*
Dishes load without their 258k ingredient lines; lines load on demand for the
~25 dishes a solve actually selects. Full load is 0.5 s.

### 8. `racik/optimizer.py` — choosing the menu *(500+ lines)*
CP-SAT. Two things to read carefully:

- **The rubric block** near the top — the five hard gates G1–G5 and the scoring
  formula, stated in one place rather than buried in the loop, because "why was
  this dish never considered?" is a question people actually ask.
  `explain_candidates()` returns the gates, the weights, and how many dishes each
  gate removed.
- **Soft floors.** Every nutrient floor carries penalised slack, so a solve always
  returns a menu plus a list of what it relaxed. "Infeasible" helps no cook.

### 9. `racik/procurement.py` — the shopping list *(77 lines)*
Aggregates on TKPI code, not recipe wording, so "bawang merah", "bamer" and
"5 siung bawang merah" become one purchase-order line.

### 10. `racik/report.py` — the deliverable *(300+ lines)*
The printable compliance + procurement document. Note that it marks advisory
nutrients in every table and gives relaxations their own section: a compliance
document that hides a relaxed floor is worse than no document.

### 11. `racik/sealion.py` + `racik/bedrock.py` — the model *(224 + 250 lines)*
Three deployment paths behind one interface: hosted `api.sea-lion.ai`, Bedrock
Custom Model Import via boto3, or any OpenAI-compatible server. `make_client()`
picks from the environment. Read the `bedrock.py` docstring for the constraints
that are real and verified against AWS docs.

### 12. `racik/orchestrator.py` — the agent *(700+ lines)*
Eight tools over the deterministic engine, plus the loop.

The system prompt's absolute rule is the grounding contract. The loop is
**bounded in code, not in the prompt** — a stop condition that lives only in a
prompt eventually does not stop. Every tool outcome, including failure, lands in
the trace; two bugs were found here because error paths were skipping it.

### 13. `racik/i18n.py`, `racik/translate.py` — language *(146 + 300 lines)*
`i18n.py` holds the prose the server writes. `translate.py` glosses dish names
with SEA-LION, cached and batched, with an offline glosser as fallback — and
always reports which produced a given gloss.

### 14. `racik/api.py` — the surface *(400+ lines)*
Thin. Every endpoint delegates; the serialisation splits verified from advisory
nutrients and always attaches methodology notes, so a caller cannot render the
numbers without their caveats.

---

## Where to look first, by question

| Question | File | Symbol |
|---|---|---|
| Why is this dish 340 kcal? | `nutrition.py` | `RecipeEngine.build` |
| Where did "2 ons" become 200 g? | `urt.py` | `parse_line`, `MASS_UNITS_G` |
| Why wasn't my dish chosen? | `optimizer.py` | `explain_candidates` |
| Why is the menu short on protein? | `optimizer.py` | soft floors, `Relaxation` |
| Why does chicken cost more than it weighs? | `tkpi.py` | `Food.purchase_grams` |
| Can the model invent a number? | `orchestrator.py` | `SYSTEM_PROMPT`, `_invoke` |
| What does the kitchen actually receive? | `report.py` | `build_report` |

## Verifying the claims

```bash
python -m pytest tests -q          # 202 tests, no network, no API key
python scripts/build_db.py         # rebuild every number from source, ~30 s
```

The test suite is the fastest way to check that this document is honest. Each
claim above has a test: the `ons = 100 g` convention, the ayam/bayam trap, oil
absorption, funnel arithmetic, the bounded agent loop, and that the report
discloses relaxations.
