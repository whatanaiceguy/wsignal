from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from wsignal.db import get_session
from wsignal.interface.app import app
from wsignal.interface.run_history import active_elapsed_seconds
from wsignal.models import Run


@pytest.mark.asyncio
async def test_public_and_dev_history_share_counts_and_preserve_contract():
    now = datetime.now(UTC)
    rows = [
        Run(id=2, query="new", state="running", started_at=now, heartbeat_at=now),
        Run(
            id=1, query="old", direction_ru="Тема", state="finished",
            started_at=now - timedelta(seconds=20), finished_at=now,
        ),
    ]

    for row in rows:
        for field in ("direction_ru", "direction_en", "max_research", "top_n",
                      "finished_at", "heartbeat_at"):
            if field not in row.__dict__:
                setattr(row, field, None)

    event_queries = []

    class Session:
        async def scalars(self, statement):
            assert "runs.started_at DESC, runs.id DESC" in str(statement)
            return SimpleNamespace(all=lambda: rows)

        async def execute(self, statement):
            if "run_events" in str(statement):
                event_queries.append(statement)
                assert statement.compile().params["run_id_1"] == [2, 1]
                return [
                    (1, "run_started", now - timedelta(seconds=20)),
                    (1, "run_stopped", now - timedelta(seconds=15)),
                    (1, "run_started", now - timedelta(seconds=5)),
                    (1, "run_stopped", now),
                ]
            assert "citations.verified IS true" in str(statement)
            return SimpleNamespace(one=lambda: (3, 4, 5, 6, 7, 0.25, 2))

    async def session():
        yield Session()

    app.dependency_overrides[get_session] = session
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            public = await client.get("/api/runs")
            dev = await client.get("/api/dev/runs")
        assert len(event_queries) == 2
        assert public.status_code == dev.status_code == 200
        data = public.json()
        assert [row["id"] for row in data] == [2, 1]
        assert data[0]["is_live"] is True
        assert data[1]["is_live"] is False
        assert data[1]["elapsed_s"] == 10
        assert data[0]["counts"] == {
            "agents": 3, "entries": 4, "documents": 6, "citations": 7,
            "cost_usd": 0.25, "citations_verified": 2,
        }
        assert dev.json()[0]["counts"] == {
            "agents": 3, "entries": 4, "turns": 5, "documents": 6,
            "citations": 7, "cost_usd": 0.25,
        }
        rows.clear()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            assert (await client.get("/api/runs")).json() == []
        assert len(event_queries) == 2
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize(
    "state,finished,heartbeat,marks,expected",
    [
        pytest.param("finished", 20, None, [("run_started", 0), ("run_stopped", 20)],
                     20.0, id="single-finished"),
        pytest.param("exhausted", 6062, None, [
            ("run_started", 0), ("run_stopped", 2402),
            ("run_started", 6023), ("run_stopped", 6062),
        ], 2441.0, id="stop-resume-stop"),
        pytest.param("running", 20, 25, [
            ("run_started", 0), ("run_stopped", 10), ("run_started", 20),
        ], 20.0, id="running-open-uses-now"),
        pytest.param("finished", 20, None, [], 20.0, id="no-events-finished"),
        pytest.param("running", None, None, [], 30.0, id="no-events-running"),
        pytest.param("failed", None, 10, [("run_stopped", 5)],
                     30.0, id="no-start-falls-back"),
        pytest.param("finished", 20, None, [
            ("run_started", 0), ("run_started", 10), ("run_stopped", 20),
        ], 20.0, id="start-start-stop"),
        pytest.param("failed", 20, 25, [("run_started", 5)],
                     15.0, id="open-uses-finished"),
        pytest.param("interrupted", None, 25, [("run_started", 5)],
                     20.0, id="open-uses-heartbeat"),
        pytest.param("failed", None, None, [("run_started", 5)],
                     0.0, id="open-without-end-does-not-tick"),
        pytest.param("running", None, None, [
            ("run_stopped", 0), ("run_started", 5), ("run_stopped", 10),
            ("run_stopped", 15),
        ], 5.0, id="unmatched-stops"),
    ],
)
def test_active_elapsed_seconds(state, finished, heartbeat, marks, expected):
    start = datetime(2026, 9, 29, 17, 52, tzinfo=UTC)

    def at(seconds):
        return start + timedelta(seconds=seconds) if seconds is not None else None

    elapsed = active_elapsed_seconds(
        started_at=start,
        finished_at=at(finished),
        heartbeat_at=at(heartbeat),
        state=state,
        marks=[(kind, at(seconds)) for kind, seconds in marks],
        now=at(30),
    )

    assert elapsed == expected
    assert isinstance(elapsed, float)
