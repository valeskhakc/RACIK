"""Tests for the standard AWS Bedrock Converse API client (bedrock_converse.py).

No live AWS call is made anywhere here: `_to_converse`/`_to_tool_config`/
`_from_converse` are pure functions tested directly, and `chat()` is tested
against a fake boto3 client injected in place of `_runtime()`.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from racik.bedrock_converse import (BedrockConverseClient, _from_converse,
                                    _to_converse, _to_tool_config)
from racik.sealion import SeaLionError, SeaLionNotConfigured


# ---------------------------------------------------------------- message translation

def test_system_message_is_pulled_out_of_the_message_list():
    system, messages = _to_converse([
        {"role": "system", "content": "You are Agen Gizi."},
        {"role": "user", "content": "Plan a menu."},
    ])
    assert system == [{"text": "You are Agen Gizi."}]
    assert messages == [{"role": "user", "content": [{"text": "Plan a menu."}]}]


def test_tool_role_message_becomes_a_user_tool_result_block():
    _, messages = _to_converse([
        {"role": "tool", "tool_call_id": "call_1", "name": "plan_menu",
         "content": '{"days": 5}'},
    ])
    assert messages == [{"role": "user", "content": [{"toolResult": {
        "toolUseId": "call_1", "content": [{"text": '{"days": 5}'}]}}]}]


def test_assistant_tool_calls_become_tool_use_blocks():
    _, messages = _to_converse([
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "plan_menu", "arguments": '{"stage": "sd"}'}}]},
    ])
    assert messages[0]["role"] == "assistant"
    tool_use = messages[0]["content"][0]["toolUse"]
    assert tool_use == {"toolUseId": "call_1", "name": "plan_menu",
                        "input": {"stage": "sd"}}


def test_assistant_with_no_content_gets_an_empty_text_block():
    """Converse rejects an empty content array; every message needs a block."""
    _, messages = _to_converse([{"role": "assistant", "content": None,
                                 "tool_calls": []}])
    assert messages[0]["content"] == [{"text": ""}]


# ---------------------------------------------------------------- tool schema translation

def test_openai_style_tool_becomes_a_tool_spec():
    config = _to_tool_config([{
        "type": "function",
        "function": {"name": "gizi_advice", "description": "Decide adjustments.",
                     "parameters": {"type": "object", "properties": {}}},
    }], tool_choice="auto")
    assert config["tools"][0]["toolSpec"]["name"] == "gizi_advice"
    assert config["tools"][0]["toolSpec"]["inputSchema"]["json"]["type"] == "object"
    assert config["toolChoice"] == {"auto": {}}


def test_required_tool_choice_forces_a_single_named_tool():
    config = _to_tool_config([{
        "type": "function",
        "function": {"name": "only_tool", "parameters": {}},
    }], tool_choice="required")
    assert config["toolChoice"] == {"tool": {"name": "only_tool"}}


# ---------------------------------------------------------------- response parsing

def test_from_converse_extracts_text_and_tool_use():
    response = {
        "output": {"message": {"role": "assistant", "content": [
            {"text": "Here is the plan."},
            {"toolUse": {"toolUseId": "t1", "name": "plan_menu",
                        "input": {"days": 5}}},
        ]}},
        "usage": {"inputTokens": 120, "outputTokens": 40},
    }
    result = _from_converse(response)
    assert result.content == "Here is the plan."
    assert result.wants_tools
    assert result.tool_calls[0].name == "plan_menu"
    assert result.tool_calls[0].arguments == {"days": 5}
    assert result.usage == {"prompt_tokens": 120, "completion_tokens": 40}


def test_from_converse_handles_text_only_response():
    response = {"output": {"message": {"content": [{"text": "OK"}]}}, "usage": {}}
    result = _from_converse(response)
    assert result.content == "OK"
    assert not result.wants_tools


# ---------------------------------------------------------------- client configuration

def test_unconfigured_without_model_id(monkeypatch):
    monkeypatch.delenv("BEDROCK_MODEL_ID", raising=False)
    client = BedrockConverseClient(model_id="")
    assert client.configured is False
    with pytest.raises(SeaLionNotConfigured):
        client.chat([{"role": "user", "content": "hi"}])


def test_model_id_from_environment(monkeypatch):
    monkeypatch.setenv("BEDROCK_MODEL_ID", "anthropic.claude-haiku-4-5-v1:0")
    client = BedrockConverseClient()
    assert client.model_id == "anthropic.claude-haiku-4-5-v1:0"
    assert client.model == "anthropic.claude-haiku-4-5-v1:0"


# ---------------------------------------------------------------- chat() against a fake runtime

class _FakeBedrockRuntime:
    """Stands in for the boto3 bedrock-runtime client."""

    def __init__(self, response=None, exc=None):
        self.response = response
        self.exc = exc
        self.calls: list[dict] = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc:
            raise self.exc
        return self.response


def test_chat_calls_converse_with_expected_shape():
    client = BedrockConverseClient(model_id="anthropic.claude-haiku-4-5-v1:0")
    fake = _FakeBedrockRuntime(response={
        "output": {"message": {"content": [{"text": "hello"}]}},
        "usage": {"inputTokens": 1, "outputTokens": 1},
    })
    client._client = fake

    result = client.chat(
        [{"role": "system", "content": "Be terse."},
         {"role": "user", "content": "Say hi."}],
        max_tokens=64, temperature=0.0)

    assert result.content == "hello"
    call = fake.calls[0]
    assert call["modelId"] == "anthropic.claude-haiku-4-5-v1:0"
    assert call["system"] == [{"text": "Be terse."}]
    assert call["inferenceConfig"] == {"maxTokens": 64, "temperature": 0.0}
    assert "toolConfig" not in call       # no tools passed -> no toolConfig
    assert client.calls == 1


def test_chat_translates_access_denied_into_sealion_error():
    class AccessDeniedException(Exception):
        pass

    client = BedrockConverseClient(model_id="anthropic.claude-haiku-4-5-v1:0")
    client._client = _FakeBedrockRuntime(exc=AccessDeniedException("nope"))

    with pytest.raises(SeaLionError, match="bedrock:InvokeModel"):
        client.chat([{"role": "user", "content": "hi"}])
