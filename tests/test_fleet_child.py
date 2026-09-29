import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

import wsignal.inference.fleet as fleet_module
import wsignal.inference.orchestration as orchestration_module
from wsignal.inference.agent import AgentResult
from wsignal.inference.fleet import OrchestratorFleet
from wsignal.inference.orchestration import Orchestration, interrupt_pending_refutations
from wsignal.inference.pipeline import _run_state
from wsignal.models import Agent, Field, Refutation, Run, Turn


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class _Session:
    def __init__(self, agents=None, scripted=None):
        self.agents = agents or {}
        self.scripted = list(scripted or [])
        self.statements = []
        self.delivered_payloads = []
        self.turns = {}
        self.refutations = {}
        self.fields = {}

    async def get(self, model, identity):
        if model is Run:
            return SimpleNamespace(id=1)
        if model is Turn:
            return self.turns.get(identity)
        if model is Refutation:
            return self.refutations.get(identity)
        if model is Field:
            return self.fields.get(identity)
        return self.agents.get(identity)

    async def scalars(self, statement):
        self.statements.append(statement)
        query = str(statement)
        if "turns.content" in query:
            return _Rows(self.delivered_payloads)
        if "refutations" in query:
            return _Rows([])
        if "fields" in query:
            return _Rows(self.scripted.pop(0) if self.scripted else [])
        if "turns" in query:
            return _Rows(self.turns.get(40, []))
        return _Rows([])

    async def scalar(self, _statement):
        return None

    async def commit(self):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _RestoreSession:
    def __init__(self, agents, fields, reviews):
        self.agents = agents
        self.fields = fields
        self.reviews = reviews
        self.commits = 0

    async def get(self, model, identity):
        if model is Agent:
            return self.agents.get(identity)
        if model is Field:
            return self.fields.get(identity)
        if model is Refutation:
            return self.reviews.get(identity)
        return None

    async def scalars(self, statement):
        query = str(statement)
        if "refutations.state !=" in query:
            return _Rows([])
        if "refutations.state =" in query:
            return _Rows([review for review in self.reviews.values() if review.state == "pending"])
        if "fields.state IN" in query:
            return _Rows(list(self.fields.values()))
        return _Rows([])

    async def scalar(self, _statement):
        return 0

    async def commit(self):
        self.commits += 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _RestoreSessionMaker:
    def __init__(self, session):
        self.session = session

    def __call__(self):
        return self.session


class _Toolbox:
    def __init__(self, session, _fetcher, _cache):
        self.session = session
        self.registered = []

    def register(self, schema, _handler, _roles):
        self.registered.append(schema["function"]["name"])


def _shard(owner, number, field_ids):
    return {
        "orchestrator_agent_id": owner,
        "shard": number,
        "field_ids": field_ids,
        "fields": [
            {"id": field_id, "focus": f"focus {field_id}", "rationale": "why"}
            for field_id in field_ids
        ],
        "ceiling": len(field_ids),
        "brief": "brief",
    }


@pytest.mark.asyncio
async def test_real_child_path_gives_every_shard_its_own_pool_and_the_run_clock(monkeypatch):
    sessions = []
    toolboxes = []
    opening = {}

    def maker():
        session = _Session(
            agents={
                2: SimpleNamespace(id=2, state="running"),
                3: SimpleNamespace(id=3, state="running"),
            }
        )
        sessions.append(session)
        return session

    def toolbox(session, fetcher, cache):
        made = _Toolbox(session, fetcher, cache)
        toolboxes.append(made)
        return made

    class Runtime:
        def __init__(self, session, _llm, toolbox, _max_steps, _sink):
            self.session = session

        async def run(self, agent, message, max_steps=None):
            opening[agent.id] = message
            return AgentResult(agent.id, f"shard {agent.id}", 1, "answered", 0)

    monkeypatch.setattr(fleet_module, "Toolbox", toolbox)
    monkeypatch.setattr(fleet_module, "AgentRuntime", Runtime)

    started = datetime.now(UTC) - timedelta(minutes=7)
    primary_session = _Session()
    primary = Orchestration(
        session=primary_session,
        sessionmaker=maker,
        runtime=None,
        llm=None,
        fetcher=None,
        run=SimpleNamespace(id=1),
        prompts={"orchestrator": "system"},
        budget_minutes=37,
        budget_started_at=started,
        max_research=20,
        owner_agent_id=1,
        initial_agent_id=1,
        partition_worker=True,
    )
    primary._selected_fields = {10}
    fleet = OrchestratorFleet(
        session=primary_session,
        sessionmaker=maker,
        runtime=None,
        orchestration=primary,
        llm=None,
        fetcher=None,
        cache=None,
        run=SimpleNamespace(id=1),
        top_n=15,
        prompts={"orchestrator": "system"},
        max_steps=10,
        max_restarts=1,
        sink=None,
    )

    await fleet._summoned(
        {
            "orchestrators": [
                _shard(1, 1, [10]),
                _shard(2, 2, [11, 12]),
                _shard(3, 3, [13]),
            ]
        }
    )
    results = await asyncio.gather(*fleet._children.values())

    assert [result.content for result in results] == ["shard 2", "shard 3"]
    first = fleet._child_orchestrations[2]
    second = fleet._child_orchestrations[3]
    assert len({id(primary._returns), id(first._returns), id(second._returns)}) == 3
    assert len({id(primary._inflight), id(first._inflight), id(second._inflight)}) == 3
    assert len({id(primary_session), id(first._session), id(second._session)}) == 3
    assert (first._owner_agent_id, second._owner_agent_id) == (2, 3)
    assert (first._selected_fields, second._selected_fields) == ({11, 12}, {13})
    assert (first._research_budget, second._research_budget) == (2, 1)
    assert first._budget.minutes == second._budget.minutes == 37
    assert first._budget.started_at == second._budget.started_at == started
    for made in toolboxes:
        assert "collect" in made.registered
        assert "split_direction" not in made.registered
        assert "summon_orchestrators" not in made.registered
    assert "field_id=11" in opening[2] and "field_id=13" not in opening[2]
    assert "field_id=13" in opening[3]

    first._returns.put_nowait({"kind": "research", "field_id": 11, "analysis": "one"})
    assert (await primary._collect({}, 1))["returns"] == []
    assert (await second._collect({}, 3))["returns"] == []
    assert [item["field_id"] for item in (await first._collect({}, 2))["returns"]] == [11]


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt_exists", [False, True])
async def test_restore_replays_only_returns_without_a_collect_receipt(
    monkeypatch, receipt_exists
):
    field = SimpleNamespace(id=5, state="dispatched", agent_id=40, focus="focus")
    researcher = SimpleNamespace(id=40, state="idle")
    receipt = {
        "kind": "tool_result",
        "name": "collect",
        "payload": {"returns": [{"kind": "research", "field_id": 5, "agent_id": 40}]},
    }
    session = _Session(agents={40: researcher}, scripted=[[field]])
    session.delivered_payloads = [receipt] if receipt_exists else []
    orchestration = Orchestration(
        session=session,
        sessionmaker=None,
        runtime=None,
        llm=None,
        fetcher=None,
        run=SimpleNamespace(id=1),
        prompts={},
    )

    async def final_answer(_agent_id):
        return "stored analysis"

    async def effort(_session, _agent_ids):
        return {}

    orchestration._final_answer = final_answer
    monkeypatch.setattr(orchestration_module, "effort_for", effort)

    restored = await orchestration.restore()

    assert restored["replayed"] == (
        [] if receipt_exists else [{"agent_id": 40, "field_id": 5, "focus": "focus"}]
    )
    assert restored["agents_with_nothing_to_replay"] == []
    if receipt_exists:
        delivered_query = str(session.statements[3].compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        ))
        assert "turns.kind = 'tool_result'" in delivered_query
        assert "(turns.content ->> 'name') = 'collect'" in delivered_query
    assert orchestration._returns.empty() is receipt_exists
    if not receipt_exists:
        replayed = orchestration._returns.get_nowait()
        assert (replayed["analysis"], replayed["stopped"]) == (
            "stored analysis", "answered"
        )


@pytest.mark.asyncio
async def test_restore_continues_refuter_and_mid_rebuttal_once(monkeypatch):
    researcher = SimpleNamespace(id=40, state="running", role="researcher")
    refuter = SimpleNamespace(id=41, state="running", role="refuter", brief="attack")
    field = SimpleNamespace(
        id=5, state="dispatched", agent_id=40, focus="focus", orchestrator_agent_id=1
    )
    review = SimpleNamespace(
        id=7,
        researcher_agent_id=40,
        refuter_agent_id=41,
        rebuttal_required=True,
        state="pending",
        outcome=None,
        collected=False,
        delivery_version=0,
    )
    session = _RestoreSession({40: researcher, 41: refuter}, {5: field}, {7: review})
    sessionmaker = _RestoreSessionMaker(session)
    calls = []

    class Runtime:
        def __init__(self, *_args):
            pass

        async def resume(self, agent_id, _message, **_kwargs):
            calls.append(agent_id)
            agent = session.agents[agent_id]
            agent.state = "idle"
            return AgentResult(agent_id, f"answer {agent_id}", 1, "answered", 0)

    monkeypatch.setattr(orchestration_module, "AgentRuntime", Runtime)

    async def effort(_session, _agent_ids):
        return {"searches_run": 0, "sources_checked": 0}

    monkeypatch.setattr(orchestration_module, "effort_for", effort)
    orchestration = Orchestration(
        session=session,
        sessionmaker=sessionmaker,
        runtime=None,
        llm=None,
        fetcher=None,
        run=SimpleNamespace(id=1),
        prompts={},
    )
    orchestration._owner_agent_id = 1
    orchestration._partition_worker = True
    orchestration._selected_fields = {5}

    restored = await orchestration.restore()

    assert calls == [41, 40]
    assert restored["continued_agents"] == [40, 41]
    assert restored["failed_agents"] == []
    assert orchestration._returns.qsize() == 1
    outcome = orchestration._returns.get_nowait()
    assert outcome["refutation_id"] == 7
    assert outcome["attack"] == "answer 41"
    assert outcome["rebuttal"] == "answer 40"


@pytest.mark.asyncio
async def test_restore_reports_an_agent_whose_history_cannot_continue(monkeypatch):
    researcher = SimpleNamespace(id=40, state="running", role="researcher", brief="")
    field = SimpleNamespace(
        id=5, state="dispatched", agent_id=40, focus="focus", orchestrator_agent_id=1
    )
    session = _RestoreSession({40: researcher}, {5: field}, {})

    class Runtime:
        def __init__(self, *_args):
            pass

        async def resume(self, *_args, **_kwargs):
            raise ValueError("unusable history")

    monkeypatch.setattr(orchestration_module, "AgentRuntime", Runtime)
    orchestration = Orchestration(
        session=session,
        sessionmaker=_RestoreSessionMaker(session),
        runtime=None,
        llm=None,
        fetcher=None,
        run=SimpleNamespace(id=1),
        prompts={},
    )
    orchestration._owner_agent_id = 1
    orchestration._partition_worker = True
    orchestration._selected_fields = {5}

    restored = await orchestration.restore()

    assert researcher.state == "failed"
    assert restored["continued_agents"] == []
    assert restored["failed_agents"] == [40]
    returned = orchestration._returns.get_nowait()
    assert (returned["kind"], returned["agent_id"], returned["stopped"]) == (
        "research", 40, "failed"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("rebuttal_required", [False, True])
async def test_interrupting_refutation_fails_agents_that_will_not_be_driven(
    rebuttal_required,
):
    researcher = SimpleNamespace(id=40, state="running")
    refuter = SimpleNamespace(id=41, state="running")
    review = SimpleNamespace(
        id=7,
        researcher_agent_id=40,
        refuter_agent_id=41,
        rebuttal_required=rebuttal_required,
        delivery_version=0,
        collected=False,
        outcome=None,
        completed_at=None,
    )
    session = _RestoreSession({40: researcher, 41: refuter}, {}, {7: review})

    async def scalar(_statement):
        return (
            {"content": "Критик пытался доказать обратное"}
            if rebuttal_required
            else None
        )

    session.scalar = scalar
    await interrupt_pending_refutations(session, [review], "process died")

    assert review.state == "interrupted"
    assert refuter.state == "failed"
    assert researcher.state == ("failed" if rebuttal_required else "running")
    assert review.outcome["stopped"] == "interrupted"
    assert review.outcome["incomplete"] == "process died"


@pytest.mark.parametrize(
    ("summary", "stopped", "expected"),
    [
        ("summary", "answered", "finished"),
        ("summary", "max_steps", "exhausted"),
        (None, "max_steps", "exhausted"),
        (None, "answered", "exhausted"),
        ("summary", "supervisor_gave_up", "failed"),
        (None, "supervisor_gave_up", "failed"),
    ],
)
def test_run_state_reflects_the_whole_fleet(summary, stopped, expected):
    assert _run_state(summary, stopped) == expected
