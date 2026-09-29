import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from wsignal.inference.orchestration import Orchestration


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class _Session:
    def __init__(self, agents, review):
        self.agents = agents
        self.review = review

    async def get(self, model, identity):
        if model.__name__ == "Refutation":
            return self.review if identity == self.review.id else None
        return self.agents.get(identity)

    async def scalars(self, statement):
        if "refutations" in str(statement):
            return _Rows([self.review])
        return _Rows([])

    async def scalar(self, statement):
        return None

    async def commit(self):
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "target_id", "resumed_agent_id"),
    [("resume_agent", 40, 40), ("resume_agent", 41, 41), ("refute", 40, 41)],
)
async def test_resume_interrupted_exchange_reuses_review_and_agents(
    tool, target_id, resumed_agent_id
):
    researcher = SimpleNamespace(
        id=40, run_id=1, state="failed", role="researcher",
        parent_agent_id=1,
    )
    refuter = SimpleNamespace(
        id=41, run_id=1, state="failed", role="refuter",
        parent_agent_id=1, brief="Check the claim.",
    )
    review = SimpleNamespace(
        id=7, run_id=1, researcher_agent_id=40, refuter_agent_id=41,
        rebuttal_required=True, state="cancelled", collected=True,
        outcome={"stopped": "cancelled"}, completed_at=datetime.now(UTC),
        delivery_version=1,
    )
    session = _Session({40: researcher, 41: refuter}, review)
    orchestration = Orchestration(
        session=session, sessionmaker=None,
        runtime=None, llm=None, fetcher=None, run=SimpleNamespace(id=1),
        prompts={},
    )
    orchestration._owner_agent_id = 1
    calls = []

    async def continue_exchange(*args, **kwargs):
        calls.append((args, kwargs))

    orchestration._refuter_task = continue_exchange
    if tool == "resume_agent":
        result = await orchestration._resume_agent(
            {"agent_id": target_id, "message": "Continue the interrupted exchange."}, 1
        )
    else:
        result = await orchestration._refute(
            {"agent_id": target_id, "instruction": "Continue the interrupted exchange."},
            1,
        )
    await asyncio.gather(*orchestration._inflight.values())

    assert result["resumed"] is True
    assert result["refutation_id"] == 7
    assert result["refuter_agent_id"] == 41
    assert review.state == "pending"
    assert review.collected is False
    assert review.outcome is None
    assert calls[0][0][:3] == (7, 41, 40)
    assert calls[0][1]["resume_existing"] is True
    assert calls[0][1]["resume_target_id"] == resumed_agent_id
    assert calls[0][1]["resume_message"] == "Continue the interrupted exchange."
