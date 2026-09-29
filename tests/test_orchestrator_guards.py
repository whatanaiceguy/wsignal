import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import sqlite

from wsignal.inference.agent import AgentRuntime
from wsignal.inference.fleet import OrchestratorFleet
from wsignal.inference.orchestration import ORCHESTRATOR_TOOLS, Orchestration
from wsignal.inference.pipeline import _run_state


async def _async_noop():
    return None


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class _Session:
    def __init__(self):
        self.turns = []
        self.agent = SimpleNamespace(id=4, role="orchestrator", state="running")
        self.run = SimpleNamespace(id=9, state="running", finished_at=None)
        self.entries = ["already written"]
        self.commits = 0

    async def scalars(self, statement):
        if "turns.kind = :kind_1" in str(statement):
            return _Rows([turn for turn in self.turns if turn.kind == "assistant"])
        return _Rows(self.turns)

    async def execute(self, _statement):
        return _Rows([])

    async def scalar(self, statement):
        if "max(" in str(statement).lower():
            return max((turn.seq for turn in self.turns), default=0) + 1
        return max(self.turns, key=lambda turn: turn.seq, default=None)

    def add(self, turn):
        turn.seq = max((item.seq for item in self.turns), default=0) + 1
        self.turns.append(turn)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        pass

    async def get(self, _model, _identity):
        return self.agent


class _Toolbox:
    def schemas_for(self, _role):
        return []

    async def call(self, *_args):
        return {"ok": True}


class _BudgetLlm:
    def __init__(self):
        self.calls = 0

    def model_for(self, _role):
        return "test-model"

    async def converse(self, *_args, **_kwargs):
        self.calls += 1
        call = SimpleNamespace(id=f"c{self.calls}", name=f"move-{self.calls}", arguments={})
        return SimpleNamespace(
            message={"role": "assistant", "content": None, "tool_calls": []},
            content=None,
            duration_ms=0,
            cost_usd=None,
            prompt_tokens=0,
            cached_tokens=0,
            tool_calls=[call],
            wants_tools=True,
            accounting=lambda: {},
        )


@pytest.mark.asyncio
async def test_orchestrator_budget_exhaustion_stops_with_persisted_reason():
    session = _Session()
    llm = _BudgetLlm()
    runtime = AgentRuntime(session, llm, _Toolbox(), max_steps=2)

    result = await runtime.run(session.agent, "start", partial_on_failure=True)

    assert llm.calls == 2
    assert result.stopped == "call_budget_exhausted"
    assert "300 calls" not in result.content
    failure = [
        turn.content for turn in session.turns
        if turn.kind == "assistant" and turn.content.get("reason")
    ]
    assert failure[-1]["reason"] == "orchestrator model-call budget exhausted (2 calls)"
    assert result.content == failure[-1]["reason"]
    assert session.agent.state == "failed"


@pytest.mark.asyncio
async def test_status_adds_calls_remaining_to_each_tool_result(monkeypatch):
    orchestration = object.__new__(Orchestration)
    orchestration._partition_worker = False
    orchestration._owner_agent_id = 4
    remaining = iter([300, 299])
    payloads = []

    async def status():
        return {"calls_remaining": next(remaining)}

    async def handler(_arguments, _agent_id):
        return {"ok": True}

    orchestration.status = status
    wrapped = orchestration._with_status(handler)
    payloads.append(await wrapped({}, 4))
    payloads.append(await wrapped({}, 4))

    assert [payload["_status"]["calls_remaining"] for payload in payloads] == [300, 299]


@pytest.mark.asyncio
async def test_end_run_records_reason_stops_and_preserves_written_entries():
    session = _Session()
    events = []
    stopped = []
    orchestration = object.__new__(Orchestration)
    orchestration._initial_agent_id = 4
    orchestration._child_shard = False
    orchestration._end_run_reason = None
    orchestration._run = session.run
    orchestration._session = session
    orchestration._sink = events.append

    async def stop_all(reason):
        stopped.append(reason)

    orchestration._end_run_callback = stop_all
    result = await orchestration._end_run({"reason": "All sources are failing"}, 4)

    assert result == {
        "run_ended": True,
        "reason": "All sources are failing",
        "state": "failed",
    }
    assert session.run.state == "failed"
    assert session.run.finished_at is not None
    assert stopped == ["All sources are failing"]
    assert events[-1] == {
        "kind": "run_ended",
        "run_id": 9,
        "reason": "All sources are failing",
        "state": "failed",
    }
    assert session.entries == ["already written"]


@pytest.mark.asyncio
async def test_end_run_can_finish_rather_than_fail():
    session = _Session()
    orchestration = object.__new__(Orchestration)
    orchestration._initial_agent_id = 4
    orchestration._child_shard = False
    orchestration._end_run_reason = None
    orchestration._run = session.run
    orchestration._session = session
    orchestration._sink = None
    orchestration._end_run_callback = None

    result = await orchestration._end_run({"reason": "done", "state": "finished"}, 4)

    assert result["state"] == "finished"
    assert session.run.state == "finished"


def test_error_groups_split_environment_inputs_and_limits():
    from wsignal.inference.orchestration import _error_group

    assert _error_group("fetch", "ConnectTimeout: timed out") == "environment"
    assert _error_group("corpus", "the research corpus did not answer") == "environment"
    assert _error_group("write_entry", "field_id 1 is not a field in this run") == "input"
    assert _error_group("search", "unknown adapter 'x'; pick one") == "input"
    assert _error_group("search", "web-call budget is spent; no request was made") == "limits"


class _ErrorRows:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


@pytest.mark.asyncio
async def test_error_warning_fires_once_per_threshold_and_then_stops():
    rows = []

    class Session:
        async def execute(self, _statement):
            return _ErrorRows(rows)

    orchestration = object.__new__(Orchestration)
    orchestration._child_shard = False
    orchestration._session = Session()
    orchestration._run = type("Run", (), {"id": 9})()

    rows.extend([("fetch", "timed out")] * 29)
    assert await orchestration._error_warning() is None
    rows.append(("write_entry", "state 'x' is not one of banked"))
    first = await orchestration._error_warning()
    assert first.startswith("30 tool errors")
    assert "Environment 29" in first and "Inputs 1" in first
    assert await orchestration._error_warning() is None
    rows.extend([("fetch", "timed out")] * 100)
    assert "130 tool errors" in await orchestration._error_warning()
    rows.append(("fetch", "timed out"))
    assert await orchestration._error_warning() is None


def test_end_run_is_required_and_registered_only_for_root_orchestrator():
    schema = next(
        schema["function"] for schema in ORCHESTRATOR_TOOLS
        if schema["function"]["name"] == "end_run"
    )
    assert schema["parameters"]["required"] == ["reason", "state"]
    assert "route around them" in schema["description"]

    class Toolbox:
        def __init__(self):
            self.registered = []

        def register(self, schema, _handler, _roles):
            self.registered.append(schema["function"]["name"])

    root = Orchestration(
        session=None,
        sessionmaker=None,
        runtime=None,
        llm=None,
        fetcher=None,
        run=SimpleNamespace(id=1, seeded=False),
        prompts={"orchestrator": "prompt"},
    )
    child = Orchestration(
        session=None,
        sessionmaker=None,
        runtime=None,
        llm=None,
        fetcher=None,
        run=SimpleNamespace(id=1, seeded=False),
        prompts={"orchestrator": "prompt"},
        child_shard=True,
    )
    root_tools, child_tools = Toolbox(), Toolbox()
    root.install(root_tools)
    child.install(child_tools)
    assert "end_run" in root_tools.registered
    assert "end_run" not in child_tools.registered


@pytest.mark.asyncio
async def test_end_run_cancels_the_fleet_agents():
    fleet = object.__new__(OrchestratorFleet)
    fleet._end_run_reason = None
    fleet._stop_reason = None
    fleet._children = {}
    fleet._child_orchestrations = {}
    fleet._orchestration = SimpleNamespace(shutdown=_async_noop)
    fleet._primary = None

    async def wait_forever():
        await asyncio.Event().wait()

    task = asyncio.create_task(wait_forever())
    fleet._children[11] = task
    await fleet._end_run("broken")

    assert task.cancelled()
    assert fleet._end_run_reason == "broken"
    assert fleet._stop_reason == "broken"


def test_orchestration_uses_the_run_budget_for_status():
    orchestration = Orchestration(
        session=None,
        sessionmaker=None,
        runtime=None,
        llm=None,
        fetcher=None,
        run=SimpleNamespace(id=1, seeded=False, orchestrator_max_steps=200),
        prompts={"orchestrator": "prompt"},
    )

    assert orchestration._max_model_calls == 200


def test_call_budget_exhaustion_maps_to_existing_terminal_run_state():
    assert _run_state(None, "call_budget_exhausted") == "exhausted"
    assert _run_state(None, "run_ended") == "failed"


@pytest.mark.asyncio
async def test_end_run_rejects_child_and_non_owner():
    orchestration = object.__new__(Orchestration)
    orchestration._initial_agent_id = 4
    orchestration._child_shard = True
    orchestration._end_run_reason = None

    result = await orchestration._end_run({"reason": "broken"}, 5)

    assert result == {"error": "only the top-level orchestrator may end the run"}
    assert orchestration._end_run_reason is None


@pytest.mark.asyncio
async def test_budget_loop_does_not_issue_a_301st_model_call():
    session = _Session()
    llm = _BudgetLlm()
    runtime = AgentRuntime(session, llm, _Toolbox(), max_steps=300)
    result = await runtime.run(session.agent, "start", partial_on_failure=True)

    assert llm.calls == 300
    assert result.stopped == "call_budget_exhausted"
    assert session.agent.state == "failed"


class _IdleSession(_Session):
    def __init__(self):
        super().__init__()
        self.db = sqlite3.connect(":memory:")
        self.db.execute(
            "CREATE TABLE turns (agent_id INTEGER, seq INTEGER, kind TEXT, content JSON)"
        )

    def add(self, turn):
        super().add(turn)
        self.db.execute(
            "INSERT INTO turns VALUES (?, ?, ?, ?)",
            (turn.agent_id, turn.seq, turn.kind, json.dumps(turn.content)),
        )

    async def scalar(self, statement):
        if str(statement).startswith("SELECT turns.content"):
            sql = str(statement.compile(
                dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True}
            ))
            row = self.db.execute(sql).fetchone()
            return json.loads(row[0]) if row else None
        return await super().scalar(statement)

    async def scalars(self, statement):
        if "fields." in str(statement):
            return _Rows([SimpleNamespace(id=17)])
        return await super().scalars(statement)


def _idle_orchestration(session, closed=False):
    orchestration = Orchestration(
        session=session, sessionmaker=None, runtime=None, llm=None, fetcher=None,
        run=session.run, prompts={}, owner_agent_id=4, child_shard=True,
        partition_worker=True,
    )
    if closed:
        orchestration._time_dispatch_error = lambda: "research closed"
    return orchestration


@pytest.mark.asyncio
async def test_second_empty_collect_dispatches_and_other_tool_resets():
    from unittest.mock import AsyncMock

    from wsignal.models import Turn

    session = _IdleSession()
    orchestration = _idle_orchestration(session)
    orchestration._dispatch_researchers = AsyncMock(return_value={"dispatched": [17]})
    first = await orchestration._collect({}, 4)
    assert first["error"]
    orchestration._dispatch_researchers.assert_not_awaited()
    session.add(Turn(agent_id=4, kind="tool_result", content={
        "name": "collect", "payload": first,
    }))
    session.add(Turn(agent_id=99, kind="tool_result", content={
        "name": "read_fields", "payload": {},
    }))
    session.add(Turn(agent_id=4, kind="tool_call", content={
        "name": "collect", "arguments": {},
    }))
    resumed = _idle_orchestration(session)
    resumed._dispatch_researchers = orchestration._dispatch_researchers
    second = await resumed._collect({}, 4)
    assert second["dispatch"] == {"dispatched": [17]}
    resumed._dispatch_researchers.assert_awaited_once_with(
        {"field_ids": [17], "brief": ""}, 4,
    )
    for kind in ("tool_call", "tool_result"):
        session.add(Turn(agent_id=4, kind=kind, content={
            "name": "read_fields", "payload": {},
        }))
        reset = await resumed._collect({}, 4)
        assert reset["error"]
    assert resumed._dispatch_researchers.await_count == 1
    session.db.close()


@pytest.mark.asyncio
async def test_closed_research_second_empty_collect_ends_agent():
    session = _IdleSession()
    orchestration = _idle_orchestration(session, closed=True)
    payloads = []

    class Toolbox(_Toolbox):
        async def call(self, _name, arguments, agent_id):
            payload = await orchestration._collect(arguments, agent_id)
            payloads.append(payload)
            return payload

    class Llm(_BudgetLlm):
        async def converse(self, *args, **kwargs):
            reply = await super().converse(*args, **kwargs)
            reply.tool_calls[0].name = "collect"
            return reply

    llm = Llm()
    events = []
    result = await AgentRuntime(session, llm, Toolbox(), max_steps=5, sink=events.append).run(
        session.agent, "start",
    )
    assert llm.calls == 2
    assert "orchestrator_finished" not in payloads[0]
    assert "collect" not in payloads[0]["next_action"].lower()
    assert payloads[1]["orchestrator_finished"] is True
    assert session.agent.state == "done"
    assert session.agent.returned_at is not None
    assert result.stopped == "answered"
    assert _run_state(result.content, result.stopped) == "finished"
    assert events[-1]["kind"] == "orchestrator_stopped"
    session.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("resumed", [False, True])
async def test_finished_orchestrator_fleet_finishes_with_pending_fields(resumed):
    from wsignal.inference.agent import AgentResult

    session = _Session()
    session.agent.state = "done"
    db = sqlite3.connect(":memory:")
    db.executescript(
        "CREATE TABLE fields (run_id INTEGER, orchestrator_agent_id INTEGER, state TEXT);"
        "CREATE TABLE agents (id INTEGER, state TEXT);"
        "INSERT INTO agents VALUES (4, 'done'), (5, 'running');"
        "INSERT INTO fields VALUES (9, 4, 'pending');"
    )

    async def scalars(statement):
        sql = str(statement.compile(
            dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True}
        ))
        return _Rows([row[0] for row in db.execute(sql).fetchall()])

    async def final_answer(_agent_id):
        return None

    async def primary():
        return AgentResult(4, "Finished idle shard.", 2, "answered", 2)

    session.scalars = scalars
    fleet = object.__new__(OrchestratorFleet)
    fleet._session = session
    fleet._run = session.run
    fleet._primary_agent = session.agent
    fleet._primary = None if resumed else asyncio.create_task(primary())
    fleet._orchestration = SimpleNamespace(_final_answer=final_answer)
    fleet._children_started = asyncio.Event()
    fleet._children_started.set()
    fleet._children = {}
    fleet._restart_counts = {}
    fleet._end_run_reason = None
    summary, _, stopped = await fleet._wait()
    assert _run_state(summary, stopped) == "finished"
    db.execute("INSERT INTO fields VALUES (9, 4, 'dispatched')")
    assert await fleet._unfinished_owned_states() == ["dispatched"]
    db.execute("INSERT INTO fields VALUES (9, 5, 'pending')")
    assert await fleet._unfinished_owned_states() == ["dispatched", "pending"]
    db.close()
