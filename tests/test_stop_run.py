import asyncio
from types import SimpleNamespace

import httpx
import pytest

import wsignal.interface.app as app_module
import wsignal.interface.jobs as jobs_module
from wsignal.inference.pipeline import interruption_reason
from wsignal.interface.jobs import STOP_MESSAGE, RunSlot


@pytest.mark.asyncio
async def test_stop_cancels_the_active_run_with_the_ui_message(monkeypatch):
    seen = []

    async def fake_run_query(_query, on_run_id, **_kwargs):
        on_run_id(5)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as exc:
            seen.append(interruption_reason(exc))
            raise

    monkeypatch.setattr(jobs_module, "run_query", fake_run_query)
    slot = RunSlot()
    assert await slot.start("q") == 5

    assert await slot.stop(6) is False
    assert await slot.stop(5) is True
    assert slot.task.done()
    assert seen == [STOP_MESSAGE]
    assert await slot.stop(5) is False


def test_interruption_reason_without_a_message_names_the_exception():
    assert interruption_reason(asyncio.CancelledError()) == "run interrupted: CancelledError"
    assert interruption_reason(KeyboardInterrupt()) == "run interrupted: KeyboardInterrupt"


def _sessionmaker(run):
    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def get(self, _model, _run_id):
            return run

    return lambda: Session


class _Runner:
    def __init__(self, stops):
        self.stops = stops
        self.calls = []

    async def stop(self, run_id):
        self.calls.append(run_id)
        return self.stops


async def _post(monkeypatch, run, stops):
    runner = _Runner(stops)
    monkeypatch.setattr(app_module, "get_sessionmaker", _sessionmaker(run))
    monkeypatch.setattr(app_module, "runner", runner)
    transport = httpx.ASGITransport(app=app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/api/search/29/stop"), runner


@pytest.mark.asyncio
async def test_stop_endpoint_stops_and_reports_the_state(monkeypatch):
    response, runner = await _post(monkeypatch, SimpleNamespace(state="failed"), True)

    assert response.status_code == 200
    assert response.json() == {"run_id": 29, "state": "failed"}
    assert runner.calls == [29]


@pytest.mark.asyncio
async def test_stop_endpoint_refuses_a_run_this_server_is_not_running(monkeypatch):
    response, _runner = await _post(monkeypatch, SimpleNamespace(state="running"), False)

    assert response.status_code == 409
    assert "не выполняется" in response.json()["detail"]


@pytest.mark.asyncio
async def test_stop_endpoint_404s_an_unknown_run(monkeypatch):
    response, runner = await _post(monkeypatch, None, True)

    assert response.status_code == 404
    assert runner.calls == []
