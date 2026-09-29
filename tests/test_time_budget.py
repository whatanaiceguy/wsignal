import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from wsignal.inference.agent import _tool_result_signature
from wsignal.inference.fleet import OrchestratorFleet
from wsignal.inference.orchestration import Budget, Orchestration


def expired_budget(**kwargs):
    return Budget(datetime.now(UTC) - timedelta(seconds=61), 1, **kwargs)


def fleet_for(budget, counts):
    fleet = object.__new__(OrchestratorFleet)
    fleet._orchestration = SimpleNamespace(_budget=budget)
    fleet._run = SimpleNamespace(id=9)
    fleet._restart_counts = {}
    fleet.events = []
    fleet._sink = fleet.events.append
    fleet._time_inflight = AsyncMock(return_value=counts)
    fleet.shutdown = AsyncMock()
    return fleet


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [
    "agents_running", "returns_uncollected", "fields_unwritten", "refutations_unfinished",
])
async def test_extension_preserves_each_kind_of_inflight_work(kind):
    budget = expired_budget()
    fleet = fleet_for(budget, {kind: 1})

    assert await fleet._extend_time()
    assert budget.extensions == 1
    assert 599 < budget.remaining_s <= 600
    fleet.shutdown.assert_not_awaited()
    assert fleet.events == [{
        "kind": "research_extended", "run_id": 9, "extension": 1,
        "grace_s": 600, "inflight": {kind: 1},
    }]


@pytest.mark.asyncio
@pytest.mark.parametrize("method", [
    "_dispatch_researchers", "_split_direction", "_summon_orchestrators",
])
async def test_new_work_refused_during_extension(method):
    orchestration = object.__new__(Orchestration)
    orchestration._budget = expired_budget()
    assert orchestration._budget.extend({"agents_running": 1})

    result = await getattr(orchestration, method)({}, 4)

    assert "no new research or fields" in result["error"]
    assert "refute and write" in result["error"]


@pytest.mark.asyncio
async def test_next_tool_result_reports_extension_once():
    orchestration = object.__new__(Orchestration)
    orchestration._budget = expired_budget()
    orchestration._budget.extend({"fields_unwritten": 1})
    orchestration._partition_worker = False
    orchestration.status = AsyncMock(return_value={"time": "extended 1/2"})
    orchestration._error_warning = AsyncMock(return_value=None)
    handler = AsyncMock(return_value={"ok": True})

    first = dict(await orchestration._with_status(handler)({}, 4))
    handler.return_value = {"ok": True}
    second = await orchestration._with_status(handler)({}, 4)

    assert handler.await_count == 2
    assert first["ok"] is True
    assert "Finish, collect" in first["_time_warning"]
    assert "_time_warning" not in second


@pytest.mark.asyncio
@pytest.mark.parametrize("counts,max_extensions", [({}, 2), ({"agents_running": 1}, 0)])
async def test_no_extension_hard_stops(counts, max_extensions):
    fleet = fleet_for(expired_budget(max_extensions=max_extensions), counts)

    async def running():
        await asyncio.Event().wait()

    fleet._wait = running
    result = await fleet.wait()

    assert result == (None, 0, "time_limit_reached")
    fleet.shutdown.assert_awaited_once()
    assert [event["kind"] for event in fleet.events] == ["time_limit_reached"]


@pytest.mark.asyncio
async def test_hard_stop_after_max_extensions():
    budget = expired_budget(grace_s=0.001, max_extensions=2)
    fleet = fleet_for(budget, {"fields_unwritten": 1})

    async def running():
        await asyncio.Event().wait()

    fleet._wait = running
    assert await fleet.wait() == (None, 0, "time_limit_reached")
    assert budget.extensions == 2
    assert [event["kind"] for event in fleet.events] == [
        "research_extended", "research_extended", "time_limit_reached",
    ]
    fleet.shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_fleet_finishes_normally_within_grace():
    budget = expired_budget()
    fleet = fleet_for(budget, {"fields_unwritten": 1})

    async def running():
        while not budget.extensions:
            await asyncio.sleep(0)
        return "written", 0, "answered"

    fleet._wait = running
    assert await fleet.wait() == ("written", 0, "answered")
    fleet.shutdown.assert_not_awaited()


def test_status_clock_does_not_defeat_repeat_guard():
    assert _tool_result_signature({"_status": {"time": "extended 1/2, 540 s left"}}) == (
        _tool_result_signature({"_status": {"time": "extended 1/2, 539 s left"}})
    )


@pytest.mark.asyncio
async def test_inflight_counts_include_database_work_after_queue_is_drained():
    statements = []
    values = iter([3, 2])

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def scalar(self, statement):
            statements.append(str(statement))
            return next(values)

    fleet = object.__new__(OrchestratorFleet)
    fleet._sessionmaker = Session
    fleet._run = SimpleNamespace(id=9)
    fleet._orchestration = SimpleNamespace(inflight=0, _returns=asyncio.Queue())
    fleet._child_orchestrations = {
        20: SimpleNamespace(inflight=1, _returns=asyncio.Queue()),
    }
    fleet._child_orchestrations[20]._returns.put_nowait({"kind": "research"})

    assert await fleet._time_inflight() == {
        "agents_running": 1,
        "returns_uncollected": 1,
        "fields_unwritten": 3,
        "refutations_unfinished": 2,
    }
    assert "fields.state" in statements[0]
    assert "refutations.collected IS false" in statements[1]
