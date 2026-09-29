from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

import wsignal.interface.app as app_module
from wsignal.interface.jobs import RunAlreadyActive


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
    def __init__(self, busy=False):
        self.calls = []
        self.busy = busy

    async def start(self, query, **kwargs):
        if self.busy:
            raise RunAlreadyActive
        self.calls.append((query, kwargs))
        return kwargs["resume_run_id"]


async def _post(monkeypatch, run, body=None, runner=None):
    runner = runner or _Runner()
    monkeypatch.setattr(app_module, "get_sessionmaker", _sessionmaker(run))
    monkeypatch.setattr(app_module, "runner", runner)
    transport = httpx.ASGITransport(app=app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/search/29/resume", json=body)
    return response, runner


def _run(state, seconds_ago):
    return SimpleNamespace(
        state=state, heartbeat_at=datetime.now(UTC) - timedelta(seconds=seconds_ago)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["running", "failed", "exhausted"])
async def test_a_stopped_run_resumes_with_an_optional_new_budget(monkeypatch, state):
    response, runner = await _post(monkeypatch, _run(state, 600), {"max_cost_usd": 2.02})

    assert response.status_code == 202
    assert response.json() == {"run_id": 29}
    assert runner.calls == [("", {"resume_run_id": 29, "max_cost_usd": 2.02})]


@pytest.mark.asyncio
async def test_resume_without_a_body_keeps_the_stored_budget(monkeypatch):
    response, runner = await _post(monkeypatch, _run("failed", 600))

    assert response.status_code == 202
    assert runner.calls == [("", {"resume_run_id": 29, "max_cost_usd": None})]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("run", "status", "detail"),
    [
        (None, 404, "не найден"),
        (_run("finished", 600), 409, "завершено"),
        (_run("running", 5), 409, "ещё выполняется"),
    ],
)
async def test_resume_refuses_missing_finished_and_live_runs(monkeypatch, run, status, detail):
    response, runner = await _post(monkeypatch, run)

    assert response.status_code == status
    assert detail in response.json()["detail"]
    assert runner.calls == []


@pytest.mark.asyncio
async def test_resume_conflicts_while_another_run_is_active(monkeypatch):
    response, _runner = await _post(monkeypatch, _run("failed", 600), runner=_Runner(busy=True))

    assert response.status_code == 409
    assert "Другое исследование" in response.json()["detail"]
