from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from wsignal.db import get_session
from wsignal.interface.app import app
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

    class Session:
        async def scalars(self, statement):
            assert "runs.started_at DESC, runs.id DESC" in str(statement)
            return SimpleNamespace(all=lambda: rows)

        async def execute(self, statement):
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
        assert public.status_code == dev.status_code == 200
        data = public.json()
        assert [row["id"] for row in data] == [2, 1]
        assert data[0]["is_live"] is True
        assert data[1]["is_live"] is False
        assert data[1]["elapsed_s"] == 20
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
    finally:
        app.dependency_overrides.clear()
