from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from wsignal.interface.report import build_response


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows

    def __iter__(self):
        return iter(self.rows)


class _ReportSession:
    def __init__(self, entries):
        self.entries = entries
        self.scalars_calls = 0
        self.entry_statement = None
        self.entry_results = [entries]

    async def get(self, _model, identity):
        return SimpleNamespace(
            id=identity,
            query="run across shards",
            state="finished",
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
            heartbeat_at=None,
        )

    async def scalars(self, statement):
        self.scalars_calls += 1
        if self.scalars_calls == 1:
            self.entry_statement = statement
            return _Result(self.entry_results.pop(0))
        self.scoped_statement = statement
        return _Result([])

    async def execute(self, _statement):
        return _Result([])

    async def scalar(self, _statement):
        return 0


def _entry(identity, shard, name):
    return SimpleNamespace(
        id=identity,
        run_id=7,
        name_ru=name,
        name_en=None,
        transition_ru="transition",
        score=0.7,
        state="noise",
        why_ru="reason",
        current_state_ru=None,
        dynamics_ru=None,
        what_would_refute_ru=None,
        searches_run=0,
        sources_checked=0,
        problem_ru=None,
        advantage_ru=None,
        case_example_ru=None,
        field_id=shard,
        refuter_agent_id=None,
        corpus_query=None,
    )


@pytest.mark.asyncio
async def test_final_report_reads_entries_written_by_every_shard():
    entries = [_entry(1, 101, "primary entry"), _entry(2, 202, "child entry")]
    session = _ReportSession(entries)

    response = await build_response(session, 7)

    assert response.run_id == 7
    assert response.state == "finished"
    assert [entry.name_ru for entry in response.entries] == ["primary entry", "child entry"]
    sql = str(session.entry_statement.compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
    ))
    assert "entries.run_id = 7" in sql
    assert "orchestrator_agent_id" not in sql
    assert session.entry_results == []
