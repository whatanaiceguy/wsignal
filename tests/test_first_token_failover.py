import asyncio
import json

import httpx
import pytest

from wsignal.config import Settings
from wsignal.inference import llm
from wsignal.inference.llm import LlmClient, LlmUnavailable

_MESSAGES = [{"role": "user", "content": "hi"}]
_ENDPOINTS = {
    "data": {
        "endpoints": [
            {"provider_name": "Wafer", "tag": "wafer/us"},
            {"provider_name": "Wafer", "tag": "wafer"},
            {"provider_name": "Sail Research", "tag": "sail-research/fp8"},
        ]
    }
}


def _settings(**overrides):
    values = {
        "llm_api_key": "sk-test",
        "llm_model_orchestrator": "test/model",
        "llm_model_assistant": "test/model",
        "llm_model_researcher": "test/model",
        "llm_model_refuter": "test/model",
        "llm_provider_by_role": {},
        "llm_model_extractor": "test/model",
        "llm_first_token_timeout_s": 0.05,
        "llm_first_token_failovers": 2,
        "llm_max_attempts": 4,
        "llm_retry_base_delay_s": 0,
    }
    return Settings(**(values | overrides))


def _sse(*chunks, pause_from=None, pause_s=0.0):
    async def body():
        for position, chunk in enumerate(chunks):
            if pause_from is not None and position >= pause_from:
                yield b": OPENROUTER PROCESSING\n\n"
                await asyncio.sleep(pause_s)
            yield f"data: {json.dumps(chunk)}\n\n".encode()
        yield b"data: [DONE]\n\n"

    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream", "x-generation-id": "gen-stream"},
        content=body(),
    )


def _client(handler, events):
    client = LlmClient(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    client.set_event_sink(events.append)
    return client


def _stalls(events):
    return [event for event in events if event["kind"] == "first_token_timeout"]


async def test_stall_aborts_and_fails_over_to_another_provider(monkeypatch):
    monkeypatch.setattr(llm, "get_settings", lambda: _settings())
    bodies, events, lookups = [], [], []

    async def handler(request):
        if request.url.path.endswith("/endpoints"):
            lookups.append(request.url.path)
            return httpx.Response(200, json=_ENDPOINTS)
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return _sse(
                {"provider": "Wafer", "choices": [{"delta": {"role": "assistant", "content": ""}}]},
                {"provider": "Wafer", "choices": [{"delta": {"content": "late"}}]},
                pause_from=1,
                pause_s=5.0,
            )
        return _sse(
            {
                "id": "gen-2",
                "model": "test/model",
                "provider": "Friendli",
                "choices": [{"delta": {"role": "assistant", "content": "ok"}}],
            },
            {
                "provider": "Friendli",
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1},
            },
        )

    client = _client(handler, events)
    try:
        reply = await client.converse("researcher", _MESSAGES, session_id="agent-3")
    finally:
        await client.aclose()

    assert reply.content == "ok"
    assert reply.provider == "Friendli"
    assert reply.prompt_tokens == 5
    assert reply.attempts == 1
    assert len(bodies) == 2
    assert bodies[0]["stream"] is True
    assert "provider" not in bodies[0]
    assert bodies[1]["provider"] == {"ignore": ["wafer"]}
    assert lookups == ["/api/v1/models/test/model/endpoints"]
    [stall] = _stalls(events)
    assert stall["provider"] == "Wafer"
    assert stall["provider_source"] == "stream"
    assert stall["elapsed_s"] >= 0.05
    assert stall["retrying"] is True
    assert stall["session_id"] == "agent-3"
    assert stall["generation_id"] == "gen-stream"
    assert "Wafer" in stall["error"]
    assert client.log.calls[-1].ok is True


async def test_stall_exhausts_failovers_and_raises_existing_error(monkeypatch):
    monkeypatch.setattr(llm, "get_settings", lambda: _settings(llm_first_token_failovers=1))
    bodies, events = [], []

    async def handler(request):
        if request.url.path.endswith("/endpoints"):
            return httpx.Response(200, json=_ENDPOINTS)
        body = json.loads(request.content)
        if not body.get("stream"):
            return httpx.Response(200, json={"provider": "Wafer"})
        bodies.append(body)
        await asyncio.sleep(5.0)
        return _sse({"choices": [{"delta": {"content": "never"}}]})

    client = _client(handler, events)
    try:
        await client.probe_providers()
        with pytest.raises(LlmUnavailable) as caught:
            await client.converse("researcher", _MESSAGES, session_id="agent-7")
    finally:
        await client.aclose()

    assert len(bodies) == 2
    assert bodies[0]["provider"] == {"order": ["Wafer"], "allow_fallbacks": True}
    assert bodies[1]["provider"] == {"ignore": ["wafer"]}
    first, second = _stalls(events)
    assert (first["provider"], first["provider_source"], first["retrying"]) == (
        "Wafer",
        "lock",
        True,
    )
    assert (second["provider"], second["provider_source"], second["retrying"]) == (
        None,
        None,
        False,
    )
    assert "unknown provider" in second["error"]
    assert any(event["kind"] == "provider_unlocked" for event in events)
    assert client._provider_locks == {}
    assert caught.value.attempts == 1
    assert "FirstTokenTimeout" in caught.value.detail
    assert client.log.calls[-1].ok is False


async def test_first_token_in_time_then_slow_stream_is_not_aborted(monkeypatch):
    monkeypatch.setattr(llm, "get_settings", lambda: _settings())
    bodies, events = [], []

    async def handler(request):
        bodies.append(json.loads(request.content))
        return _sse(
            {
                "id": "gen-3",
                "provider": "Friendli",
                "choices": [
                    {
                        "delta": {
                            "role": "assistant",
                            "reasoning": "thi",
                            "reasoning_details": [
                                {"type": "reasoning.text", "text": "thi", "index": 0}
                            ],
                        }
                    }
                ],
            },
            {
                "provider": "Friendli",
                "choices": [
                    {
                        "delta": {
                            "reasoning": "nk",
                            "reasoning_details": [
                                {"type": "reasoning.text", "text": "nk", "index": 0}
                            ],
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "search", "arguments": '{"q":'},
                                }
                            ],
                        }
                    }
                ],
            },
            {
                "provider": "Friendli",
                "choices": [
                    {
                        "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"x"}'}}]},
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 9, "completion_tokens": 4},
            },
            pause_from=1,
            pause_s=0.2,
        )

    client = _client(handler, events)
    try:
        reply = await client.converse("researcher", _MESSAGES)
    finally:
        await client.aclose()

    assert len(bodies) == 1
    assert _stalls(events) == []
    assert reply.duration_ms >= 400
    assert reply.provider == "Friendli"
    assert reply.completion_tokens == 4
    assert reply.message["reasoning"] == "think"
    assert reply.message["reasoning_details"] == [
        {"type": "reasoning.text", "text": "think", "index": 0}
    ]
    [call] = reply.tool_calls
    assert (call.id, call.name, call.arguments) == ("call_1", "search", {"q": "x"})


_FLEX = {"only": ["openai/flex"], "allow_fallbacks": False}


def _ok_stream(provider="OpenAI"):
    return _sse(
        {"id": "gen-ok", "model": "test/model", "provider": provider,
         "choices": [{"delta": {"role": "assistant", "content": "ok"}}]},
        {"provider": provider, "choices": [{"delta": {}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 5, "completion_tokens": 1}},
    )


async def test_a_pinned_role_sends_its_route_and_other_roles_do_not(monkeypatch):
    monkeypatch.setattr(
        llm, "get_settings", lambda: _settings(llm_provider_by_role={"researcher": _FLEX})
    )
    bodies, events = [], []

    async def handler(request):
        bodies.append(json.loads(request.content))
        return _ok_stream()

    client = _client(handler, events)
    try:
        await client.converse("researcher", _MESSAGES)
        await client.converse("orchestrator", _MESSAGES)
    finally:
        await client.aclose()

    assert bodies[0]["provider"] == _FLEX
    assert "provider" not in bodies[1]


async def test_a_stall_on_a_pinned_route_retries_the_same_route(monkeypatch):
    monkeypatch.setattr(
        llm, "get_settings", lambda: _settings(llm_provider_by_role={"refuter": _FLEX})
    )
    bodies, events, lookups = [], [], []

    async def handler(request):
        if request.url.path.endswith("/endpoints"):
            lookups.append(request.url.path)
            return httpx.Response(200, json=_ENDPOINTS)
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return _sse(
                {"provider": "OpenAI",
                 "choices": [{"delta": {"role": "assistant", "content": ""}}]},
                {"provider": "OpenAI", "choices": [{"delta": {"content": "late"}}]},
                pause_from=1,
                pause_s=5.0,
            )
        return _ok_stream()

    client = _client(handler, events)
    try:
        reply = await client.converse("refuter", _MESSAGES)
    finally:
        await client.aclose()

    assert reply.content == "ok"
    assert [body["provider"] for body in bodies] == [_FLEX, _FLEX]
    assert lookups == []
    [stall] = _stalls(events)
    assert stall["provider_source"] == "pinned"
    assert stall["ignored"] == []
    assert stall["retrying"] is True


def test_researcher_and_refuter_default_to_luna_on_flex():
    settings = Settings(_env_file=None)
    assert settings.llm_model_researcher == "openai/gpt-6-luna"
    assert settings.llm_model_refuter == "openai/gpt-6-luna"
    assert settings.llm_provider_by_role["researcher"] == _FLEX
    assert settings.llm_provider_by_role["refuter"] == _FLEX
    assert "orchestrator" not in settings.llm_provider_by_role

