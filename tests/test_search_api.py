import asyncio

import httpx
import pytest
from fastapi import HTTPException

import wsignal.interface.app as app_module
import wsignal.interface.jobs as jobs_module
from tests.helpers_api import FakeSessionmaker
from wsignal.interface.jobs import RunSlot


@pytest.mark.asyncio
async def test_post_returns_run_id_conflicts_while_active_then_allows_next(monkeypatch):
    slot = RunSlot()
    release = asyncio.Event()
    next_id = 100

    async def fake_run_query(_query, on_run_id, **_kwargs):
        nonlocal next_id
        next_id += 1
        on_run_id(next_id)
        await release.wait()

    monkeypatch.setattr(jobs_module, "run_query", fake_run_query)
    monkeypatch.setattr(app_module, "runner", slot)
    transport = httpx.ASGITransport(app=app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post("/api/search", json={"query": "first"})
        assert first.status_code == 202
        assert first.json() == {"run_id": 101}
        second = await client.post("/api/search", json={"query": "second"})
        assert second.status_code == 409
        assert second.json()["detail"]
        assert next_id == 101
        release.set()
        await slot.task
        release.clear()
        third = await client.post("/api/search", json={"query": "third"})
        assert third.status_code == 202
        assert third.json() == {"run_id": 102}
        release.set()
        await slot.task


@pytest.mark.asyncio
async def test_post_rejects_blank_query_without_starting_run(monkeypatch):
    class Runner:
        calls = 0

        async def start(self, *_args, **_kwargs):
            self.calls += 1
            return 12

    runner = Runner()
    monkeypatch.setattr(app_module, "runner", runner)
    transport = httpx.ASGITransport(app=app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        blank = await client.post("/api/search", json={"query": "   \t\n"})
    assert blank.status_code == 422
    assert runner.calls == 0


@pytest.mark.asyncio
async def test_unknown_run_response_is_404(monkeypatch):
    monkeypatch.setattr(
        app_module, "get_sessionmaker", lambda: FakeSessionmaker(run_exists=False)
    )
    with pytest.raises(HTTPException) as raised:
        await app_module.search_result(999)
    assert raised.value.status_code == 404


@pytest.mark.asyncio
async def test_event_endpoint_uses_last_event_id_and_streams_events(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        app_module, "get_sessionmaker", lambda: FakeSessionmaker(run_exists=True)
    )

    async def fake_events(_maker, run_id, after):
        captured.update(run_id=run_id, after=after)
        yield "id: 8\nevent: progress\ndata: {}\n\n"

    monkeypatch.setattr(app_module, "iter_events", fake_events)
    transport = httpx.ASGITransport(app=app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/search/7/events?after=2", headers={"Last-Event-ID": "7"}
        )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    assert response.text == "id: 8\nevent: progress\ndata: {}\n\n"
    assert captured == {"run_id": 7, "after": 7}
