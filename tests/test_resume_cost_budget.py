from types import SimpleNamespace

import pytest

from wsignal.inference.agent import RunCostBudget
from wsignal.inference.pipeline import _resume_cost_budget


@pytest.mark.asyncio
async def test_resume_preserves_stored_limit_and_seeds_spend_from_turn_costs():
    class Session:
        async def scalar(self, _statement):
            return 2.75

    run = SimpleNamespace(id=8, max_cost_usd=5.0)
    budget = RunCostBudget(None)

    await _resume_cost_budget(Session(), run, None, budget)

    assert budget.limit_usd == 5.0
    assert budget.spent_usd == 2.75
    assert budget.reason is None


@pytest.mark.asyncio
async def test_resume_cost_override_is_recorded_and_existing_spend_can_exhaust_it():
    class Session:
        async def scalar(self, _statement):
            return 3.0

    run = SimpleNamespace(id=8, max_cost_usd=5.0)
    budget = RunCostBudget(None)

    await _resume_cost_budget(Session(), run, 2.0, budget)

    assert run.max_cost_usd == budget.limit_usd == 2.0
    assert budget.spent_usd == 3.0
    assert "run cost limit reached" in budget.reason
