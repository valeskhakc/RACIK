"""Anthropic's Messages API (api.anthropic.com) — a third interim provider
alongside SEA-LION and Bedrock.

Exists to unblock testing Agen Gizi/Agen Biaya's live-LLM path independently
of AWS credential availability: same duck-typed contract as `SeaLionClient`
(`.configured`, `.chat(...)`, `.describe()`), so it drops into
`orchestrator.py`/`agents.py` exactly like the other clients — none of that
code needs to know which provider it's talking to. Swapping to Bedrock later
is an env var change (`RACIK_LLM_PROVIDER=bedrock`), not a rewrite.

Built on `urllib`, matching `sealion.py`'s "no new dependency" discipline —
no `anthropic` SDK needed.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Optional

from .sealion import ChatResult, SeaLionClient, SeaLionError, SeaLionNotConfigured, ToolCall

DEFAULT_BASE_URL = "https://api.anthropic.com/v1"
DEFAULT_MODEL = "claude-haiku-4-5-20251001"
ANTHROPIC_VERSION = "2023-06-01"


class AnthropicClient(SeaLionClient):
    """Direct Anthropic API — no AWS account or Bedrock model-access needed."""

    def __init__(self, api_key: Optional[str] = None,
                 model: Optional[str] = None,
                 base_url: str = DEFAULT_BASE_URL,
                 timeout: float = 60.0, max_retries: int = 3):
        super().__init__(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY", ""),
                         base_url=base_url, min_interval=0.0, max_retries=max_retries)
        self.model = model or os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL)
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def describe(self) -> dict:
        return {
            "provider": "Anthropic (direct API)",
            "model": self.model,
            "base_url": self.base_url,
            "configured": self.configured,
            "calls_made": self.calls,
            "note": ("Messages API with tool use; requires ANTHROPIC_API_KEY "
                     "from console.anthropic.com."),
        }

    def chat(self, messages: list[dict], tools: Optional[list[dict]] = None,
             temperature: float = 0.0, max_tokens: int = 1400,
             tool_choice: str = "auto") -> ChatResult:
        if not self.configured:
            raise SeaLionNotConfigured(
                "No ANTHROPIC_API_KEY set. Get one at console.anthropic.com "
                "and export ANTHROPIC_API_KEY.")

        system, anthropic_messages = _to_anthropic_messages(messages)
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": anthropic_messages,
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = _to_tool_specs(tools)
            payload["tool_choice"] = _to_tool_choice(tool_choice, payload["tools"])

        body = self._post("/messages", payload)
        return _from_anthropic(body)

    def _post(self, path: str, payload: dict) -> dict:
        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries):
            request = urllib.request.Request(
                self.base_url + path,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": self.api_key,
                    "anthropic-version": ANTHROPIC_VERSION,
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as res:
                    self.calls += 1
                    return json.loads(res.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = _read_error(exc)
                if exc.code == 429:
                    last_error = SeaLionError(f"rate limited: {detail}")
                    time.sleep(min(2 ** attempt * 2.0, 20.0))
                    continue
                if exc.code in (401, 403):
                    raise SeaLionError(
                        f"Anthropic rejected the API key ({exc.code}): {detail}"
                    ) from exc
                if 500 <= exc.code < 600:
                    last_error = SeaLionError(f"server error {exc.code}: {detail}")
                    time.sleep(2 ** attempt)
                    continue
                raise SeaLionError(f"HTTP {exc.code}: {detail}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = SeaLionError(f"network error: {exc}")
                time.sleep(2 ** attempt)
        raise last_error or SeaLionError("request failed with no diagnosis")


# ---------------------------------------------------------------- message translation


def _to_anthropic_messages(messages: list[dict]) -> tuple[str, list[dict]]:
    """OpenAI-style messages (system/user/assistant/tool) -> Anthropic's
    (system string, messages with only user/assistant roles and content blocks)."""
    system_parts: list[str] = []
    out: list[dict] = []
    for msg in messages:
        role = msg.get("role")
        if role == "system":
            if msg.get("content"):
                system_parts.append(str(msg["content"]))
            continue
        if role == "tool":
            out.append({"role": "user", "content": [{
                "type": "tool_result",
                "tool_use_id": msg.get("tool_call_id", ""),
                "content": str(msg.get("content", "")),
            }]})
            continue
        content: list[dict] = []
        if msg.get("content"):
            content.append({"type": "text", "text": str(msg["content"])})
        for call in msg.get("tool_calls") or []:
            function = call.get("function", {})
            arguments = function.get("arguments", "{}")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments or "{}")
                except json.JSONDecodeError:
                    arguments = {}
            content.append({"type": "tool_use", "id": call.get("id", ""),
                            "name": function.get("name", ""), "input": arguments})
        if not content:
            content = [{"type": "text", "text": ""}]
        out.append({"role": "assistant" if role == "assistant" else "user",
                    "content": content})
    return "\n\n".join(system_parts), out


def _to_tool_specs(tools: list[dict]) -> list[dict]:
    specs = []
    for tool in tools:
        fn = tool.get("function", tool)
        specs.append({
            "name": fn["name"],
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
        })
    return specs


def _to_tool_choice(tool_choice, specs: list[dict]) -> dict:
    if isinstance(tool_choice, dict):
        return tool_choice
    if tool_choice == "required" and len(specs) == 1:
        return {"type": "tool", "name": specs[0]["name"]}
    return {"type": "auto"}


def _from_anthropic(body: dict) -> ChatResult:
    if "error" in body:
        raise SeaLionError(f"Anthropic error: {body['error']}")
    text_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    for block in body.get("content") or []:
        if block.get("type") == "text":
            text_parts.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            tool_calls.append(ToolCall(id=block.get("id", ""),
                                       name=block.get("name", ""),
                                       arguments=block.get("input", {}) or {}))
    usage = body.get("usage") or {}
    return ChatResult(
        content="\n".join(text_parts).strip(),
        tool_calls=tool_calls,
        raw=body,
        usage={"prompt_tokens": usage.get("input_tokens", 0),
               "completion_tokens": usage.get("output_tokens", 0)},
    )


def _read_error(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8")[:400]
    except Exception:
        return exc.reason or "unknown"
