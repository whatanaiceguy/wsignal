import asyncio
import time

import httpx
import pytest

from wsignal.retry import (
    RetryExhausted,
    backoff_s,
    is_transient,
    retry_after_s,
    with_retries,
)


def _status_error(code: int, headers: dict | None = None) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.test/")
    response = httpx.Response(code, headers=headers or {}, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


@pytest.mark.parametrize("code", [408, 409, 425, 429, 500, 502, 503, 504])
def test_transient_statuses_are_worth_asking_again(code):
    assert is_transient(_status_error(code)) is True


@pytest.mark.parametrize("code", [400, 401, 402, 403, 404, 406, 422])
def test_refusals_are_answers_not_failures(code):
    assert is_transient(_status_error(code)) is False


def test_406_can_be_enabled_for_a_single_caller():
    assert is_transient(_status_error(406), status_also=frozenset({406})) is True
    assert is_transient(_status_error(406)) is False


def test_transport_failures_are_transient():
    assert is_transient(httpx.ConnectTimeout("no route")) is True
    assert is_transient(httpx.ReadTimeout("slow")) is True
    assert is_transient(httpx.RemoteProtocolError("hung up")) is True


def test_caller_may_name_its_own_transient_type():
    class Malformed(Exception):
        pass

    assert is_transient(Malformed(), ()) is False
    assert is_transient(Malformed(), (Malformed,)) is True


def test_retry_after_seconds_is_honoured():
    assert retry_after_s(_status_error(429, {"retry-after": "7"})) == 7.0


def test_retry_after_http_date_is_honoured():
    when = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(time.time() + 30))
    delay = retry_after_s(_status_error(503, {"retry-after": when}))
    assert delay is not None and 20 <= delay <= 40


def test_no_retry_after_header_is_none():
    assert retry_after_s(_status_error(429)) is None
    assert retry_after_s(httpx.ConnectTimeout("x")) is None


def test_backoff_is_bounded_and_jittered():
    seen = {backoff_s(5, base=1.0, cap=3.0) for _ in range(50)}
    assert all(0.0 <= value <= 3.0 for value in seen)
    assert len(seen) > 1


async def test_succeeds_after_a_transient_failure():
    calls = []

    async def flaky(remaining_s):
        calls.append(remaining_s)
        if len(calls) < 3:
            raise _status_error(503)
        return "ok"

    value, attempts = await with_retries(
        flaky, deadline_s=5.0, max_attempts=4, base_delay=0.01, max_delay=0.02
    )
    assert value == "ok"
    assert attempts == 3


async def test_the_attempt_is_told_what_is_left():
    seen = []

    async def record(remaining_s):
        seen.append(remaining_s)
        raise _status_error(500)

    with pytest.raises(RetryExhausted):
        await with_retries(
            record, deadline_s=2.0, max_attempts=3, base_delay=0.01, max_delay=0.02
        )
    assert len(seen) == 3
    assert seen[0] <= 2.0
    assert seen[-1] < seen[0]


async def test_a_refusal_is_not_retried():
    calls = 0

    async def refused(remaining_s):
        nonlocal calls
        calls += 1
        raise _status_error(402)

    with pytest.raises(RetryExhausted) as caught:
        await with_retries(refused, deadline_s=5.0, max_attempts=4, base_delay=0.01)
    assert calls == 1
    assert caught.value.reason == "fatal"
    assert caught.value.status == 402


async def test_attempts_run_out():
    async def always(remaining_s):
        raise _status_error(504)

    with pytest.raises(RetryExhausted) as caught:
        await with_retries(
            always, deadline_s=10.0, max_attempts=2, base_delay=0.01, max_delay=0.02
        )
    assert caught.value.reason == "attempts"
    assert caught.value.attempts == 2


async def test_the_deadline_stops_it_even_with_attempts_left():
    async def slow(remaining_s):
        await asyncio.sleep(0.05)
        raise _status_error(503)

    started = time.monotonic()
    with pytest.raises(RetryExhausted) as caught:
        await with_retries(
            slow, deadline_s=0.2, max_attempts=50, base_delay=0.01, max_delay=0.02
        )
    assert caught.value.reason == "deadline"
    assert caught.value.attempts < 50
    assert time.monotonic() - started < 1.0


async def test_queueing_is_not_charged_to_the_deadline():
    attempts = 0

    async def queue():
        await asyncio.sleep(0.15)

    async def flaky(remaining_s):
        nonlocal attempts
        attempts += 1
        assert remaining_s > 0.25
        if attempts < 3:
            raise _status_error(503)
        return "ok"

    value, n = await with_retries(
        flaky,
        deadline_s=0.4,
        max_attempts=4,
        base_delay=0.01,
        max_delay=0.02,
        before_attempt=queue,
    )
    assert value == "ok"
    assert n == 3


async def test_the_deadline_still_bites_on_slow_attempts():
    async def queue():
        await asyncio.sleep(0.05)

    async def slow(remaining_s):
        await asyncio.sleep(0.12)
        raise _status_error(503)

    with pytest.raises(RetryExhausted) as caught:
        await with_retries(
            slow,
            deadline_s=0.3,
            max_attempts=50,
            base_delay=0.01,
            max_delay=0.02,
            before_attempt=queue,
        )
    assert caught.value.reason == "deadline"
    assert caught.value.attempts < 50


async def test_attempt_timeout_is_cut_and_exhausted():
    async def always_slow(_remaining_s):
        await asyncio.sleep(1.0)

    started = time.monotonic()
    with pytest.raises(RetryExhausted) as caught:
        await with_retries(
            always_slow,
            deadline_s=0.05,
            max_attempts=4,
            base_delay=0.01,
            max_delay=0.01,
        )

    assert caught.value.reason == "deadline"
    assert caught.value.attempts == 1
    assert "exceeded remaining deadline" in caught.value.last_error
    assert time.monotonic() - started < 0.5


async def test_before_attempt_has_an_independent_ceiling():
    async def slow_queue():
        await asyncio.sleep(1.0)

    async def never_called(_remaining_s):
        pytest.fail("call must not start after the queue ceiling")

    with pytest.raises(RetryExhausted) as caught:
        await with_retries(
            never_called,
            deadline_s=5.0,
            before_attempt=slow_queue,
            queue_timeout_s=0.02,
        )

    assert caught.value.reason == "queue_timeout"
    assert "before_attempt exceeded queue timeout of 0.0s" in caught.value.last_error


async def test_cancellation_is_not_a_retry():
    calls = 0

    async def cancelled(remaining_s):
        nonlocal calls
        calls += 1
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await with_retries(cancelled, deadline_s=5.0, max_attempts=4, base_delay=0.01)
    assert calls == 1
