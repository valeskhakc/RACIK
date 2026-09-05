"""Standard AWS Bedrock foundation models via the Converse API.

This is deliberately a separate client from `bedrock.py`'s
`BedrockSeaLionClient`, which speaks to a SEA-LION model *imported* into
Bedrock via Custom Model Import — a narrower, harder-to-verify path where
AWS's own docs say tool calling "is not guaranteed" for imported models.

The Converse API is the opposite case: it's Bedrock's standard, documented,
tool-calling-guaranteed interface for first-party and partner foundation
models (Claude, Nova, Llama, Mistral, ...) already available in an account —
no import job, no ARN, no context-window surgery. If you have "AWS Bedrock
access" in the ordinary sense (a model enabled in the console, standard IAM
credentials), this is the client that reaches it.

Same duck-typed contract as `SeaLionClient` (`.configured`, `.chat(...)`,
`.describe()`), so it drops into `orchestrator.py` and `agents.py` exactly
like the SEA-LION clients do — none of that calling code needs to know which
provider it's talking to.
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

from .sealion import ChatResult, SeaLionClient, SeaLionError, SeaLionNotConfigured, ToolCall

DEFAULT_REGION = "us-east-1"


class BedrockConverseClient(SeaLionClient):
    """A standard Bedrock foundation model, spoken to via `converse()`."""

    def __init__(self, model_id: Optional[str] = None, region: Optional[str] = None,
                 timeout: float = 90.0, max_attempts: int = 6):
        super().__init__(api_key="", base_url="bedrock-converse://", min_interval=0.0)
        self.model_id = model_id or os.environ.get("BEDROCK_MODEL_ID", "")
        self.region = (region or os.environ.get("AWS_REGION")
                       or os.environ.get("AWS_DEFAULT_REGION") or DEFAULT_REGION)
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.model = self.model_id or "bedrock-converse:<unset>"
        self._client = None

    @property
    def configured(self) -> bool:
        return bool(self.model_id) and _boto3_available()

    def describe(self) -> dict:
        return {
            "provider": "AWS Bedrock (Converse API)",
            "model": self.model_id or "(BEDROCK_MODEL_ID unset)",
            "region": self.region,
            "configured": self.configured,
            "boto3_available": _boto3_available(),
            "calls_made": self.calls,
            "note": ("Standard Bedrock foundation model via bedrock-runtime "
                     "converse(); requires BEDROCK_MODEL_ID and AWS "
                     "credentials (env vars, ~/.aws, or an IAM role)."),
        }

    def _runtime(self):
        if self._client is not None:
            return self._client
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:  # pragma: no cover
            raise SeaLionNotConfigured(
                "boto3 is required for the Bedrock provider: pip install boto3"
            ) from exc
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
        if not self.model_id:
            raise SeaLionNotConfigured(
                "Set BEDROCK_MODEL_ID to a Bedrock foundation model id "
                "available in your account/region, e.g. "
                "'anthropic.claude-haiku-4-5-v1:0' (check `aws bedrock "
                "list-foundation-models` for what your account can call).")

        system_blocks, converse_messages = _to_converse(messages)
        kwargs: dict[str, Any] = {
            "modelId": self.model_id,
            "messages": converse_messages,
            "inferenceConfig": {"maxTokens": max_tokens, "temperature": temperature},
        }
        if system_blocks:
            kwargs["system"] = system_blocks
        if tools:
            kwargs["toolConfig"] = _to_tool_config(tools, tool_choice)

        try:
            response = self._runtime().converse(**kwargs)
            self.calls += 1
        except Exception as exc:
            name = type(exc).__name__
            if "AccessDenied" in name or "UnrecognizedClient" in name:
                raise SeaLionError(
                    f"Bedrock rejected the credentials ({name}). The caller "
                    f"needs bedrock:InvokeModel on {self.model_id}.") from exc
            if "ValidationException" in name:
                raise SeaLionError(
                    f"Bedrock rejected the request ({name}): {exc}. Check that "
                    f"{self.model_id} is enabled for your account in "
                    f"{self.region} and supports tool use if tools were sent."
                ) from exc
            if "ThrottlingException" in name:
                raise SeaLionError(f"Bedrock throttled the request: {exc}") from exc
            raise SeaLionError(f"Bedrock converse() failed ({name}): {exc}") from exc

        return _from_converse(response)


# ---------------------------------------------------------------- message translation


def _to_converse(messages: list[dict]) -> tuple[list[dict], list[dict]]:
    """OpenAI-style messages (system/user/assistant/tool) -> Converse's
    (system blocks, messages with only user/assistant roles)."""
    system_blocks: list[dict] = []
    out: list[dict] = []
    for msg in messages:
        role = msg.get("role")
        if role == "system":
            if msg.get("content"):
                system_blocks.append({"text": str(msg["content"])})
            continue
        if role == "tool":
            # A tool result — Converse carries these as a user-role toolResult block.
            out.append({"role": "user", "content": [{
                "toolResult": {
                    "toolUseId": msg.get("tool_call_id", ""),
                    "content": [{"text": str(msg.get("content", ""))}],
                },
            }]})
            continue
        content: list[dict] = []
        if msg.get("content"):
            content.append({"text": str(msg["content"])})
        for call in msg.get("tool_calls") or []:
            function = call.get("function", {})
            arguments = function.get("arguments", "{}")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments or "{}")
                except json.JSONDecodeError:
                    arguments = {}
            content.append({"toolUse": {
                "toolUseId": call.get("id", ""),
                "name": function.get("name", ""),
                "input": arguments,
            }})
        if not content:
            content = [{"text": ""}]
        out.append({"role": "assistant" if role == "assistant" else "user",
                    "content": content})
    return system_blocks, out


def _to_tool_config(tools: list[dict], tool_choice: str) -> dict:
    """OpenAI-style `{"type":"function","function":{...}}` tool schemas ->
    Converse's `toolConfig`."""
    specs = []
    for tool in tools:
        fn = tool.get("function", tool)
        specs.append({"toolSpec": {
            "name": fn["name"],
            "description": fn.get("description", ""),
            "inputSchema": {"json": fn.get("parameters", {"type": "object", "properties": {}})},
        }})
    config: dict[str, Any] = {"tools": specs}
    if isinstance(tool_choice, dict):
        config["toolChoice"] = tool_choice
    elif tool_choice == "required" and len(specs) == 1:
        config["toolChoice"] = {"tool": {"name": specs[0]["toolSpec"]["name"]}}
    else:
        config["toolChoice"] = {"auto": {}}
    return config


def _from_converse(response: dict) -> ChatResult:
    message = (response.get("output") or {}).get("message") or {}
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    for block in message.get("content") or []:
        if "text" in block:
            text_parts.append(block["text"])
        elif "toolUse" in block:
            use = block["toolUse"]
            tool_calls.append(ToolCall(id=use.get("toolUseId", ""),
                                       name=use.get("name", ""),
                                       arguments=use.get("input", {}) or {}))
    usage = response.get("usage") or {}
    return ChatResult(
        content="\n".join(text_parts).strip(),
        tool_calls=tool_calls,
        raw=message,
        usage={"prompt_tokens": usage.get("inputTokens", 0),
               "completion_tokens": usage.get("outputTokens", 0)},
    )


def _boto3_available() -> bool:
    try:
        import boto3  # noqa: F401
        return True
    except ImportError:
        return False
