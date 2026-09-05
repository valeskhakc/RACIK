# Deploying Racik to Hugging Face Spaces (free)

Free, no credit card, and the existing `Dockerfile` deploys as-is. This is a
runbook — nothing here has been run yet. The steps below split into **what
only you can do** (needs your Hugging Face / GitHub login) and **what's
already prepared** in this repo.

## What's already prepared

- `Dockerfile` — unchanged from the AWS runbook; builds `racik.db` at image
  build time and serves the app on port 8000.
- Root `README.md` now starts with the YAML frontmatter Spaces reads to know
  how to build it (`sdk: docker`, `app_port: 8000`). It'll render as a plain
  text block at the top of the GitHub-rendered README — a minor cosmetic
  trade-off for keeping one README instead of maintaining two.
- `.github/workflows/sync-to-hf-space.yml` — on every push to `main`, mirrors
  this repo to your Space so you never `git push` to Hugging Face by hand.
  It needs two GitHub repo secrets (step 3 below).

## 1. Create the Space

Go to [huggingface.co/new-space](https://huggingface.co/new-space) (sign in
first if needed):

- **Space name**: anything, e.g. `racik`
- **SDK**: Docker
- **Visibility**: Public (or Private if you'd rather keep it unlisted for now)
- Create it. You'll land on an empty Space — that's expected, nothing is
  pushed yet.

Note the Space's full name from the URL: `huggingface.co/spaces/<username>/<space-name>`.

## 2. Create a Hugging Face access token

[huggingface.co/settings/tokens](https://huggingface.co/settings/tokens) ->
**New token** -> role **Write** (needs write access to push to the Space).
Copy it now; you won't see it again.

## 3. Add the two GitHub repo secrets

In this GitHub repo: **Settings -> Secrets and variables -> Actions -> New
repository secret**, add:

| Name | Value |
|---|---|
| `HF_TOKEN` | the token from step 2 |
| `HF_SPACE` | `<username>/<space-name>` from step 1 |

## 4. Add the LLM provider as a Space secret (not a GitHub secret)

In the Space itself: **Settings -> Repository secrets** (or "Variables and
secrets"), add whichever provider you're using right now:

```
RACIK_LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=<your key from console.anthropic.com>
```

or, once you have working Bedrock credentials:

```
RACIK_LLM_PROVIDER=bedrock
BEDROCK_MODEL_ID=<a model id enabled in your account>
AWS_REGION=<region>
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
AWS_SESSION_TOKEN=...        # only if using temporary/STS credentials
```

These live only in the Space's own secret store — never in a committed file,
matching how `racik/.env` is handled locally.

## 5. Trigger the sync

Push anything to `main` (or open the Actions tab and run "Sync to Hugging
Face Space" manually via **Run workflow**). Hugging Face builds the Docker
image on its own infrastructure after receiving the push — first build takes
a few minutes (installing `ortools`/`pandas` and running the ETL); watch
progress in the Space's own "Logs" tab.

## 6. Smoke test

Once the Space shows "Running": `https://<username>-<space-name>.hf.space/api/health`
and `https://<username>-<space-name>.hf.space/api/orchestrator` (confirms
whichever LLM provider you configured is reachable).

## Known limitations

- **State is not durable.** Same caveat as the AWS runbook: `racik_state.db`
  (operator reviews, menu history, stock/audit) lives on the Space's
  container disk and is lost on a rebuild/restart. Fine for demos.
- **Free-tier CPU.** The community CPU tier is enough for FastAPI + CP-SAT
  at the portion counts this app targets, but a solve under heavy concurrent
  load will be slower than a dedicated instance.
- **Spaces can idle-sleep** depending on tier/settings; the first request
  after a sleep period takes longer while it wakes.
