from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from email.utils import parsedate_to_datetime

import httpx

from wsignal.config import get_settings

RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

_TRANSIENT_TRANSPORT = (
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
    httpx.ProxyError,
    httpx.NetworkError,
)


class RetryExhausted(RuntimeError):

    def __init__(
        self,
        label: str,
        reason: str,
        attempts: int,
        elapsed_s: float,
        last_error: str,
        status: int | None = None,
    ) -> None:
        self.label = label
        self.reason = reason
        self.attempts = attempts
        self.elapsed_s = elapsed_s
        self.last_error = last_error
        self.status = status
        super().__init__(
            f"{label}: gave up after {attempts} attempt(s) in {elapsed_s:.1f}s "
            f"({reason}): {last_error}"
        )


@dataclass(slots=True)
class RetryNote:
    attempt: int
    delay_s: float
    error: str
    status: int | None


def status_of(exc: BaseException) -> int | None:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code
    return None


def is_transient(
    exc: BaseException,
    also: tuple[type[BaseException], ...] = (),
    status_also: frozenset[int] = frozenset(),
) -> bool:
    if isinstance(exc, _TRANSIENT_TRANSPORT) or (also and isinstance(exc, also)):
        return True
    status = status_of(exc)
    return status is not None and status in RETRYABLE_STATUS | status_also


def retry_after_s(exc: BaseException) -> float | None:
    if not isinstance(exc, httpx.HTTPStatusError):
        return None
    raw = exc.response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    return max(0.0, when.timestamp() - time.time())


def backoff_s(attempt: int, base: float, cap: float) -> float:
    ceiling = min(cap, base * (2 ** (attempt - 1)))
    return random.uniform(0.0, ceiling)


async def with_retries[T](
    call: Callable[[float], Awaitable[T]],
    *,
    deadline_s: float,
    max_attempts: int = 4,
    base_delay: float = 1.0,
    max_delay: float = 20.0,
    label: str = "call",
    transient_also: tuple[type[BaseException], ...] = (),
    status_also: frozenset[int] = frozenset(),
    before_attempt: Callable[[], Awaitable[None]] | None = None,
    queue_timeout_s: float | None = None,
    on_retry: Callable[[RetryNote], None] | None = None,
) -> tuple[T, int]:
    started = time.monotonic()
    queued_s = 0.0
    queue_timeout = (
        get_settings().retry_queue_timeout_s
        if queue_timeout_s is None
        else queue_timeout_s
    )
    attempt = 0
    last_error = ""
    last_status: int | None = None

    def spent() -> float:
        return time.monotonic() - started - queued_s

    while True:
        attempt += 1
        if spent() >= deadline_s:
            raise RetryExhausted(
                label, "deadline", attempt - 1, time.monotonic() - started,
                last_error or "no time left to try", last_status,
            )

        if before_attempt is not None:
            mark = time.monotonic()
            queue_scope = asyncio.timeout(queue_timeout)
            try:
                async with queue_scope:
                    await before_attempt()
            except TimeoutError as exc:
                queued_s += time.monotonic() - mark
                if not queue_scope.expired():
                    raise
                detail = f"before_attempt exceeded queue timeout of {queue_timeout:.1f}s"
                raise RetryExhausted(
                    label,
                    "queue_timeout",
                    attempt - 1,
                    time.monotonic() - started,
                    detail,
                    last_status,
                ) from exc
            except BaseException:
                queued_s += time.monotonic() - mark
                raise
            else:
                queued_s += time.monotonic() - mark

        remaining = deadline_s - spent()
        try:
            async with asyncio.timeout(remaining) as timeout_scope:
                value = await call(remaining)
            return value, attempt
        except Exception as exc:
            timed_out = isinstance(exc, TimeoutError) and timeout_scope.expired()
            if timed_out:
                last_error = (
                    f"TimeoutError: attempt exceeded remaining deadline ({remaining:.1f}s)"
                )[:300]
                last_status = None
            else:
                last_error = f"{type(exc).__name__}: {exc}"[:300]
                last_status = status_of(exc)

            if not timed_out and not isinstance(exc, TimeoutError) and not is_transient(
                exc, transient_also, status_also
            ):
                raise RetryExhausted(
                    label, "fatal", attempt, time.monotonic() - started,
                    last_error, last_status,
                ) from exc

            if attempt >= max_attempts:
                raise RetryExhausted(
                    label, "attempts", attempt, time.monotonic() - started,
                    last_error, last_status,
                ) from exc

            delay = 0.0 if timed_out else retry_after_s(exc)
            if delay is None:
                delay = backoff_s(attempt, base_delay, max_delay)

            if on_retry is not None:
                on_retry(RetryNote(attempt, delay, last_error, last_status))

            if delay >= deadline_s - spent():
                raise RetryExhausted(
                    label, "deadline", attempt, time.monotonic() - started,
                    last_error, last_status,
                ) from exc

            await asyncio.sleep(delay)
