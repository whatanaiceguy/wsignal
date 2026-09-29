import asyncio
import time

from wsignal.parsing.http import TokenBucket


async def test_waiters_queue_rather_than_burst():
    bucket = TokenBucket(per_second=50.0)
    started = time.monotonic()
    await asyncio.gather(*(bucket.acquire() for _ in range(5)))
    elapsed = time.monotonic() - started
    assert elapsed >= 0.06


def test_a_429_widens_the_interval():
    bucket = TokenBucket(per_second=1 / 3)
    assert bucket.interval == 3.0
    assert bucket.widen() == 6.0
    assert bucket.widen() == 12.0


def test_widening_is_capped():
    bucket = TokenBucket(per_second=1 / 3, max_interval=10.0)
    for _ in range(10):
        bucket.widen()
    assert bucket.interval == 10.0


async def test_widening_changes_observable_waiter_spacing():
    bucket = TokenBucket(per_second=100.0)
    bucket.widen()
    started = time.monotonic()
    await bucket.acquire()
    await bucket.acquire()
    assert time.monotonic() - started >= 0.015
