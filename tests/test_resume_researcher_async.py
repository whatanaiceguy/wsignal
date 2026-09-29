import asyncio
from types import SimpleNamespace

import pytest

from wsignal.inference.agent import AgentResult
from wsignal.inference.orchestration import Orchestration


class _Rows:
    def all(self):
        return []


class _Session:
    def __init__(self, target, field):
        self.target = target
        self.field = field

    async def get(self, model, identity):
        if model.__name__ == "Agent":
            return self.target if identity == self.target.id else None
        if model.__name__ == "Field":
            return self.field if identity == self.field.id else None
        return None

    async def scalar(self, statement):
        return self.field if "fields." in str(statement) else None

    async def scalars(self, _statement):
        return _Rows()

    async def commit(self):
        return None


@pytest.mark.asyncio
async def test_researcher_resume_returns_immediately_and_collects_answer(monkeypatch):
    target = SimpleNamespace(
        id=42, run_id=1, role="researcher", state="idle", parent_agent_id=7, brief="brief"
    )
    field = SimpleNamespace(id=12, run_id=1, agent_id=42, focus="research focus")
    session = _Session(target, field)
    finished = asyncio.Event()
    resume_started = asyncio.Event()

    class SessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *_exc):
            return None

    class Runtime:
        def __init__(self, *_args, **_kwargs):
            self._cost_budget = None

        async def resume(self, agent_id, message, **_kwargs):
            assert agent_id == 42
            assert message == "Continue research"
            resume_started.set()
            await finished.wait()
            return AgentResult(42, "answer", 2, "answered", 1, None)

    async def effort_for(_session, _agent_ids):
        return {"searches_run": 3, "sources_checked": 2}

    monkeypatch.setattr("wsignal.inference.orchestration.AgentRuntime", Runtime)
    monkeypatch.setattr("wsignal.inference.orchestration.effort_for", effort_for)
    monkeypatch.setattr("wsignal.inference.orchestration.Toolbox", lambda *_args: object())

    orchestration = Orchestration(
        session=session,
        sessionmaker=SessionContext,
        runtime=None,
        llm=None,
        fetcher=None,
        run=SimpleNamespace(id=1),
        prompts={},
        owner_agent_id=7,
    )
    response = await asyncio.wait_for(
        orchestration._resume_agent(
            {"agent_id": 42, "message": "Continue research"}, 7
        ),
        timeout=0.2,
    )

    assert response == {
        "resumed": True,
        "agent_id": 42,
        "running": 1,
        "note": "the researcher is running and will return through collect",
    }
    await resume_started.wait()
    refused = await orchestration._resume_agent(
        {"agent_id": 42, "message": "Again"}, 7
    )
    assert "still running" in refused["error"]

    finished.set()
    await asyncio.gather(*orchestration._inflight.values())
    orchestration._partition_worker = False
    orchestration._sink = None
    collected = await orchestration._collect({}, 7)

    assert collected["collected"] == 1
    assert collected["returns"] == [
        {
            "kind": "research",
            "field_id": 12,
            "focus": "research focus",
            "agent_id": 42,
            "stopped": "answered",
            "steps": 2,
            "effort": {"searches_run": 3, "sources_checked": 2},
            "analysis": "answer",
            "submitted": False,
            "note": (
                "agent did not submit structured findings; final text truncated to "
                "3000 characters"
            ),
        }
    ]
    assert collected["still_running"] == []


@pytest.mark.asyncio
async def test_researcher_resume_is_refused_when_already_inflight():
    target = SimpleNamespace(
        id=42, run_id=1, role="researcher", state="idle", parent_agent_id=7
    )
    session = _Session(target, SimpleNamespace(id=12, agent_id=42, focus="focus"))
    orchestration = object.__new__(Orchestration)
    orchestration._cost_budget = None
    orchestration._session = session
    orchestration._run = SimpleNamespace(id=1)
    orchestration._partition_worker = False
    orchestration._owner_agent_id = 7
    orchestration._inflight = {42: asyncio.create_task(asyncio.sleep(1))}

    result = await orchestration._resume_agent(
        {"agent_id": 42, "message": "Again"}, 7
    )

    assert result == {"error": "agent is still running; collect before starting more work"}
    await orchestration._inflight[42]
