import json

import httpx
import pytest

from wsignal.config import Settings
from wsignal.inference import llm
from wsignal.inference.llm import LlmClient

_MESSAGES = [{"role": "user", "content": "hi"}]


def _settings(**overrides):
    models = {
        "llm_model_orchestrator": "test/model",
        "llm_model_assistant": "test/model",
        "llm_model_researcher": "test/model",
        "llm_model_refuter": "test/model",
        "llm_provider_by_role": {},
        "llm_model_extractor": "test/model",
    }
    return Settings(**(models | overrides))


def _client(handler):
    return LlmClient(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


@pytest.mark.asyncio
async def test_probe_locks_provider_and_requests_send_fallback_preference(monkeypatch):
    monkeypatch.setattr(
        llm, "get_settings", lambda: _settings(llm_model_assistant="test/other")
    )
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(
            200,
            json={
                "provider": "Decart",
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            },
        )

    client = _client(handler)
    try:
        chosen = await client.probe_providers()
        await client.converse("orchestrator", _MESSAGES)
    finally:
        await client.aclose()

    assert chosen == {"test/model": "Decart", "test/other": "Decart"}
    assert {request["model"] for request in requests[:2]} == {"test/model", "test/other"}
    assert all(request["max_tokens"] <= 3 for request in requests[:2])
    assert all("reasoning" not in request for request in requests[:2])
    assert requests[2]["provider"] == {"order": ["Decart"], "allow_fallbacks": True}


@pytest.mark.asyncio
async def test_probe_failure_leaves_model_unlocked(monkeypatch):
    monkeypatch.setattr(llm, "get_settings", lambda: _settings())

    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(503, json={"error": "down"})

    client = _client(handler)
    try:
        assert await client.probe_providers() == {}
        assert client._provider_locks == {}
        assert bodies[0]["model"] == "test/model"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_provider_server_error_unlocks_and_retry_has_no_preference(monkeypatch):
    monkeypatch.setattr(
        llm,
        "get_settings",
        lambda: _settings(llm_max_attempts=2, llm_retry_base_delay_s=0),
    )
    bodies = []
    events = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            return httpx.Response(200, json={"provider": "Decart"})
        if len(bodies) == 2:
            return httpx.Response(503, json={"error": "temporary"})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
        )

    client = _client(handler)
    client.set_event_sink(events.append)
    try:
        await client.probe_providers()
        await client.converse("orchestrator", _MESSAGES)
    finally:
        await client.aclose()

    assert bodies[1]["provider"] == {"order": ["Decart"], "allow_fallbacks": True}
    assert "provider" not in bodies[2]
    assert client._provider_locks == {}
    unlocked = next(event for event in events if event["kind"] == "provider_unlocked")
    assert unlocked["reason"]


@pytest.mark.asyncio
async def test_provider_could_not_serve_message_unlocks_before_retry(monkeypatch):
    monkeypatch.setattr(
        llm,
        "get_settings",
        lambda: _settings(llm_max_attempts=2, llm_retry_base_delay_s=0),
    )
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            return httpx.Response(200, json={"provider": "Decart"})
        if len(bodies) == 2:
            return httpx.Response(400, json={"error": "Provider Decart could not serve"})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
        )

    client = _client(handler)
    try:
        await client.probe_providers()
        await client.converse("orchestrator", _MESSAGES)
    finally:
        await client.aclose()

    assert bodies[1]["provider"] == {"order": ["Decart"], "allow_fallbacks": True}
    assert "provider" not in bodies[2]


@pytest.mark.asyncio
async def test_disabled_setting_skips_probes(monkeypatch):
    monkeypatch.setattr(
        llm, "get_settings", lambda: _settings(llm_provider_lock_enabled=False)
    )
    calls = []
    client = _client(lambda request: calls.append(request))
    try:
        assert await client.probe_providers() == {}
    finally:
        await client.aclose()

    assert calls == []


@pytest.mark.asyncio
async def test_each_resume_probe_learns_provider_again(monkeypatch):
    monkeypatch.setattr(llm, "get_settings", lambda: _settings())
    providers = iter(["Decart", "Wafer"])
    client = _client(lambda _request: httpx.Response(200, json={"provider": next(providers)}))
    try:
        assert await client.probe_providers() == {"test/model": "Decart"}
        assert await client.probe_providers() == {"test/model": "Wafer"}
    finally:
        await client.aclose()

    assert client._provider_locks == {"test/model": "Wafer"}
