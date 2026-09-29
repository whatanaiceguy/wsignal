from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from wsignal.models import Agent, Citation, Document, Entry, Run, Turn


def is_live(state: str, heartbeat_at: datetime | None, now: datetime | None = None) -> bool:
    current = now or datetime.now(UTC)
    return (
        state == "running"
        and heartbeat_at is not None
        and heartbeat_at >= current - timedelta(seconds=60)
    )


async def run_history(session: AsyncSession) -> list[dict]:
    rows = (await session.scalars(select(Run).order_by(Run.started_at.desc(), Run.id.desc()))).all()
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
        finished = run.finished_at or datetime.now(run.started_at.tzinfo)
        result.append({
            **run.__dict__,
            "is_live": is_live(run.state, run.heartbeat_at),
            "elapsed_s": (finished - run.started_at).total_seconds(),
            "counts": {
                "agents": counts[0], "entries": counts[1], "turns": counts[2],
                "documents": counts[3], "citations": counts[4],
                "cost_usd": float(counts[5]), "citations_verified": counts[6],
            },
        })
    return result
