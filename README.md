---
title: Racik
emoji: 🍚
colorFrom: green
colorTo: yellow
sdk: docker
app_port: 8000
pinned: false
---

# Racik

AI-powered meal planning for Indonesia's **Makan Bergizi Gratis** (MBG) free
school-meal program. An SPPG kitchen operator enters a location, a budget,
a headcount and free-text notes; Racik plans a multi-day menu that meets AKG
2019 nutrient floors under the MBG per-portion budget, explains why each dish
was chosen, and hands back scaled recipes and a shopping list.

This repo has two parts:

- **`racik/`** — the planning engine: an 18k-recipe corpus turned into a
  costed, AKG-scored dish index; a CP-SAT menu optimiser; two LLM-backed
  planning agents (**Agen Gizi** for nutrition, **Agen Biaya** for budget)
  plus a deterministic **Validator**; and persistence for operator review,
  menu-history rotation, and stock/cost audit. Full detail in
  [`racik/README.md`](racik/README.md).
- **`index.html` / `css/` / `js/`** — the operator-facing frontend (the
  styled UI shown in the product mockups), served by the same FastAPI app
  as the primary interface. A plainer Indonesian-language reference
  implementation is also mounted at `/legacy`.

## Quickstart

```bash
cd racik
python -m venv .venv && .venv/Scripts/activate      # or source .venv/bin/activate on Linux/Mac
pip install -r requirements.txt boto3
python scripts/build_db.py                          # one-time ETL, ~20-30s
python -m uvicorn racik.api:app --port 8000          # from inside racik/
```

Open `http://localhost:8000`. No API key or AWS credentials are required —
without them, Agen Gizi/Agen Biaya and the rule-based orchestrator fall back
to a deterministic, non-LLM path so the app is fully demoable with zero
configuration.

To enable a live LLM (either agent behind the scenes, and the `/api/ask`
natural-language planner), the fastest path needs no AWS account at all:

```bash
export RACIK_LLM_PROVIDER=anthropic
export ANTHROPIC_API_KEY=<a key from console.anthropic.com>
```

or, with AWS Bedrock access:

```bash
export RACIK_LLM_PROVIDER=bedrock
export BEDROCK_MODEL_ID=<a model id enabled in your AWS account/region>
export AWS_REGION=<region>
# plus standard AWS credentials: env vars, ~/.aws/credentials, or an IAM role
```

See [`racik/README.md`](racik/README.md) for the SEA-LION alternative, the
architecture, the CP-SAT rubric, and the honest limitations list.

## Deploying

- [`deploy/huggingface-spaces.md`](deploy/huggingface-spaces.md) — free, no
  credit card, deploys the existing `Dockerfile` as-is.
- [`deploy/README.md`](deploy/README.md) — AWS App Runner, if you'd rather
  stay on AWS end to end.

## Tests

```bash
cd racik && python -m pytest tests -q
```

201 tests, no network or API key required.
