import json

import httpx
import pytest
from pydantic import BaseModel

from wsignal.config import Settings
from wsignal.inference import llm
from wsignal.inference.llm import LlmClient, request_body

_MESSAGES = [{"role": "user", "content": "hi"}]


@pytest.mark.parametrize(
    "role,effort",
    [("orchestrator", "high"), ("assistant", "high")],
)
def test_default_request_body_reasoning_effort_by_role(role, effort):
    body = request_body("z-ai/glm-5.3", _MESSAGES, role=role)

    assert body["reasoning"] == {"effort": effort}


def test_request_body_honours_reasoning_effort_override(monkeypatch):
    settings = Settings(llm_reasoning_effort_by_role={"assistant": "low"})
    monkeypatch.setattr(llm, "get_settings", lambda: settings)

    body = request_body("z-ai/glm-5.3", _MESSAGES, role="assistant")

    assert body["reasoning"] == {"effort": "low"}


def test_role_missing_from_reasoning_settings_has_no_reasoning_key(monkeypatch):
    settings = Settings(llm_reasoning_effort_by_role={})
    monkeypatch.setattr(llm, "get_settings", lambda: settings)

    body = request_body("z-ai/glm-5.3", _MESSAGES, role="assistant")

    assert "reasoning" not in body


class _Result(BaseModel):
    value: str


@pytest.mark.asyncio
async def test_structured_request_includes_reasoning_effort(monkeypatch):
    settings = Settings(
        llm_model_extractor="z-ai/glm-5.3",
        llm_reasoning_effort_by_role={"extractor": "medium"},
    )
    monkeypatch.setattr(llm, "get_settings", lambda: settings)
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"value":"ok"}'}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    client = LlmClient(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        result = await client.structured("extractor", "system", "user", _Result)
    finally:
        await client.aclose()

    assert result == _Result(value="ok")
    assert captured["reasoning"] == {"effort": "medium"}
    assert captured["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [402, 400, 403])
async def test_structured_does_not_retry_non_transient_4xx_and_records_payload_usage(
    monkeypatch, status
):
    settings = Settings(llm_max_attempts=4, llm_retry_base_delay_s=0)
    monkeypatch.setattr(llm, "get_settings", lambda: settings)
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            status,
            json={"error": "failed", "usage": {"prompt_tokens": 7, "completion_tokens": 3}},
        )

    client = LlmClient(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        with pytest.raises(llm.LlmError):
            await client.structured("extractor", "system", "user", _Result)
    finally:
        await client.aclose()

    assert calls == 1
    assert client.log.calls[-1].prompt_tokens == 7
    assert client.log.calls[-1].completion_tokens == 3


@pytest.mark.asyncio
async def test_structured_retries_429_using_retry_policy(monkeypatch):
    settings = Settings(llm_max_attempts=3, llm_retry_base_delay_s=0)
    monkeypatch.setattr(llm, "get_settings", lambda: settings)
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, json={"error": "busy"})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"value":"ok"}'}}]},
        )

    client = LlmClient(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        result = await client.structured("extractor", "system", "user", _Result)
    finally:
        await client.aclose()

    assert result == _Result(value="ok")
    assert calls == 2


@pytest.mark.asyncio
async def test_converse_retry_exhaustion_raises_unavailable_with_attempts(monkeypatch):
    settings = Settings(llm_max_attempts=2, llm_retry_base_delay_s=0)
    monkeypatch.setattr(llm, "get_settings", lambda: settings)
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"error": "temporarily unavailable"})

    client = LlmClient(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        with pytest.raises(llm.LlmUnavailable) as raised:
            await client.converse("assistant", _MESSAGES)
    finally:
        await client.aclose()

    assert calls == 2
    assert raised.value.attempts == 2
    assert client.log.calls[-1].attempts == 2
    assert client.log.calls[-1].ok is False


@pytest.mark.asyncio
async def test_converse_uses_success_payload_usage_and_cost_after_retry(monkeypatch):
    settings = Settings(llm_max_attempts=3, llm_retry_base_delay_s=0)
    monkeypatch.setattr(llm, "get_settings", lambda: settings)
    calls = 0
    successful_payload = {
        "id": "generation-success",
        "provider": "provider-success",
        "choices": [{"message": {"role": "assistant", "content": "ok"}}],
        "usage": {
            "prompt_tokens": 12,
            "completion_tokens": 5,
            "prompt_tokens_details": {"cached_tokens": 3, "cache_write_tokens": 2},
            "completion_tokens_details": {"reasoning_tokens": 1},
            "cost": 0.0123,
        },
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, json={"usage": {"prompt_tokens": 999}, "error": "retry"})
        return httpx.Response(200, json=successful_payload)

    client = LlmClient(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        reply = await client.converse("assistant", _MESSAGES)
    finally:
        await client.aclose()

    assert calls == 2
    assert reply.prompt_tokens == 12
    assert reply.completion_tokens == 5
    assert reply.cached_tokens == 3
    assert reply.cache_write_tokens == 2
    assert reply.reasoning_tokens == 1
    assert reply.cost_usd == 0.0123
    assert reply.attempts == 2
    assert reply.provider == "provider-success"
    assert reply.generation_id == "generation-success"
    assert client.log.calls[-1].prompt_tokens == 12
    assert client.log.calls[-1].completion_tokens == 5
