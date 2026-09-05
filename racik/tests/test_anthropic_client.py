"""Tests for the direct Anthropic Messages API client (anthropic_client.py).

No live API call anywhere here: message/tool-schema translation is tested as
pure functions, and chat() is tested against a monkeypatched _post so no
network or API key is needed.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from racik.anthropic_client import (AnthropicClient, _from_anthropic,
                                    _to_anthropic_messages, _to_tool_choice,
                                    _to_tool_specs)
from racik.sealion import SeaLionError, SeaLionNotConfigured


# ---------------------------------------------------------------- message translation

def test_system_message_is_pulled_out_of_the_message_list():
    system, messages = _to_anthropic_messages([
        {"role": "system", "content": "You are Agen Biaya."},
        {"role": "user", "content": "Plan a budget."},
    ])
    assert system == "You are Agen Biaya."
    assert messages == [{"role": "user",
                         "content": [{"type": "text", "text": "Plan a budget."}]}]


def test_tool_role_message_becomes_a_user_tool_result_block():
    _, messages = _to_anthropic_messages([
        {"role": "tool", "tool_call_id": "call_1", "name": "plan_menu",
         "content": '{"days": 5}'},
    ])
    assert messages == [{"role": "user", "content": [{
        "type": "tool_result", "tool_use_id": "call_1", "content": '{"days": 5}'}]}]


def test_assistant_tool_calls_become_tool_use_blocks():
    _, messages = _to_anthropic_messages([
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "biaya_advice", "arguments": '{"ingredient_budget_share": 0.7}'}}]},
    ])
    block = messages[0]["content"][0]
    assert block == {"type": "tool_use", "id": "call_1", "name": "biaya_advice",
                     "input": {"ingredient_budget_share": 0.7}}


def test_assistant_with_no_content_gets_an_empty_text_block():
    _, messages = _to_anthropic_messages([{"role": "assistant", "content": None,
                                           "tool_calls": []}])
    assert messages[0]["content"] == [{"type": "text", "text": ""}]


# ---------------------------------------------------------------- tool schema translation

def test_openai_style_tool_becomes_an_input_schema():
    specs = _to_tool_specs([{
        "type": "function",
        "function": {"name": "gizi_advice", "description": "Decide adjustments.",
                     "parameters": {"type": "object", "properties": {}}},
    }])
    assert specs == [{"name": "gizi_advice", "description": "Decide adjustments.",
                      "input_schema": {"type": "object", "properties": {}}}]


def test_required_tool_choice_forces_a_single_named_tool():
    specs = [{"name": "only_tool", "input_schema": {}}]
    assert _to_tool_choice("required", specs) == {"type": "tool", "name": "only_tool"}


def test_auto_tool_choice_by_default():
    assert _to_tool_choice("auto", [{"name": "x"}]) == {"type": "auto"}


# ---------------------------------------------------------------- response parsing

def test_from_anthropic_extracts_text_and_tool_use():
    body = {
        "content": [
            {"type": "text", "text": "Here is my decision."},
            {"type": "tool_use", "id": "t1", "name": "gizi_advice",
             "input": {"exclude_terms": ["udang"]}},
        ],
        "usage": {"input_tokens": 100, "output_tokens": 30},
    }
    result = _from_anthropic(body)
    assert result.content == "Here is my decision."
    assert result.wants_tools
    assert result.tool_calls[0].name == "gizi_advice"
    assert result.tool_calls[0].arguments == {"exclude_terms": ["udang"]}
    assert result.usage == {"prompt_tokens": 100, "completion_tokens": 30}


def test_from_anthropic_raises_on_api_error_body():
    with pytest.raises(SeaLionError):
        _from_anthropic({"error": {"type": "invalid_request_error", "message": "bad"}})


# ---------------------------------------------------------------- client configuration

def test_unconfigured_without_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    client = AnthropicClient(api_key="")
    assert client.configured is False
    with pytest.raises(SeaLionNotConfigured):
        client.chat([{"role": "user", "content": "hi"}])


def test_api_key_and_model_from_environment(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
    client = AnthropicClient()
    assert client.configured is True
    assert client.model == "claude-haiku-4-5-20251001"


# ---------------------------------------------------------------- chat() against a mocked _post

def test_chat_posts_expected_shape_and_parses_response(monkeypatch):
    client = AnthropicClient(api_key="sk-ant-test")
    captured = {}

    def fake_post(self, path, payload):
        captured["path"] = path
        captured["payload"] = payload
        return {"content": [{"type": "text", "text": "hello"}],
                "usage": {"input_tokens": 1, "output_tokens": 1}}
    monkeypatch.setattr(AnthropicClient, "_post", fake_post)

    result = client.chat(
        [{"role": "system", "content": "Be terse."},
         {"role": "user", "content": "Say hi."}],
        max_tokens=64, temperature=0.0)

    assert result.content == "hello"
    assert captured["path"] == "/messages"
    assert captured["payload"]["system"] == "Be terse."
    assert captured["payload"]["max_tokens"] == 64
    assert "tools" not in captured["payload"]


def test_chat_translates_auth_error_into_sealion_error(monkeypatch):
    import urllib.error

    client = AnthropicClient(api_key="sk-ant-bad")

    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(client.base_url + "/messages", 401,
                                     "Unauthorized", {}, None)
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    with pytest.raises(SeaLionError, match="rejected the API key"):
        client.chat([{"role": "user", "content": "hi"}])
