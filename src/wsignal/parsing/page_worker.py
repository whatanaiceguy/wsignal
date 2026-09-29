from __future__ import annotations

import asyncio
import multiprocessing
import os
import threading
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor

from wsignal.config import get_settings
from wsignal.parsing.text import html_to_text, looks_blocked, page_metadata


class PageProcessingTimeout(TimeoutError):
    pass


_pool: ProcessPoolExecutor | None = None
_pool_lock = threading.Lock()
_pool_generation = 0


def process_page(raw: str, url: str = "") -> tuple[str, str | None, dict]:
    extracted = html_to_text(raw)
    blocked = looks_blocked(raw, extracted)
    metadata = page_metadata(raw, url)
    return extracted, blocked, metadata


def _get_pool() -> tuple[ProcessPoolExecutor, int]:
    global _pool
    global _pool_generation
    with _pool_lock:
        if _pool is None:
            _pool = ProcessPoolExecutor(
                max_workers=min(4, os.cpu_count() or 1),
                mp_context=multiprocessing.get_context("spawn"),
            )
            _pool_generation += 1
        return _pool, _pool_generation


def _discard_pool(pool: ProcessPoolExecutor, kill: bool) -> None:
    global _pool
    global _pool_generation
    with _pool_lock:
        if _pool is not pool:
            return
        _pool = None
        _pool_generation += 1
    processes = tuple((pool._processes or {}).values())
    if kill:
        for process in processes:
            if process.is_alive():
                process.kill()
        for process in processes:
            process.join(timeout=0.2)
    pool.shutdown(wait=False, cancel_futures=True)


async def run_page_processing(
    raw: str,
    timeout: float | None = None,
    processor: Callable[[str], tuple[str, str | None, dict]] | None = None,
    url: str = "",
) -> tuple[str, str | None, dict]:
    timeout_s = get_settings().page_parse_timeout_s if timeout is None else timeout
    worker = processor or process_page
    loop = asyncio.get_running_loop()
    for attempt in range(2):
        pool, generation = _get_pool()
        future = loop.run_in_executor(
            pool, worker, raw, url
        ) if processor is None else loop.run_in_executor(pool, worker, raw)
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout_s)
        except TimeoutError as exc:
            if isinstance(exc, PageProcessingTimeout):
                raise
            _discard_pool(pool, kill=True)
            raise PageProcessingTimeout(timeout_s) from None
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                _discard_pool(pool, kill=True)
                raise
            with _pool_lock:
                replaced = _pool is not pool or _pool_generation != generation
            if attempt == 0 and (replaced or pool._broken):
                continue
            raise
        except Exception:
            with _pool_lock:
                replaced = _pool is not pool or _pool_generation != generation
            if attempt == 0 and (replaced or pool._broken):
                continue
            raise
    raise RuntimeError("page processing retry exhausted")


async def shutdown_page_processor() -> None:
    global _pool
    global _pool_generation
    with _pool_lock:
        pool = _pool
        _pool = None
        if pool is not None:
            _pool_generation += 1
    if pool is not None:
        await asyncio.to_thread(pool.shutdown, wait=True, cancel_futures=True)
