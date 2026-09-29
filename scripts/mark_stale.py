import argparse
import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from wsignal.config import get_settings
from wsignal.models import Agent, Run


async def mark_stale(dry_run: bool) -> None:
    engine = create_async_engine(get_settings().database_url)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    cutoff = datetime.now(UTC) - timedelta(minutes=10)
    try:
        async with sessionmaker() as session:
            run_ids = list(
                await session.scalars(
                    select(Run.id).where(
                        Run.state == "running",
                        (Run.heartbeat_at.is_(None)) | (Run.heartbeat_at < cutoff),
                    ).order_by(Run.id)
                )
            )
            agent_ids = list(
                await session.scalars(
                    select(Agent.id).where(
                        Agent.run_id.in_(run_ids), Agent.state == "running"
                    ).order_by(Agent.id)
                )
            ) if run_ids else []
            action = "Would mark" if dry_run else "Marked"
            print(f"{action} runs failed: {', '.join(map(str, run_ids)) or 'none'}")
            print(f"{action} agents failed: {', '.join(map(str, agent_ids)) or 'none'}")
            if not dry_run:
                if run_ids:
                    await session.execute(
                        update(Run).where(Run.id.in_(run_ids)).values(state="failed")
                    )
                if agent_ids:
                    await session.execute(
                        update(Agent).where(Agent.id.in_(agent_ids)).values(state="failed")
                    )
                await session.commit()
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(mark_stale(args.dry_run))


if __name__ == "__main__":
    main()
