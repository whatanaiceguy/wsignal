from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from wsignal.config import get_settings
from wsignal.db import get_sessionmaker
from wsignal.inference.pipeline import mark_interrupted_run, run_query
from wsignal.models import Run, RunEvent

logger = logging.getLogger(__name__)


STOP_MESSAGE = "stopped from the UI"
API_RESTART_REASON = "api restarted"


async def running_heartbeats(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> dict[int, datetime | None]:
    async with sessionmaker() as session:
        rows = await session.execute(
            select(Run.id, Run.heartbeat_at).where(Run.state == "running")
        )
        return {run_id: beat for run_id, beat in rows.all()}


async def mark_dead_run(
    sessionmaker: async_sessionmaker[AsyncSession], run_id: int, reason: str
) -> None:
    await mark_interrupted_run(sessionmaker, run_id, reason)
    async with sessionmaker() as session:
        session.add(
            RunEvent(
                run_id=run_id,
                kind="run_stopped",
                payload={"run_id": run_id, "state": "failed", "reason": reason},
            )
        )
        await session.commit()


class RunAlreadyActive(Exception):
    pass


class RunSlot:
    def __init__(self) -> None:
        self.task: asyncio.Task | None = None
        self.run_id: int | None = None

    async def start(
        self,
        query: str,
        top: int | None = None,
        max_research: int | None = None,
        shard_size: int | None = None,
        max_cost_usd: float | None = None,
        resume_run_id: int | None = None,
        operator_message: str | None = None,
    ) -> int:
        if self.task is not None and not self.task.done():
            raise RunAlreadyActive
        self.run_id = resume_run_id
        loop = asyncio.get_running_loop()
        known = loop.create_future()

        def identified(run_id: int) -> None:
            self.run_id = run_id
            if not known.done():
                known.set_result(run_id)

        async def execute() -> Any:
            assigned_run_id = resume_run_id

            def capture(run_id: int) -> None:
                nonlocal assigned_run_id
                assigned_run_id = run_id
                identified(run_id)

            try:
                return await run_query(
                    query,
                    top=top,
                    max_research=max_research,
                    shard_size=shard_size,
                    max_cost_usd=max_cost_usd,
                    resume_run_id=resume_run_id,
                    operator_message=operator_message,
                    on_run_id=capture,
                )
            except Exception:
                if assigned_run_id is not None:
                    try:
                        async with get_sessionmaker()() as session:
                            run = await session.get(Run, assigned_run_id)
                            if run is not None and run.state == "running":
                                run.state = "failed"
                                run.finished_at = datetime.now(UTC)
                                await session.commit()
                    except Exception:
                        pass
                raise

        self.task = asyncio.create_task(execute(), name="wsignal-run")
        task = self.task

        def completed(done: asyncio.Task) -> None:
            if done.cancelled():
                if not known.done():
                    known.set_exception(RuntimeError("run_query отменён до создания запуска"))
                return
            error = done.exception()
            if error is not None:
                logger.error(
                    "Background run task failed",
                    exc_info=(type(error), error, error.__traceback__),
                )
            if known.done():
                return
            if error is not None:
                known.set_exception(error)
            else:
                known.set_exception(RuntimeError("run_query завершился до создания запуска"))

        task.add_done_callback(completed)
        try:
            return await known
        except BaseException:
            if not task.done():
                task.cancel()
            raise

    async def reap_dead_runs(
        self,
        wait_s: float,
        sessionmaker: async_sessionmaker[AsyncSession] | None = None,
    ) -> list[int]:
        maker = sessionmaker or get_sessionmaker()
        before = await running_heartbeats(maker)
        if not before:
            return []
        await asyncio.sleep(wait_s)
        after = await running_heartbeats(maker)
        own = self.run_id if self.task is not None and not self.task.done() else None
        dead = [
            run_id for run_id, beat in before.items()
            if run_id != own and run_id in after and after[run_id] == beat
        ]
        for run_id in dead:
            await mark_dead_run(maker, run_id, API_RESTART_REASON)
            logger.warning("Run %s had no heartbeat after the api started; marked failed", run_id)
        return dead

    async def stop(self, run_id: int, wait_s: float = 30.0) -> bool:
        task = self.task
        if task is None or task.done() or self.run_id != run_id:
            return False
        task.cancel(STOP_MESSAGE)
        await asyncio.wait({task}, timeout=wait_s)
        return True

    async def resume_orphan(self) -> int | None:
        if not get_settings().resume_on_startup:
            return None
        if self.task is not None and not self.task.done():
            return self.run_id
        async with get_sessionmaker()() as session:
            run = await session.scalar(
                select(Run)
                .where(Run.state == "running")
                .order_by(Run.started_at.desc(), Run.id.desc())
                .limit(1)
            )
            run_id = run.id if run is not None else None
        if run_id is None:
            return None
        return await self.start("", resume_run_id=run_id)


runner = RunSlot()


async def iter_events(
    sessionmaker: async_sessionmaker[AsyncSession],
    run_id: int,
    after: int = 0,
    poll_interval: float = 1.0,
    keepalive_interval: float = 15.0,
) -> AsyncIterator[str]:
    cursor = after
    silent_since = time.monotonic()
    while True:
        while True:
            async with sessionmaker() as session:
                rows = (
                    await session.scalars(
                        select(RunEvent)
                        .where(RunEvent.run_id == run_id, RunEvent.id > cursor)
                        .order_by(RunEvent.id)
                        .limit(500)
                    )
                ).all()
            for row in rows:
                cursor = row.id
                payload = {
                    "ts": row.ts.isoformat(),
                    "kind": row.kind,
                    **row.payload,
                }
                encoded = json.dumps(payload, ensure_ascii=False, default=str)
                yield f"id: {row.id}\nevent: {row.kind}\ndata: {encoded}\n\n"
                silent_since = time.monotonic()
            if len(rows) < 500:
                break
        async with sessionmaker() as session:
            run = await session.get(Run, run_id)
            state = run.state if run is not None else "failed"
        if state != "running" and not rows:
            yield f"event: end\ndata: {json.dumps({'state': state}, ensure_ascii=False)}\n\n"
            return
        now = time.monotonic()
        if now - silent_since >= keepalive_interval:
            yield ": keepalive\n\n"
            silent_since = now
        await asyncio.sleep(poll_interval)
