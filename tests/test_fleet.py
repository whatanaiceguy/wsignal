from types import SimpleNamespace

import pytest

from wsignal.inference.fleet import OrchestratorFleet
from wsignal.inference.orchestration import partition_fields


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (10, [10]),
        (11, [6, 5]),
        (12, [6, 6]),
        (20, [10, 10]),
        (25, [9, 8, 8]),
        (30, [10, 10, 10]),
    ],
)
def test_partition_fields_balances_all_fields(count, expected):
    fields = [SimpleNamespace(id=index) for index in range(count)]
    shards = partition_fields(fields)

    assert [len(shard) for shard in shards] == expected
    flattened = [field.id for shard in shards for field in shard]
    assert sorted(flattened) == list(range(count))
    assert len(flattened) == len(set(flattened))



def test_fleet_inflight_counts_every_shard_researcher_and_orchestrator_task():
    fleet = object.__new__(OrchestratorFleet)
    fleet._orchestration = SimpleNamespace(inflight=2)
    fleet._child_orchestrations = {
        5: SimpleNamespace(inflight=3),
        6: SimpleNamespace(inflight=0),
    }
    fleet._primary = SimpleNamespace(done=lambda: False)
    fleet._children = {
        5: SimpleNamespace(done=lambda: False),
        6: SimpleNamespace(done=lambda: True),
    }

    assert fleet.inflight == 2 + 3 + 1 + 1
