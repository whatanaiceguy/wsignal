import asyncio
import time
from contextlib import asynccontextmanager

import pytest

import wsignal.inference.tools as tools
import wsignal.parsing.page_worker as page_worker
from tests.page_worker_helpers import slow_process_page
from wsignal.inference.tools import Toolbox
from wsignal.parsing.page_worker import (
    PageProcessingTimeout,
    run_page_processing,
    shutdown_page_processor,
)
from wsignal.parsing.text import page_metadata


def test_meta_extraction_handles_many_tags_and_trailing_quotes_quickly():
    raw = (
        "<!doctype html><html><head>"
        + '<meta name="k" content="v">' * 100
        + '"' * 30000
        + "</head><body>x</body></html>"
    )
    started = time.perf_counter()
    metadata = page_metadata(raw)
    elapsed = time.perf_counter() - started
    assert metadata == {
        "title": "",
        "site_name": "",
        "lang": "",
        "published_at": None,
        "date_source": None,
    }
    assert elapsed < 1.0


@pytest.mark.asyncio
async def test_timeout_does_not_block_loop_and_next_processing_works():
    ticks = 0
    running = True

    async def tick():
        nonlocal ticks
        while running:
            ticks += 1
            await asyncio.sleep(0.01)

    ticker = asyncio.create_task(tick())
    try:
        generation = page_worker._pool_generation
        with pytest.raises(PageProcessingTimeout, match="1.0"):
            await run_page_processing("<html><body>x</body></html>", 1.0, slow_process_page)
        before = ticks
        result = await run_page_processing("<html><body>fresh</body></html>", 5.0)
        assert result[0].strip() == "fresh"
        assert page_worker._pool_generation > generation + 1
        assert ticks > before
        assert ticks >= 10
    finally:
        running = False
        await ticker
        await shutdown_page_processor()


@pytest.mark.asyncio
async def test_fetch_returns_timeout_as_a_retrieval_error(monkeypatch):
    class Session:
        async def scalar(self, _statement):
            return None

    class Fetcher:
        @asynccontextmanager
        async def measure(self):
            yield {"requests": 1, "bytes": 10}

        async def get_text(self, _url):
            return "<html><body>x</body></html>"

    async def slow_processing(raw, url=""):
        from wsignal.parsing.page_worker import run_page_processing

        return await run_page_processing(raw, 0.1, slow_process_page)

    async def record_event(**_kwargs):
        return None

    monkeypatch.setattr(tools, "run_page_processing", slow_processing)
    monkeypatch.setattr(tools, "record_source_event", record_event)
    result = await Toolbox(Session(), Fetcher())._fetch({"url": "https://example.test"}, None)
    assert result["url"] == "https://example.test"
    assert result["error"] == "page processing exceeded 0.1 s"
    assert result["stored"] is False
    assert "retrieval failure" in result["note"]


@pytest.mark.asyncio
async def test_cancelling_processing_kills_worker_and_next_call_succeeds():
    generation = page_worker._pool_generation
    task = asyncio.create_task(
        run_page_processing("<html><body>slow</body></html>", 10.0, slow_process_page)
    )
    await asyncio.sleep(0.3)
    pool = page_worker._pool
    processes = tuple(pool._processes.values())
    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task

        assert page_worker._pool_generation > generation
        assert processes
        assert all(not process.is_alive() for process in processes)
        result = await asyncio.wait_for(
            run_page_processing("<html><body>after cancel</body></html>", 5.0),
            timeout=5.0,
        )
        assert "after cancel" in result[0]
    finally:
        for process in processes:
            if process.is_alive():
                process.kill()
                process.join(timeout=0.2)
        await shutdown_page_processor()


@pytest.mark.asyncio
async def test_default_processor_receives_the_url_as_a_keyword():
    try:
        _, _, metadata = await run_page_processing(
            "<html><body><p>story</p></body></html>",
            url="https://example.test/2025/06/19/story/",
        )
    finally:
        await shutdown_page_processor()
    assert str(metadata["published_at"]) == "2025-06-19"
    assert metadata["date_source"] == "url"
