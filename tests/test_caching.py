import asyncio

import pytest

from wsignal.inference.llm import _caches_only_when_asked, request_body
from wsignal.inference.tools import RetrievalCache

_MESSAGES = [{"role": "user", "content": "hi"}]


@pytest.mark.asyncio
async def test_run_once_coalesces_concurrent_calls_and_cleans_up_after_exception():
    cache = RetrievalCache()
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return "shared"

    owner = asyncio.create_task(cache.run_once(("shared",), factory))
    await started.wait()
    waiter = asyncio.create_task(cache.run_once(("shared",), factory))
    await asyncio.sleep(0)
    release.set()
    assert await asyncio.gather(owner, waiter) == ["shared", "shared"]
    assert calls == 1

    failures = 0

    async def fail_once():
        nonlocal failures
        failures += 1
        if failures == 1:
            raise ValueError("factory failed")
        return "recovered"

    with pytest.raises(ValueError, match="factory failed"):
        await cache.run_once(("failure",), fail_once)
    assert await cache.run_once(("failure",), fail_once) == "recovered"
    assert failures == 2


@pytest.mark.asyncio
async def test_cancelled_cache_owner_does_not_leave_waiters_blocked():
    cache = RetrievalCache()
    started = asyncio.Event()

    async def factory():
        started.set()
        await asyncio.Event().wait()

    owner = asyncio.create_task(cache.run_once(("key",), factory))
    await started.wait()
    waiter = asyncio.create_task(cache.run_once(("key",), factory))
    await asyncio.sleep(0)
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner
    with pytest.raises(RuntimeError, match="interrupted"):
        await asyncio.wait_for(waiter, 0.2)
    assert await cache.run_once(("key",), lambda: _constant("recovered")) == "recovered"


async def _constant(value):
    return value


def test_anthropic_and_alibaba_have_to_be_asked():
    assert _caches_only_when_asked("anthropic/claude-opus-5")
    assert _caches_only_when_asked("anthropic/claude-sonnet-4.6")
    assert _caches_only_when_asked("qwen/qwen3-max")


def test_the_providers_that_cache_by_themselves_are_left_alone():
    for model in (
        "openai/gpt-5.6-luna",
        "z-ai/glm-5.3",
        "google/gemini-2.5-pro",
        "deepseek/deepseek-chat",
        "x-ai/grok-4",
        "moonshotai/kimi-k2",
    ):
        assert not _caches_only_when_asked(model), model


def test_an_explicit_cache_model_is_asked_and_an_automatic_one_is_not():
    anthropic = request_body("anthropic/claude-opus-5", _MESSAGES, role="unconfigured")
    assert anthropic["cache_control"] == {"type": "ephemeral"}

    glm = request_body("z-ai/glm-5.3", _MESSAGES, role="unconfigured")
    assert "cache_control" not in glm


def test_sticky_routing_is_sent_for_every_provider():
    for model in ("anthropic/claude-opus-5", "z-ai/glm-5.3", "openai/gpt-5.6-luna"):
        body = request_body(model, _MESSAGES, session_id="agent-135", role="unconfigured")
        assert body["session_id"] == "agent-135", model


def test_a_session_id_is_cut_to_the_length_the_api_accepts():
    body = request_body("z-ai/glm-5.3", _MESSAGES, session_id="x" * 400, role="unconfigured")
    assert len(body["session_id"]) == 256


def test_nothing_else_about_the_request_changed():
    tools = [{"type": "function", "function": {"name": "search"}}]
    body = request_body(
        "openai/gpt-5.6-luna", _MESSAGES, tools, temperature=0.0, role="unconfigured"
    )
    assert body["model"] == "openai/gpt-5.6-luna"
    assert body["messages"] == _MESSAGES
    assert body["temperature"] == 0.0
    assert body["tools"] == tools
    assert body["tool_choice"] == "auto"
    assert set(body) == {"model", "messages", "temperature", "tools", "tool_choice"}


def test_a_call_without_tools_sends_no_tool_choice():
    body = request_body("openai/gpt-5.6-luna", _MESSAGES, role="unconfigured")
    assert "tools" not in body and "tool_choice" not in body
