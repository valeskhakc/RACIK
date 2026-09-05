"""Running SEA-LION on Amazon Bedrock.

SEA-LION is not a native Bedrock foundation model. It reaches Bedrock through
**Custom Model Import**: you copy the Hugging Face weights to S3, import them,
and invoke the resulting model ARN. That gives a private, VPC-reachable,
IAM-governed deployment with no third-party rate limit — which is what a
government-adjacent workload usually needs.

Three ways to talk to it, and the right one depends on whether you need tools:

1. ``BedrockSeaLionClient`` (this module) — boto3 ``InvokeModel`` using the
   **OpenAIChatCompletion** payload format that Custom Model Import has
   supported since 17 Nov 2025. Same message and response shape as the hosted
   API, so it drops straight into the orchestrator.
2. **Bedrock Access Gateway** — an OpenAI-compatible HTTP shim in front of
   Bedrock. Needs no code at all: point ``SeaLionClient(base_url=...)`` at it.
3. **Hosted api.sea-lion.ai** — the default; simplest, but rate-limited and
   off your own infrastructure.

Constraints worth knowing before you commit (all from AWS's own docs, verified
rather than assumed):

* **Tool calling is not guaranteed.** AWS states tool calling for imported
  models in the context of GPT-OSS architectures, and the Converse API is
  explicitly unsupported for Qwen models. Racik's orchestrator depends on
  function calling, so verify it end to end on your import before relying on
  it — ``verify_deployment()`` below does exactly that. If tools do not come
  back, use the Access Gateway or the hosted API for the orchestrator and keep
  Bedrock for plain generation such as dish-name translation.
* **Context must be under 128K tokens.** SEA-LION v4.5 27B ships a 262K
  window, so it needs ``max_position_embeddings`` reduced before import, or a
  smaller SEA-LION variant.
* **Regions are limited** to us-east-1, us-east-2, us-west-2 and eu-central-1.
  There is no ap-southeast-1, which matters for an Indonesian deployment:
  expect cross-region latency and check data-residency rules.
* **Cold starts are real.** Bedrock evicts idle imported models and returns
  ``ModelNotReadyException`` on the next call while it restores. This client
  configures boto3's standard retry mode for that.
* The chat template must be present in ``tokenizer_config.json``; Custom Model
  Import applies no default template.
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

from .sealion import (ChatResult, SeaLionClient, SeaLionError,
                      SeaLionNotConfigured, _parse_tool_calls)

# Regions where Bedrock Custom Model Import is available.
SUPPORTED_REGIONS = ("us-east-1", "us-east-2", "us-west-2", "eu-central-1")

# Bedrock imposes this on imported models; SEA-LION v4.5 27B exceeds it as shipped.
MAX_CONTEXT_TOKENS = 128_000


class BedrockSeaLionClient(SeaLionClient):
    """SEA-LION imported into Bedrock, spoken to in OpenAI chat format."""

    def __init__(self, model_arn: Optional[str] = None,
                 region: Optional[str] = None,
                 timeout: float = 120.0,
                 max_attempts: int = 10):
        super().__init__(api_key="", base_url="bedrock://", timeout=timeout,
                         min_interval=0.0)          # no third-party rate limit
        self.model_arn = model_arn or os.environ.get("BEDROCK_MODEL_ARN", "")
        self.region = (region or os.environ.get("AWS_REGION")
                       or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1")
        self.max_attempts = max_attempts
        self.model = self.model_arn or "bedrock:<unset>"
        self._client = None
        self.last_error: Optional[str] = None

    # ------------------------------------------------------------ wiring
    @property
    def configured(self) -> bool:
        return bool(self.model_arn) and _boto3_available()

    def describe(self) -> dict:
        return {
            "provider": "SEA-LION on Amazon Bedrock (Custom Model Import)",
            "model": self.model_arn or "(BEDROCK_MODEL_ARN unset)",
            "base_url": f"bedrock-runtime.{self.region}.amazonaws.com",
            "region": self.region,
            "region_supported": self.region in SUPPORTED_REGIONS,
            "configured": self.configured,
            "boto3_available": _boto3_available(),
            "calls_made": self.calls,
            "note": ("OpenAIChatCompletion payload via InvokeModel. Tool calling "
                     "is not guaranteed for imported models — run "
                     "verify_deployment() before relying on the orchestrator."),
        }

    def _runtime(self):
        if self._client is not None:
            return self._client
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:                    # pragma: no cover
            raise SeaLionNotConfigured(
                "boto3 is required for the Bedrock provider: pip install boto3"
            ) from exc
        # Imported models are evicted when idle; the first call after eviction
        # raises ModelNotReadyException while Bedrock restores them.
        self._client = boto3.client(
            "bedrock-runtime", region_name=self.region,
            config=Config(retries={"total_max_attempts": self.max_attempts,
                                   "mode": "standard"},
                          read_timeout=self.timeout))
        return self._client

    # ------------------------------------------------------------ inference
    def chat(self, messages: list[dict], tools: Optional[list[dict]] = None,
             temperature: float = 0.0, max_tokens: int = 1400,
             tool_choice: str = "auto") -> ChatResult:
        if not self.model_arn:
            raise SeaLionNotConfigured(
                "Set BEDROCK_MODEL_ARN to the ARN returned by the Bedrock "
                "model import job, e.g. "
                "arn:aws:bedrock:us-east-1:123456789012:imported-model/abc123")

        payload: dict[str, Any] = {
            "messages": _strip_nulls(messages),
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice

        try:
            response = self._runtime().invoke_model(
                modelId=self.model_arn, body=json.dumps(payload),
                accept="application/json", contentType="application/json")
            body = json.loads(response["body"].read())
            self.calls += 1
        except Exception as exc:
            name = type(exc).__name__
            if "ModelNotReady" in name:
                raise SeaLionError(
                    "Bedrock is restoring the imported model after idle "
                    "eviction; retry shortly. Raise max_attempts to wait longer."
                ) from exc
            if "AccessDenied" in name or "UnrecognizedClient" in name:
                raise SeaLionError(
                    f"Bedrock rejected the credentials ({name}). The role needs "
                    "bedrock:InvokeModel on the imported model ARN.") from exc
            raise SeaLionError(f"Bedrock InvokeModel failed ({name}): {exc}") from exc

        return _to_chat_result(body)

    # ------------------------------------------------------------ preflight
    def verify_deployment(self) -> dict:
        """Check the import end to end, including whether tool calling works.

        Cheaper to run once than to discover mid-demo that the orchestrator
        silently lost its tools.
        """
        report: dict[str, Any] = {
            "region": self.region,
            "region_supported": self.region in SUPPORTED_REGIONS,
            "model_arn_set": bool(self.model_arn),
            "boto3_available": _boto3_available(),
            "chat": None, "tool_calling": None, "errors": [],
        }
        if not self.configured:
            report["errors"].append(
                "not configured: needs boto3 and BEDROCK_MODEL_ARN")
            return report

        try:
            plain = self.chat([{"role": "user", "content": "Reply with: OK"}],
                              max_tokens=16)
            report["chat"] = bool(plain.content)
        except SeaLionError as exc:
            report["chat"] = False
            report["errors"].append(f"chat failed: {exc}")
            return report

        probe_tool = [{
            "type": "function",
            "function": {
                "name": "ping",
                "description": "Return a pong. Call this tool.",
                "parameters": {"type": "object",
                               "properties": {"value": {"type": "string"}}},
            },
        }]
        try:
            tooled = self.chat(
                [{"role": "user", "content": "Call the ping tool."}],
                tools=probe_tool, max_tokens=128)
            report["tool_calling"] = tooled.wants_tools
            if not tooled.wants_tools:
                report["errors"].append(
                    "the import returned no tool_calls — use the Bedrock Access "
                    "Gateway or the hosted API for the orchestrator")
        except SeaLionError as exc:
            report["tool_calling"] = False
            report["errors"].append(f"tool probe failed: {exc}")
        return report


# ---------------------------------------------------------------- helpers


def _boto3_available() -> bool:
    try:
        import boto3  # noqa: F401
        return True
    except ImportError:
        return False


def _strip_nulls(messages: list[dict]) -> list[dict]:
    """Drop null content, which some servers reject on assistant tool turns."""
    out = []
    for message in messages:
        cleaned = {k: v for k, v in message.items() if v is not None}
        cleaned.setdefault("content", "")
        out.append(cleaned)
    return out


def _to_chat_result(body: dict) -> ChatResult:
    """Read an OpenAIChatCompletion response, or the BedrockCompletion fallback."""
    choices = body.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") or {}
        return ChatResult(
            content=(message.get("content") or "").strip(),
            tool_calls=_parse_tool_calls(message.get("tool_calls") or []),
            raw=message, usage=body.get("usage", {}) or {})
    # BedrockCompletion shape, used by models imported before Nov 2025.
    if "generation" in body:
        return ChatResult(content=str(body["generation"]).strip(), raw=body,
                          usage={"prompt_tokens": body.get("prompt_token_count", 0),
                                 "completion_tokens": body.get(
                                     "generation_token_count", 0)})
    raise SeaLionError(f"unrecognised Bedrock response shape: {list(body)[:6]}")


# ---------------------------------------------------------------- factory


def make_client() -> SeaLionClient:
    """Pick the LLM provider from the environment.

    RACIK_LLM_PROVIDER = openai | anthropic | bedrock | bedrock-sealion-import
                          | gateway | sealion   (default: sealion)

      openai                  OPENAI_API_KEY, OPENAI_MODEL (optional) —
                               OpenAI's Chat Completions API. No new client
                               needed: it's the same OpenAI-compatible shape
                               SeaLionClient already speaks, just pointed at
                               api.openai.com.
      anthropic               ANTHROPIC_API_KEY, ANTHROPIC_MODEL (optional)
                               — direct Anthropic Messages API, no AWS
                               account needed. Useful for testing the agent
                               pipeline's live-LLM path independently of
                               Bedrock credential availability; see
                               anthropic_client.py.
      bedrock                 BEDROCK_MODEL_ID, AWS_REGION  — Converse API,
                               any standard Bedrock foundation model
                               (Claude, Nova, Llama, ...). This is what
                               ordinary "I have Bedrock access" means; prefer
                               it unless you specifically need SEA-LION.
      bedrock-sealion-import  BEDROCK_MODEL_ARN, AWS_REGION — boto3
                               InvokeModel against a SEA-LION model brought in
                               via Bedrock Custom Model Import (this module's
                               BedrockSeaLionClient, above). Narrower and
                               tool-calling is not guaranteed — see the module
                               docstring.
      gateway   SEALION_BASE_URL (Bedrock Access Gateway) — OpenAI-compatible
      sealion   SEALION_API_KEY                           — hosted API
    """
    provider = os.environ.get("RACIK_LLM_PROVIDER", "").strip().lower()
    base_url = os.environ.get("SEALION_BASE_URL", "").strip()

    if provider == "openai" or (not provider and os.environ.get("OPENAI_API_KEY")):
        return SeaLionClient(
            api_key=os.environ.get("OPENAI_API_KEY", ""),
            base_url="https://api.openai.com/v1",
            model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
            min_interval=0.0)
    if provider == "anthropic" or (not provider and os.environ.get("ANTHROPIC_API_KEY")):
        from .anthropic_client import AnthropicClient
        return AnthropicClient()
    if provider == "bedrock" or (not provider and os.environ.get("BEDROCK_MODEL_ID")):
        from .bedrock_converse import BedrockConverseClient
        return BedrockConverseClient()
    if provider == "bedrock-sealion-import" or (
            not provider and os.environ.get("BEDROCK_MODEL_ARN")):
        return BedrockSeaLionClient()
    if provider == "gateway" or (not provider and base_url):
        return SeaLionClient(
            api_key=os.environ.get("SEALION_API_KEY", "bedrock"),
            base_url=base_url or "http://localhost:8000/api/v1",
            model=os.environ.get("SEALION_MODEL", "")
                  or os.environ.get("BEDROCK_MODEL_ARN", ""),
            min_interval=0.0)
    return SeaLionClient()
