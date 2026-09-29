from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from wsignal.models import Agent, Citation, Document, Entry, Run, RunEvent, Turn


def is_live(state: str, heartbeat_at: datetime | None, now: datetime | None = None) -> bool:
    current = now or datetime.now(UTC)
    return (
        state == "running"
        and heartbeat_at is not None
        and heartbeat_at >= current - timedelta(seconds=60)
    )


def active_elapsed_seconds(
    *,
    started_at: datetime | None,
    finished_at: datetime | None,
    heartbeat_at: datetime | None,
    state: str,
    marks: Iterable[tuple[str, datetime]],
    now: datetime,
) -> float:
    elapsed = 0.0
    segment_start = None
    has_start = False
    for kind, ts in marks:
        if kind == "run_started":
            has_start = True
            if segment_start is not None:
                elapsed += max(0.0, (ts - segment_start).total_seconds())
            segment_start = ts
        elif kind == "run_stopped" and segment_start is not None:
            elapsed += max(0.0, (ts - segment_start).total_seconds())
            segment_start = None

    if not has_start:
        return ((finished_at or now) - started_at).total_seconds() if started_at else 0.0
    if segment_start is not None:
        end = now if state == "running" else finished_at or heartbeat_at
        if end is not None:
            elapsed += max(0.0, (end - segment_start).total_seconds())
    return elapsed


async def run_history(session: AsyncSession) -> list[dict]:
    rows = (await session.scalars(select(Run).order_by(Run.started_at.desc(), Run.id.desc()))).all()
    marks_by_run: dict[int, list[tuple[str, datetime]]] = {}
    if rows:
        marks = await session.execute(
            select(RunEvent.run_id, RunEvent.kind, RunEvent.ts).where(
                RunEvent.run_id.in_([run.id for run in rows]),
                RunEvent.kind.in_(("run_started", "run_stopped")),
            ).order_by(RunEvent.run_id, RunEvent.ts, RunEvent.id)
        )
        for run_id, kind, ts in marks:
            marks_by_run.setdefault(run_id, []).append((kind, ts))
    result = []
    for run in rows:
        agent_count = select(func.count(Agent.id)).where(Agent.run_id == run.id).scalar_subquery()
        entry_count = select(func.count(Entry.id)).where(Entry.run_id == run.id).scalar_subquery()
        turn_count = (
            select(func.count(Turn.id)).join(Agent, Agent.id == Turn.agent_id)
            .where(Agent.run_id == run.id).scalar_subquery()
        )
        document_count = (
            select(func.count(func.distinct(Document.id))).join(
                Agent, Agent.id == Document.fetched_by_agent_id
            ).where(Agent.run_id == run.id).scalar_subquery()
        )
        citation_count = (
            select(func.count(Citation.id)).join(Entry, Entry.id == Citation.entry_id)
            .where(Entry.run_id == run.id).scalar_subquery()
        )
        verified_count = (
            select(func.count(Citation.id)).join(Entry, Entry.id == Citation.entry_id)
            .where(Entry.run_id == run.id, Citation.verified.is_(True)).scalar_subquery()
        )
        cost = (
            select(func.coalesce(func.sum(Turn.cost_usd), 0.0)).join(
                Agent, Agent.id == Turn.agent_id
            ).where(Agent.run_id == run.id).scalar_subquery()
        )
        counts = (
            await session.execute(
                select(
                    agent_count, entry_count, turn_count, document_count,
                    citation_count, cost, verified_count,
                )
            )
        ).one()
        result.append({
            **run.__dict__,
            "is_live": is_live(run.state, run.heartbeat_at),
            "elapsed_s": active_elapsed_seconds(
                started_at=run.started_at,
                finished_at=run.finished_at,
                heartbeat_at=run.heartbeat_at,
                state=run.state,
                marks=marks_by_run.get(run.id, []),
                now=datetime.now(run.started_at.tzinfo),
            ),
            "counts": {
                "agents": counts[0], "entries": counts[1], "turns": counts[2],
                "documents": counts[3], "citations": counts[4],
                "cost_usd": float(counts[5]), "citations_verified": counts[6],
            },
        })
    return result
