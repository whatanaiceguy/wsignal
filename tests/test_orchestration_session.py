from types import SimpleNamespace

import pytest
from sqlalchemy.exc import InterfaceError, MissingGreenlet

from wsignal.inference.agent import AgentResult, AgentRuntime
from wsignal.inference.fleet import OrchestratorFleet
from wsignal.inference.orchestration import Orchestration
from wsignal.inference.tools import Toolbox


class _Rows:
    def all(self):
        return []


class _WorkerSession:
    async def get(self, _model, identity):
        return SimpleNamespace(id=identity)

    async def scalars(self, _statement):
        return _Rows()

    async def commit(self):
        pass


class _SessionContext:
    async def __aenter__(self):
        return _WorkerSession()

    async def __aexit__(self, *_exc):
        pass


class _SessionMaker:
    def __call__(self):
        return _SessionContext()


class _ForbiddenOrchestratorSession:

    def __init__(self):
        self.reads = 0

    async def scalars(self, _statement):
        self.reads += 1
        raise AssertionError("background refuter used the orchestrator session")


class _Runtime:
    def __init__(self, *_args, **_kwargs):
        pass

    async def run(self, *_args, **_kwargs):
        return AgentResult(2, "attack", 1, "answered", 0)

    async def resume(self, *_args, **_kwargs):
        return AgentResult(1, "rebuttal", 1, "answered", 0)


@pytest.mark.parametrize(
    "role,expected",
    [("orchestrator", 300), ("researcher", 40), ("refuter", 30)],
)
def test_max_steps_are_selected_by_agent_role(monkeypatch, role, expected):
    from wsignal.config import Settings

    monkeypatch.setattr(
        "wsignal.inference.orchestration.get_settings",
        lambda: Settings(researcher_max_steps=40, refuter_max_steps=30),
    )
    orchestration = object.__new__(Orchestration)
    orchestration._agent_max_steps = 150
    orchestration._max_model_calls = 300

    assert orchestration._max_steps_for(role) == expected


@pytest.mark.asyncio
async def test_background_refuter_never_uses_orchestrator_session(monkeypatch):
    import wsignal.inference.orchestration as module

    monkeypatch.setattr(module, "AgentRuntime", _Runtime)

    async def effort_for(_session, _agent_ids):
        return {"searches_run": 0, "sources_checked": 0, "tool_calls": 0}

    monkeypatch.setattr(module, "effort_for", effort_for)

    orchestration = object.__new__(Orchestration)
    orchestration._session = _ForbiddenOrchestratorSession()
    orchestration._sessionmaker = _SessionMaker()
    orchestration._llm = object()
    orchestration._fetcher = object()
    orchestration._cache = object()
    orchestration._agent_max_steps = 2
    orchestration._max_model_calls = 300
    orchestration._sink = None
    orchestration._inflight = {2: object()}

    saved = {}

    async def save_refutation(review_id, payload, state):
        saved.update(review_id=review_id, payload=payload, state=state)

    orchestration._save_refutation = save_refutation

    await orchestration._refuter_task(7, 2, 1, "attack this", "claim", True)

    assert orchestration._session.reads == 0
    assert saved["state"] == "completed"
    assert saved["payload"]["rebuttal"] == "rebuttal"


class _RollbackSession:
    def __init__(self):
        self.rollbacks = 0

    async def rollback(self):
        self.rollbacks += 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        MissingGreenlet("greenlet_spawn has not been called"),
        InterfaceError(
            "statement",
            {},
            RuntimeError("cannot perform operation: another operation is in progress"),
        ),
    ],
)
async def test_session_failures_escape_tool_results(error):
    session = _RollbackSession()
    toolbox = Toolbox(session, None)

    async def fail(_arguments, _agent_id):
        raise error

    toolbox.register(
        {"type": "function", "function": {"name": "broken", "parameters": {}}},
        fail,
        ("orchestrator",),
    )

    with pytest.raises(type(error)):
        await toolbox.call("broken", {}, 1)
    assert session.rollbacks == 1


class _RecoverySession:
    def __init__(self, turns):
        self.turns = turns
        self.resets = 0

    async def reset(self):
        self.resets += 1

    async def scalars(self, _statement):
        turns = self.turns

        class Rows:
            def all(self):
                return turns

        return Rows()


@pytest.mark.asyncio
async def test_recovery_closes_dangling_tool_calls_in_the_transcript():
    turns = [
        SimpleNamespace(
            kind="assistant",
            content={
                "message": {
                    "tool_calls": [
                        {"id": "done", "function": {"name": "read_fields"}},
                        {"id": "lost", "function": {"name": "collect"}},
                    ]
                }
            },
        ),
        SimpleNamespace(kind="tool_result", content={"id": "done"}),
    ]
    runtime = object.__new__(AgentRuntime)
    runtime._session = _RecoverySession(turns)
    appended = []

    async def append(agent_id, kind, content, **_accounting):
        appended.append((agent_id, kind, content))

    runtime._append = append

    recovered = await runtime.recover_session(42, MissingGreenlet("dead session"))

    assert runtime._session.resets == 1
    assert recovered == 1
    assert appended[0][0:2] == (42, "tool_result")
    assert appended[0][2]["id"] == "lost"
    assert appended[0][2]["name"] == "collect"
    assert appended[0][2]["payload"]["retryable"] is True


class _RecoveringRuntime:
    def __init__(self):
        self.runs = 0
        self.resumes = 0
        self.resets = 0
        self.resumed_with = None

    async def run(self, _agent, _message, max_steps):
        self.runs += 1
        raise RuntimeError("session died")

    async def resume(self, agent_id, message, max_steps):
        self.resumes += 1
        self.resumed_with = message
        assert agent_id == 42
        return AgentResult(42, "finished", 1, "answered", 0)

    async def recover_session(self, agent_id, _exc):
        self.resets += 1
        assert agent_id == 42
        return 1


@pytest.mark.asyncio
async def test_supervisor_resets_session_and_refetches_orchestrator():
    fleet = object.__new__(OrchestratorFleet)
    fleet._sink = None
    fleet._max_steps = 3
    fleet._max_restarts = 1
    fleet._restart_counts = {}
    runtime = _RecoveringRuntime()

    result = await fleet._drive(runtime, SimpleNamespace(id=42), "start")

    assert (result.content, result.stopped) == ("finished", "answered")
    assert fleet._restart_counts == {42: 1}
    assert (runtime.runs, runtime.resumes, runtime.resets) == (1, 1, 1)
    assert "session died" in runtime.resumed_with
    assert "закрыто: 1." in runtime.resumed_with


@pytest.mark.asyncio
async def test_a_model_outage_waits_and_restarts_instead_of_failing_the_run(monkeypatch):
    from wsignal.inference import fleet as fleet_module
    from wsignal.inference.agent import AgentFailed
    from wsignal.inference.llm import LlmUnavailable

    waits = []

    async def sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(fleet_module.asyncio, "sleep", sleep)

    class Runtime:
        def __init__(self):
            self.calls = 0

        async def run(self, _agent, _message, max_steps=None):
            self.calls += 1
            cause = LlmUnavailable("orchestrator", "m", "fatal", 1, 90.0, "no first token")
            raise AgentFailed("agent 42 (orchestrator): unavailable") from cause

        async def resume(self, _agent_id, _message, max_steps=None):
            self.calls += 1
            return AgentResult(42, "finished", 1, "answered", 0)

        async def recover_session(self, _agent_id, _exc):
            return 0

    fleet = object.__new__(OrchestratorFleet)
    fleet._sink = None
    fleet._max_steps = 3
    fleet._max_restarts = 2
    fleet._restart_counts = {}
    runtime = Runtime()

    result = await fleet._drive(runtime, SimpleNamespace(id=42), "start")

    assert result.stopped == "answered"
    assert waits == [fleet_module.OUTAGE_WAIT_S]
    assert runtime.calls == 2
