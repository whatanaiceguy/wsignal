import asyncio
from datetime import UTC, datetime

import pytest

from wsignal.inference.pipeline import _heartbeat
from wsignal.interface import dev_api
from wsignal.models import Agent, Turn


class _Rows:
    def __init__(self, values=()):
        self.values = values

    def all(self):
        return self.values


class _ScalarRows:
    def __init__(self, values):
        self.values = values

    def all(self):
        return self.values


class _Session:
    def __init__(self, agent, turns, run_liveness=()):
        self.agent = agent
        self.turns = turns
        self.run_liveness = run_liveness
        self.executions = 0

    async def get(self, _model, _identity):
        return self.agent

    async def scalars(self, _statement):
        return _ScalarRows(self.turns)

    async def execute(self, _statement):
        self.executions += 1
        return _Rows(self.run_liveness if self.executions == 2 else ())


class _HeartbeatSession:
    def __init__(self, state):
        self.state = state

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def execute(self, statement):
        self.state["statement"] = statement
        self.state["ticks"] = self.state.get("ticks", 0) + 1

    async def commit(self):
        self.state["committed"] = True


@pytest.mark.parametrize(
    ("state", "heartbeat_at", "expected"),
    [
        ("running", datetime(2026, 1, 1, tzinfo=UTC), True),
        ("running", datetime(2025, 12, 31, 23, 58, tzinfo=UTC), False),
        ("running", None, False),
        ("finished", datetime(2026, 1, 1, tzinfo=UTC), False),
    ],
)
def test_liveness_requires_running_state_and_fresh_heartbeat(state, heartbeat_at, expected):
    now = datetime(2026, 1, 1, tzinfo=UTC)

    assert dev_api._is_live(state, heartbeat_at, now) is expected


@pytest.mark.asyncio
async def test_heartbeat_stamps_run_row_while_driven():
    state = {}

    def sessionmaker():
        return _HeartbeatSession(state)

    fleet = type("Fleet", (), {"inflight": 1})()
    task = asyncio.create_task(_heartbeat(fleet, None, 0.01, sessionmaker, 7))
    try:
        for _ in range(20):
            if state.get("ticks", 0) >= 2:
                break
            await asyncio.sleep(0.005)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert state["committed"] is True
    assert state["ticks"] >= 2
    assert "heartbeat_at" in str(state["statement"])
    assert "runs" in str(state["statement"])


@pytest.mark.asyncio
async def test_agent_turn_transcript_includes_turn_created_at():
    created_at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    agent = Agent(
        id=3,
        run_id=7,
        role="researcher",
        model="model",
        state="running",
        created_at=created_at,
    )
    turn = Turn(
        id=9,
        agent_id=3,
        seq=1,
        kind="assistant",
        content={"message": {"content": "done"}},
        created_at=created_at,
    )

    heartbeat_at = datetime.now(UTC)
    payload = await dev_api.agent_turns(
        3, _Session(agent, [turn], [(7, "running", heartbeat_at)])
    )

    assert payload.agent.is_live is True
    assert payload.turns[0].created_at == created_at
    assert payload.turns[0].model_dump(mode="json")["created_at"] == created_at.isoformat().replace(
        "+00:00", "Z"
    )


@pytest.mark.asyncio
async def test_heartbeat_row_ticks_faster_than_the_heartbeat_event():
    state = {}
    events = []

    def sessionmaker():
        return _HeartbeatSession(state)

    fleet = type("Fleet", (), {"inflight": 1})()
    task = asyncio.create_task(
        _heartbeat(fleet, events.append, 10.0, sessionmaker, 7, write_s=0.01)
    )
    try:
        for _ in range(40):
            if state.get("ticks", 0) >= 4:
                break
            await asyncio.sleep(0.005)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert state["ticks"] >= 4
    assert [event["kind"] for event in events] == ["heartbeat"]
