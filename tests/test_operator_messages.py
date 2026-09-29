from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import JSON, BigInteger, Column, Integer, MetaData, Table, create_engine, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from wsignal.inference.agent import AgentRuntime
from wsignal.inference.operator_messages import (
    deliver_messages,
    has_pending_messages,
    queue_message,
)
from wsignal.interface import dev_api, jobs
from wsignal.models import RunEvent, Turn


class Database:
    def __init__(self):
        self.engine = create_engine("sqlite://")
        metadata = MetaData()
        for model in (RunEvent, Turn):
            Table(model.__tablename__, metadata, *[
                Column(
                    column.name,
                    Integer() if isinstance(column.type, BigInteger)
                    else JSON() if isinstance(column.type, JSONB) else column.type,
                    primary_key=column.primary_key,
                    nullable=column.nullable,
                    server_default=column.server_default
                    if not column.primary_key else None,
                )
                for column in model.__table__.columns
            ])
        metadata.create_all(self.engine)
        self.session = Session(self.engine, expire_on_commit=False)
        self.agent = SimpleNamespace(id=4, run_id=9, role="orchestrator", state="running")
        self.run = SimpleNamespace(
            id=9, state="running", heartbeat_at=datetime.now(UTC),
        )

    def add(self, value):
        self.session.add(value)

    async def scalar(self, statement):
        return self.session.scalar(statement)

    async def scalars(self, statement):
        return self.session.scalars(statement)

    async def commit(self):
        self.session.commit()

    async def rollback(self):
        self.session.rollback()

    async def get(self, model, identity):
        return self.agent if model.__name__ == "Agent" else self.run

    def close(self):
        self.session.close()
        self.engine.dispose()


@pytest.fixture
def database():
    db = Database()
    yield db
    db.close()


class Toolbox:
    def __init__(self, database):
        self.database = database

    def schemas_for(self, role):
        return []

    async def call(self, name, arguments, agent_id):
        if name == "search":
            queue_message(self.database, 9, "check the evidence")
        await self.database.commit()
        return {"ok": True}


class Llm:
    def __init__(self, tools=False):
        self.messages = []
        self.tools = tools

    def model_for(self, role):
        return "test"

    async def converse(self, role, messages, schemas, **kwargs):
        self.messages.append(messages)
        calls = []
        message = {"role": "assistant", "content": "done"}
        if self.tools and len(self.messages) == 1:
            calls = [
                SimpleNamespace(id="call", name="search", arguments={}),
                SimpleNamespace(id="second", name="fetch", arguments={}),
            ]
            message = {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "call", "type": "function",
                    "function": {"name": "search", "arguments": "{}"},
                }, {
                    "id": "second", "type": "function",
                    "function": {"name": "fetch", "arguments": "{}"},
                }],
            }
        return SimpleNamespace(
            message=message, content=message["content"], tool_calls=calls,
            wants_tools=bool(calls), duration_ms=0, cost_usd=None,
            prompt_tokens=0, cached_tokens=0, accounting=lambda: {},
        )


def runtime(database, llm):
    agent = AgentRuntime(database, llm, Toolbox(database))
    agent.operator_agent_id = 4
    return agent


@pytest.mark.asyncio
async def test_live_message_arrives_after_tool_results_once(database):
    llm = Llm(tools=True)
    agent = runtime(database, llm)
    await agent.run(database.agent, "start")
    assert not any("operator" in str(message) for message in llm.messages[0])
    assert [message["tool_call_id"] for message in llm.messages[1][-3:-1]] == [
        "call", "second",
    ]
    assert llm.messages[1][-1] == {
        "role": "user", "content": "Message from the operator: check the evidence",
    }
    await deliver_messages(database, 9, 4)
    turns = database.session.scalars(select(Turn).where(Turn.kind == "user")).all()
    assert [turn.content["content"] for turn in turns] == [
        "start", "Message from the operator: check the evidence",
    ]
    receipts = database.session.scalars(
        select(RunEvent).where(RunEvent.kind == "operator_message_delivered")
    ).all()
    assert len(receipts) == 1
    database.session.close()
    database.session = Session(database.engine, expire_on_commit=False)
    await deliver_messages(database, 9, 4)
    turns = database.session.scalars(select(Turn).where(Turn.kind == "user")).all()
    assert [turn.content["content"] for turn in turns] == [
        "start", "Message from the operator: check the evidence",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["orchestrator", "researcher"])
async def test_children_do_not_consume_messages(database, role):
    queue_message(database, 9, "main only")
    await database.commit()
    child = SimpleNamespace(id=5, run_id=9, role=role, state="running", parent_agent_id=4)
    llm = Llm()
    await runtime(database, llm).run(child, "child work")
    assert not any("operator" in str(message) for message in llm.messages[0])
    assert len(database.session.scalars(select(RunEvent)).all()) == 1
    await runtime(database, llm).run(database.agent, "main work")
    assert llm.messages[-1][-1]["content"] == "Message from the operator: main only"


@pytest.mark.asyncio
async def test_resume_message_follows_repaired_tool_results(database):
    agent = runtime(database, Llm())
    await agent._append(4, "assistant", {"message": {
        "role": "assistant", "content": None,
        "tool_calls": [{
            "id": "interrupted", "type": "function",
            "function": {"name": "search", "arguments": "{}"},
        }],
    }})
    queue_message(database, 9, "new direction")
    await database.commit()
    await agent.resume(4, "continue")
    messages = agent._llm.messages[0]
    assert [message["role"] for message in messages] == [
        "assistant", "tool", "system", "user", "user",
    ]
    assert messages[-1]["content"] == "Message from the operator: new direction"


@pytest.mark.asyncio
async def test_queue_keeps_run_isolation_and_order(database):
    queue_message(database, 10, "other run")
    queue_message(database, 9, "first")
    queue_message(database, 9, "second")
    await database.commit()
    assert await has_pending_messages(database, 9)
    await deliver_messages(database, 9, 4)
    assert not await has_pending_messages(database, 9)
    assert await has_pending_messages(database, 10)
    turns = database.session.scalars(select(Turn).order_by(Turn.seq)).all()
    assert [turn.content["content"] for turn in turns] == [
        "Message from the operator: first", "Message from the operator: second",
    ]
    assert len(database.session.scalars(select(RunEvent)).all()) == 5


@pytest.mark.asyncio
async def test_live_endpoint_persists_message(database):
    result = await dev_api.send_message(9, dev_api.MessageRequest(text=" hello "), database)
    event = database.session.get(RunEvent, result["message_id"])
    assert result["run_id"] == 9
    assert event.kind == "operator_message"
    assert event.payload == {"text": "hello"}


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["finished", "failed", "exhausted", "stopped"])
async def test_dev_resume_accepts_all_inactive_states(database, monkeypatch, state):
    database.run.state = state
    calls = []

    async def start(query, **kwargs):
        calls.append(kwargs)
        return 9

    monkeypatch.setattr(dev_api.runner, "start", start)
    result = await dev_api.resume_run(
        9, database, dev_api.ResumeRequest(message="continue here", max_cost_usd=5),
    )
    assert result == {"run_id": 9}
    assert calls == [{
        "resume_run_id": 9, "operator_message": "continue here", "max_cost_usd": 5,
    }]


@pytest.mark.asyncio
async def test_endpoints_reject_wrong_state_and_missing_run(database):
    with pytest.raises(HTTPException) as error:
        await dev_api.resume_run(9, database)
    assert error.value.status_code == 409
    database.run.state = "finished"
    with pytest.raises(HTTPException) as error:
        await dev_api.send_message(9, dev_api.MessageRequest(text="hello"), database)
    assert error.value.status_code == 409
    database.run = None
    for request in (
        dev_api.resume_run(9, database),
        dev_api.send_message(9, dev_api.MessageRequest(text="hello"), database),
    ):
        with pytest.raises(HTTPException) as error:
            await request
        assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_resume_conflict_does_not_queue_message(database, monkeypatch):
    database.run.state = "finished"

    async def start(*args, **kwargs):
        raise jobs.RunAlreadyActive

    monkeypatch.setattr(dev_api.runner, "start", start)
    with pytest.raises(HTTPException) as error:
        await dev_api.resume_run(9, database, dev_api.ResumeRequest(message="hello"))
    assert error.value.status_code == 409
    assert not database.session.scalars(select(RunEvent)).all()


@pytest.mark.asyncio
async def test_job_forwards_operator_message(monkeypatch):
    seen = {}

    async def run_query(query, on_run_id, **kwargs):
        seen.update(kwargs)
        on_run_id(9)

    monkeypatch.setattr(jobs, "run_query", run_query)
    slot = jobs.RunSlot()
    await slot.start("", resume_run_id=9, operator_message="hello")
    await slot.task
    assert seen["operator_message"] == "hello"


@pytest.mark.parametrize("text", ["", "  ", "x" * 20001])
def test_message_validation(text):
    with pytest.raises(ValidationError):
        dev_api.MessageRequest(text=text)
    with pytest.raises(ValidationError):
        dev_api.ResumeRequest(message=text)


@pytest.mark.asyncio
async def test_failed_delivery_leaves_message_pending(database, monkeypatch):
    queue_message(database, 9, "retry after rollback")
    await database.commit()
    commit = database.commit

    async def fail_commit():
        database.session.flush()
        raise RuntimeError("commit failed")

    monkeypatch.setattr(database, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="commit failed"):
        await deliver_messages(database, 9, 4)
    await database.rollback()
    assert not database.session.scalars(select(Turn)).all()
    assert await has_pending_messages(database, 9)
    monkeypatch.setattr(database, "commit", commit)
    await deliver_messages(database, 9, 4)
    assert not await has_pending_messages(database, 9)
    assert len(database.session.scalars(select(Turn)).all()) == 1


@pytest.mark.asyncio
async def test_resume_records_message_even_when_call_budget_is_exhausted(database):
    agent = runtime(database, Llm())
    await agent._append(4, "assistant", {"message": {"role": "assistant", "content": "done"}})
    queue_message(database, 9, "new request")
    await database.commit()
    result = await agent.resume(4, "continue", max_steps=1)
    assert result.stopped == "call_budget_exhausted"
    assert not agent._llm.messages
    turns = database.session.scalars(select(Turn).where(Turn.kind == "user")).all()
    assert turns[-1].content["content"] == "Message from the operator: new request"
