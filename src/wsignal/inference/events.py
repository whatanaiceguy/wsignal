from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from wsignal.models import RunEvent

logger = logging.getLogger(__name__)

Sink = Callable[[dict[str, Any]], None]


def emit(sink: Sink | None, kind: str, **fields: Any) -> None:
    if sink is None:
        return
    try:
        sink({"kind": kind, **fields})
    except Exception:
        pass


class PersistedSink:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        sink: Sink | None,
        run_id: int | None = None,
    ) -> None:
        self.sink = sink
        self.run_id = run_id
        self.queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.failed = False
        self.writer: asyncio.Task | None = None
        self.buffer: list[dict[str, Any]] = []
        self.sessionmaker = sessionmaker
        self.closed = False

    def __call__(self, event: dict[str, Any]) -> None:
        emit(self.sink, event["kind"], **{k: v for k, v in event.items() if k != "kind"})
        if self.closed:
            return
        event = dict(event)
        kind = event.get("kind")
        if kind == "run_started":
            self.run_id = int(event["run_id"])
            self._queue_buffered()
            self._enqueue(event)
            return
        if kind == "heartbeat":
            return
        if self.run_id is None:
            self.buffer.append(dict(event))
            return
        self._enqueue(event)

    def _queue_buffered(self) -> None:
        buffered, self.buffer = self.buffer, []
        for event in buffered:
            if event.get("kind") != "heartbeat":
                self._enqueue(event)

    def _enqueue(self, event: dict[str, Any]) -> None:
        if self.writer is None:
            self.writer = asyncio.create_task(self._write(), name="run-event-writer")
        self.queue.put_nowait({"run_id": self.run_id, "event": dict(event)})

    async def _write(self) -> None:
        while True:
            item = await self.queue.get()
            try:
                if item is None:
                    return
                async with self.sessionmaker() as session:
                    event = item["event"]
                    session.add(
                        RunEvent(
                            run_id=item["run_id"],
                            kind=event["kind"],
                            payload=json.loads(
                                json.dumps(
                                    {k: v for k, v in event.items() if k != "kind"},
                                    ensure_ascii=False,
                                    default=str,
                                )
                            ),
                        )
                    )
                    try:
                        await session.flush()
                        await session.commit()
                    except Exception:
                        logger.exception("Failed to persist run event for run %s", item["run_id"])
                        await session.rollback()
                        self.failed = True
            except Exception:
                logger.exception("Run event writer failed")
                self.failed = True
            finally:
                self.queue.task_done()

    async def flush(self) -> None:
        if self.writer is not None:
            await self.queue.join()

    async def drain(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.buffer.clear()
        if self.writer is not None:
            await self.queue.join()
            self.queue.put_nowait(None)
            await self.writer
            self.writer = None


def wrap_sink(
    sink: Sink | None,
    sessionmaker: async_sessionmaker[AsyncSession],
    run_id: int | None = None,
) -> PersistedSink:
    return PersistedSink(sessionmaker, sink, run_id)
