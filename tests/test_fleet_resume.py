import asyncio
from types import SimpleNamespace

import pytest

from wsignal.inference.agent import AgentResult
from wsignal.inference.fleet import OrchestratorFleet


class _Session:
    def __init__(self, responses=None, agents=None):
        self.responses = list(responses or [])
        self.agents = agents or {}

    async def get(self, _model, identity):
        return self.agents.get(identity)

    async def scalars(self, _statement):
        values = self.responses.pop(0) if self.responses else []
        return SimpleNamespace(all=lambda: values)

    async def scalar(self, _statement):
        return None

    async def commit(self):
        return None


class _Orchestration:
    def __init__(self):
        self._summon_callback = None
        self._inflight = 0
        self._partition_worker = False
        self._owner_agent_id = 1
        self._initial_agent_id = 1
        self._researched = set()
        self._selected_fields = set()
        self._research_budget = 10
        self._max_research = 10
        self.restore_calls = 0

    async def restore(self):
        self.restore_calls += 1
        return {"returns_replayed": 1}

    async def _final_answer(self, _agent_id):
        return "completed transcript"

    async def _read_entries(self, _arguments, _agent_id):
        return {"entries": []}

    async def _summoned(self, _manifest):
        return None


@pytest.mark.asyncio
async def test_historical_restore_keeps_legacy_idle_top_level_orchestrator():
    orchestration = _Orchestration()
    fleet = OrchestratorFleet(
        session=_Session(),
        sessionmaker=None,
        runtime=None,
        orchestration=orchestration,
        llm=None,
        fetcher=None,
        cache=None,
        run=SimpleNamespace(id=1),
        top_n=15,
        prompts={},
        max_steps=10,
        max_restarts=1,
        sink=None,
    )
    primary = SimpleNamespace(id=1, state="running")

    result = await fleet.restore(primary)

    assert result["returns_replayed"] == 1
    assert fleet._primary_completed is False
    assert fleet._children_started.is_set()
    assert fleet._children == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("operator_message", [False, True])
async def test_restore_skips_completed_primary_and_child(monkeypatch, operator_message):
    primary_fields = [SimpleNamespace(id=10, state="pending", focus="primary", rationale="why")]
    child_fields = [SimpleNamespace(id=11, state="pending", focus="child", rationale="why")]
    primary = SimpleNamespace(id=1, state="done")
    child = SimpleNamespace(id=2, state="done", brief="brief")
    session = _Session(
        [[1, 2], primary_fields, [child], child_fields],
        agents={1: primary},
    )
    orchestration = _Orchestration()
    fleet = OrchestratorFleet(
        session=session,
        sessionmaker=None,
        runtime=None,
        orchestration=orchestration,
        llm=None,
        fetcher=None,
        cache=None,
        run=SimpleNamespace(id=1),
        top_n=15,
        prompts={},
        max_steps=10,
        max_restarts=1,
        sink=None,
    )

    async def pending(_session, _run_id):
        return operator_message

    monkeypatch.setattr("wsignal.inference.fleet.has_pending_messages", pending)
    await fleet.restore(primary)
    if operator_message:
        assert fleet._primary_completed is False
        assert orchestration.restore_calls == 1
        assert fleet._children == {}
        return
    await fleet.start(primary, "do not call completed work")
    result = await fleet.wait()

    assert fleet._primary_completed is True
    assert fleet._primary is None
    assert fleet._children == {}
    assert orchestration.restore_calls == 0
    assert result[0] == "completed transcript"
    assert result[2] == "answered"


@pytest.mark.asyncio
async def test_failure_and_restart_stay_with_one_shard():
    fleet = OrchestratorFleet(
        session=_Session(),
        sessionmaker=None,
        runtime=None,
        orchestration=_Orchestration(),
        llm=None,
        fetcher=None,
        cache=None,
        run=SimpleNamespace(id=1),
        top_n=15,
        prompts={},
        max_steps=10,
        max_restarts=1,
        sink=None,
    )
    failed_shard = SimpleNamespace(id=2)
    other_shard = SimpleNamespace(id=3)

    class Runtime:
        def __init__(self, fail_once):
            self.fail_once = fail_once
            self.runs = 0
            self.resumes = 0
            self.recoveries = 0

        async def run(self, *_args, **_kwargs):
            self.runs += 1
            if self.fail_once:
                self.fail_once = False
                raise RuntimeError("shard-local failure")
            return AgentResult(3, "other", 1, "answered", 0)

        async def resume(self, agent_id, *_args, **_kwargs):
            self.resumes += 1
            return AgentResult(agent_id, "recovered", 1, "answered", 0)

        async def recover_session(self, _agent_id, _exc):
            self.recoveries += 1

    failed_runtime = Runtime(True)
    other_runtime = Runtime(False)
    runtimes = {2: failed_runtime, 3: other_runtime}

    async def run_child(shard):
        owner = shard["orchestrator_agent_id"]
        agent = failed_shard if owner == 2 else other_shard
        return await fleet._drive(runtimes[owner], agent, "start")

    fleet._run_child = run_child
    await fleet._summoned(
        {
            "orchestrators": [
                {"orchestrator_agent_id": 1},
                {"orchestrator_agent_id": 2},
                {"orchestrator_agent_id": 3},
            ]
        }
    )
    results = await asyncio.gather(*fleet._children.values())

    assert [result.content for result in results] == ["recovered", "other"]
    assert (failed_runtime.runs, failed_runtime.resumes, failed_runtime.recoveries) == (1, 1, 1)
    assert (other_runtime.runs, other_runtime.resumes, other_runtime.recoveries) == (1, 0, 0)
    assert fleet._restart_counts == {2: 1, 3: 0}


def _fleet(session, orchestration=None):
    return OrchestratorFleet(
        session=session,
        sessionmaker=None,
        runtime=None,
        orchestration=orchestration or _Orchestration(),
        llm=None,
        fetcher=None,
        cache=None,
        run=SimpleNamespace(id=1),
        top_n=15,
        prompts={},
        max_steps=10,
        max_restarts=1,
        sink=None,
    )


@pytest.mark.asyncio
async def test_a_shard_that_gave_up_is_recorded_failed_not_done():
    primary = SimpleNamespace(id=1, state="running")
    fleet = _fleet(_Session(agents={1: primary}))

    async def gave_up(*_args, **_kwargs):
        return AgentResult(1, None, 0, "supervisor_gave_up", 0)

    fleet._drive = gave_up
    result = await fleet._run_primary(primary, "start")

    assert result.stopped == "supervisor_gave_up"
    assert primary.state == "failed"


@pytest.mark.asyncio
async def test_restore_retries_failed_shards():
    primary_fields = [SimpleNamespace(id=10, state="dispatched", focus="p", rationale="r")]
    child_fields = [SimpleNamespace(id=11, state="dispatched", focus="c", rationale="r")]
    primary = SimpleNamespace(id=1, state="failed")
    child = SimpleNamespace(id=2, state="failed", brief="brief")
    orchestration = _Orchestration()
    fleet = _fleet(
        _Session([[1, 2], primary_fields, [child], child_fields], agents={1: primary}),
        orchestration,
    )
    started = []

    async def run_child(shard):
        started.append((shard["orchestrator_agent_id"], shard["resume"]))
        return AgentResult(2, "done", 1, "answered", 0)

    fleet._run_child = run_child
    await fleet.restore(primary)
    await asyncio.gather(*fleet._children.values())

    assert fleet._primary_completed is False
    assert orchestration.restore_calls == 1
    assert started == [(2, True)]


async def _finished(content, stopped):
    return AgentResult(1, content, 1, stopped, 0)


@pytest.mark.parametrize(
    ("child_stopped", "owned_unfinished", "expected"),
    [
        ("answered", [], "answered"),
        ("answered", ["dispatched"], "max_steps"),
        ("max_steps", [], "max_steps"),
        ("supervisor_gave_up", [], "supervisor_gave_up"),
    ],
)
@pytest.mark.asyncio
async def test_wait_reports_the_whole_fleet(child_stopped, owned_unfinished, expected):
    fleet = _fleet(_Session([owned_unfinished]))

    async def run_child(shard):
        return AgentResult(shard["orchestrator_agent_id"], None, 1, child_stopped, 0)

    fleet._run_child = run_child
    fleet._primary_agent = SimpleNamespace(id=1)
    fleet._primary = asyncio.create_task(_finished("summary", "answered"))
    await fleet._summoned(
        {"orchestrators": [{"orchestrator_agent_id": 1}, {"orchestrator_agent_id": 2}]}
    )

    content, _restarts, stopped = await fleet.wait()

    assert content == "summary"
    assert stopped == expected


@pytest.mark.asyncio
async def test_wait_reports_a_child_that_raised_as_a_failure():
    fleet = _fleet(_Session([[]]))

    async def run_child(_shard):
        raise RuntimeError("child crashed")

    fleet._run_child = run_child
    fleet._primary_agent = SimpleNamespace(id=1)
    fleet._primary = asyncio.create_task(_finished("summary", "answered"))
    await fleet._summoned(
        {"orchestrators": [{"orchestrator_agent_id": 1}, {"orchestrator_agent_id": 2}]}
    )

    assert (await fleet.wait())[2] == "supervisor_gave_up"
