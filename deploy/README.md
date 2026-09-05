# Deploying Racik to AWS App Runner

This is a runbook, not something already run — none of these commands have
been executed. Every command here creates or costs real AWS resources; run
them yourself (or ask me to, one at a time) rather than treating this file as
already-done.

## Prerequisites

- AWS CLI v2, configured (`aws configure` or an existing profile) with
  permissions to create ECR repos, IAM roles, and App Runner services.
- Docker, to build and push the image.
- A Bedrock model **enabled for your account** in your target region —
  check the console under Bedrock → Model access, or:
  ```bash
  aws bedrock list-foundation-models --region REGION \
    --query "modelSummaries[?responseStreamingSupported].modelId"
  ```
  Pick a model whose `modelId` supports tool use via the Converse API (any
  current Claude model on Bedrock does). That value goes in
  `deploy/apprunner.json`'s `BEDROCK_MODEL_ID`.

  Newer Claude models (Sonnet 5, Haiku 4.5, ...) are inference-profile-only —
  Bedrock rejects the bare model ID and wants the profile ID from
  `aws bedrock list-inference-profiles` instead, which needs its own IAM
  grant separate from the model's. If your account/role can't invoke a
  profile, an older on-demand model like
  `anthropic.claude-3-haiku-20240307-v1:0` is far more likely to already
  work without extra IAM changes — confirmed working during development of
  this app on a restricted shared account. See `racik/README.md`'s
  "Choosing an LLM provider" section for the full explanation.

Replace `ACCOUNT_ID` and `REGION` below with your own throughout.

## 1. Push the image to ECR

```bash
aws ecr create-repository --repository-name racik --region REGION

aws ecr get-login-password --region REGION | \
  docker login --username AWS --password-stdin ACCOUNT_ID.dkr.ecr.REGION.amazonaws.com

docker build -t racik:latest .
docker tag racik:latest ACCOUNT_ID.dkr.ecr.REGION.amazonaws.com/racik:latest
docker push ACCOUNT_ID.dkr.ecr.REGION.amazonaws.com/racik:latest
```

## 2. Create the two IAM roles

**ECR access role** — lets App Runner pull the image:

```bash
aws iam create-role --role-name racik-apprunner-ecr-access \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [{"Effect": "Allow",
      "Principal": {"Service": "build.apprunner.amazonaws.com"},
      "Action": "sts:AssumeRole"}]}'

aws iam attach-role-policy --role-name racik-apprunner-ecr-access \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSAppRunnerServicePolicyForECRAccess
```

**Instance role** — lets the *running* app call Bedrock, scoped to exactly
the model you enabled (edit `deploy/iam-bedrock-policy.json`'s `REGION` and
`BEDROCK_MODEL_ID` first):

```bash
aws iam create-role --role-name racik-apprunner-bedrock-invoke \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [{"Effect": "Allow",
      "Principal": {"Service": "tasks.apprunner.amazonaws.com"},
      "Action": "sts:AssumeRole"}]}'

aws iam put-role-policy --role-name racik-apprunner-bedrock-invoke \
  --policy-name RacikBedrockInvoke \
  --policy-document file://deploy/iam-bedrock-policy.json
```

## 3. Create the App Runner service

Edit `deploy/apprunner.json`: fill in `ACCOUNT_ID`, `REGION`, and
`BEDROCK_MODEL_ID` (four places total). Then:

```bash
aws apprunner create-service --cli-input-json file://deploy/apprunner.json
```

This takes a few minutes. Check status with:

```bash
aws apprunner describe-service --service-arn <arn from the create-service output>
```

## 4. Smoke test

```bash
curl https://<service-url>/api/health
curl https://<service-url>/api/orchestrator   # confirms Bedrock is reachable
```

`/api/orchestrator` reports `"mode": "sealion"` when a live LLM is reachable
(the field name predates the Bedrock Converse addition — a working Bedrock
connection still reports through it) or `"mode": "fallback"` if Bedrock isn't
configured/reachable, in which case the app still works end-to-end on the
rule-based path.

## Known limitations of this deployment shape

- **State is not durable across restarts or multiple instances.**
  `racik_state.db` (operator reviews, menu history, stock/audit records —
  everything in `racik/racik/statedb.py`) lives on the container's local
  disk. Fine for a single-instance demo; a redeploy, a crash restart, or
  App Runner scaling to 2+ instances will fragment or lose it. The real fix
  is moving that store to RDS or DynamoDB — out of scope here given the
  timeline, but the next thing to do if this goes past a demo.
- **The corpus is baked into the image at build time.** Updating prices or
  recipes means rebuilding and redeploying the image, not a live data push.
- **Cost**: App Runner bills for provisioned compute while the service is
  running, plus Bedrock per-token charges when the LLM path is used. Tear
  the service down (`aws apprunner delete-service`) when you're done
  demoing if you don't want it running (and billing) unattended.
