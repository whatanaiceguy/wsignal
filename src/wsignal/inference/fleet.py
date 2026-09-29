from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from wsignal.inference.agent import AgentFailed, AgentResult, AgentRuntime, RunCostBudget
from wsignal.inference.events import Sink, emit
from wsignal.inference.llm import LlmClient, LlmUnavailable
from wsignal.inference.operator_messages import has_pending_messages
from wsignal.inference.orchestration import Orchestration, interrupt_pending_refutations
from wsignal.inference.tools import RetrievalCache, Toolbox
from wsignal.models import Agent, Field, Refutation, Run, Turn
from wsignal.parsing.http import Fetcher

OUTAGE_WAIT_S = 30.0


class OrchestratorFleet:
    def __init__(
        self,
        session: AsyncSession,
        sessionmaker: async_sessionmaker[AsyncSession],
        runtime: AgentRuntime,
        orchestration: Orchestration,
        llm: LlmClient,
        fetcher: Fetcher,
        cache: RetrievalCache,
        run: Run,
        top_n: int,
        prompts: dict[str, str],
        max_steps: int,
        max_restarts: int,
        sink: Sink | None,
    ) -> None:
        self._session = session
        self._sessionmaker = sessionmaker
        self._runtime = runtime
        self._orchestration = orchestration
        self._llm = llm
        self._fetcher = fetcher
        self._cache = cache
        self._run = run
        self._top_n = top_n
        self._prompts = prompts
        self._max_steps = max_steps
        self._max_restarts = max_restarts
        self._sink = sink
        self._primary: asyncio.Task | None = None
        self._primary_agent: Any | None = None
        self._primary_completed = False
        self._resuming = False
        self._children: dict[int, asyncio.Task] = {}
        self._child_orchestrations: dict[int, Orchestration] = {}
        self._child_runtimes: dict[int, AgentRuntime] = {}
        self._restart_counts: dict[int, int] = {}
        self._children_started = asyncio.Event()
        self._partition_manifest: dict | None = None
        self._restored: dict[int, dict] = {}
        self._child_restore_ready: dict[int, asyncio.Event] = {}
        self._primary_shard_ids: set[int] = set()
        orchestration._summon_callback = self._summoned
        orchestration._end_run_callback = self._end_run
        self._end_run_reason: str | None = None
        self._stop_reason: str | None = None
        self._cost_budget: RunCostBudget | None = getattr(orchestration, "_cost_budget", None)

    async def start(self, agent: Any, message: str) -> None:
        self._primary_agent = agent
        if self._primary_completed:
            return
        self._primary = asyncio.create_task(
            self._run_primary(agent, message),
            name=f"orchestrator-{agent.id}",
        )

    async def _run_primary(self, agent: Any, message: str) -> AgentResult:
        try:
            result = await self._drive(
                self._runtime, agent, message, resume_existing=self._resuming
            )
            if result.stopped == "supervisor_gave_up":
                stored_agent = await self._session.get(Agent, agent.id)
                if stored_agent is not None:
                    stored_agent.state = "failed"
                    await self._session.commit()
            return result
        finally:
            await asyncio.sleep(0)
            if self._partition_manifest is None:
                self._children_started.set()

    async def restore(self, primary_agent: Any) -> dict:
        self._resuming = True
        owners = (
            await self._session.scalars(
                select(Field.orchestrator_agent_id).where(
                    Field.run_id == self._run.id,
                    Field.orchestrator_agent_id.is_not(None),
                ).distinct()
            )
        ).all()
        primary_fields: list[Field] = []
        if owners:
            self._partition_manifest = {"owners": owners}
            self._orchestration._partition_worker = True
            self._orchestration._owner_agent_id = primary_agent.id
            primary_fields = (
                await self._session.scalars(
                    select(Field).where(
                        Field.run_id == self._run.id,
                        Field.orchestrator_agent_id == primary_agent.id,
                    )
                )
            ).all()
            self._orchestration._selected_fields = {field.id for field in primary_fields}
            self._primary_shard_ids = set(self._orchestration._selected_fields)
            self._orchestration._research_budget = len(primary_fields)
            self._orchestration._max_research = max(1, len(primary_fields))
            self._orchestration._researched.update(
                field.id for field in primary_fields if field.state != "pending"
            )
        if owners:
            self._primary_completed = await self._durably_completed(
                primary_agent, primary_fields
            )
        else:
            self._primary_completed = False
        if self._primary_completed and await has_pending_messages(self._session, self._run.id):
            self._primary_completed = False
        primary_restored = {}
        if not self._primary_completed:
            primary_restored = await self._orchestration.restore()
            self._orchestration._selected_fields = set(self._primary_shard_ids)
            self._restored[primary_agent.id] = primary_restored
        children = (
            await self._session.scalars(
                select(Agent).where(
                    Agent.run_id == self._run.id,
                    Agent.role == "orchestrator",
                    Agent.parent_agent_id == primary_agent.id,
                )
            )
        ).all()
        children_by_id = {child.id: child for child in children}
        for owner in owners:
            if owner == primary_agent.id:
                continue
            child = children_by_id.get(owner)
            if child is None:
                continue
            fields = (
                await self._session.scalars(
                    select(Field).where(
                        Field.run_id == self._run.id,
                        Field.orchestrator_agent_id == child.id,
                    )
                )
            ).all()
            if owners and await self._durably_completed(child, fields):
                continue
            self._child_restore_ready[child.id] = asyncio.Event()
            await self._start_child(
                {
                    "orchestrator_agent_id": child.id,
                    "shard": len(self._children) + 2,
                    "field_ids": [field.id for field in fields],
                    "fields": [
                        {
                            "id": field.id,
                            "focus": field.focus,
                            "rationale": field.rationale,
                            "state": field.state,
                        }
                        for field in fields
                    ],
                    "ceiling": len(fields),
                    "brief": child.brief or "",
                    "resume": True,
                }
            )
            await asyncio.sleep(0)
        if self._child_restore_ready:
            await asyncio.gather(
                *(event.wait() for event in self._child_restore_ready.values())
            )
        await self._session.commit()
        self._children_started.set()
        summary = dict(primary_restored)
        summary["returns_replayed"] = sum(
            result.get("returns_replayed", 0) for result in self._restored.values()
        )
        summary["entries_already_written"] = primary_restored.get(
            "entries_already_written", 0
        )
        summary["fields_counted_against_the_ceiling"] = primary_restored.get(
            "fields_counted_against_the_ceiling", 0
        )
        summary["continued_agents"] = sorted(
            {
                agent_id
                for result in self._restored.values()
                for agent_id in result.get("continued_agents", [])
            }
        )
        summary["failed_agents"] = sorted(
            {
                agent_id
                for result in self._restored.values()
                for agent_id in result.get("failed_agents", [])
            }
        )
        return summary

    async def _durably_completed(self, agent: Any, fields: list[Field]) -> bool:
        if agent.state == "failed":
            return False
        if agent.state == "done":
            return True
        last_turn = await self._session.scalar(
            select(Turn)
            .where(Turn.agent_id == agent.id)
            .order_by(Turn.seq.desc())
            .limit(1)
        )
        if last_turn is None:
            return False
        if last_turn.kind != "assistant":
            return False
        content = last_turn.content if isinstance(last_turn.content, dict) else {}
        message = content.get("message")
        return (
            not content.get("error")
            and isinstance(message, dict)
            and not message.get("tool_calls")
        )

    async def _end_run(self, reason: str) -> None:
        if self._end_run_reason is not None:
            return
        self._end_run_reason = reason
        self._stop_reason = reason
        tasks = list(self._children.values())
        if self._primary is not None and self._primary is not asyncio.current_task():
            tasks.append(self._primary)
        await self._orchestration.shutdown()
        for orchestration in self._child_orchestrations.values():
            await orchestration.shutdown()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _summoned(self, manifest: dict) -> None:
        self._partition_manifest = manifest
        for shard in manifest.get("orchestrators", [])[1:]:
            await self._start_child(shard)
        self._children_started.set()

    async def _start_child(self, shard: dict) -> None:
        owner = int(shard["orchestrator_agent_id"])
        if owner in self._children:
            return
        task = asyncio.create_task(self._run_child(shard), name=f"orchestrator-{owner}")
        ready = self._child_restore_ready.get(owner)
        if ready is not None:
            task.add_done_callback(lambda _task: ready.set())
        self._children[owner] = task

    async def _run_child(self, shard: dict) -> AgentResult:
        owner = int(shard["orchestrator_agent_id"])
        async with self._sessionmaker() as session:
            run = await session.get(Run, self._run.id)
            child = await session.get(Agent, owner)
            if run is None or child is None:
                raise RuntimeError(f"shard orchestrator {owner} disappeared")
            toolbox = Toolbox(session, self._fetcher, self._cache)
            runtime = AgentRuntime(session, self._llm, toolbox, self._max_steps, self._sink)
            runtime._cost_budget = getattr(self, "_cost_budget", None)
            self._child_runtimes[owner] = runtime
            orchestration = Orchestration(
                session=session,
                sessionmaker=self._sessionmaker,
                runtime=runtime,
                llm=self._llm,
                fetcher=self._fetcher,
                run=run,
                prompts=self._prompts,
                budget_minutes=self._orchestration._budget.minutes,
                budget_started_at=self._orchestration._budget.started_at,
                max_research=int(shard["ceiling"]),
                top_n=self._top_n,
                agent_max_steps=self._max_steps,
                cache=self._cache,
                sink=self._sink,
                owner_agent_id=owner,
                initial_agent_id=self._orchestration._initial_agent_id,
                shard_number=int(shard.get("shard") or 1),
                child_shard=True,
                partition_worker=True,
                shard_size=self._orchestration._shard_size,
            )
            orchestration._budget = self._orchestration._budget
            orchestration._cost_budget = getattr(self, "_cost_budget", None)
            orchestration._returns = asyncio.Queue()
            orchestration._inflight = {}
            orchestration._selected_fields = set(shard.get("field_ids") or [])
            orchestration.install(toolbox)
            orchestration._end_run_callback = self._end_run
            self._child_orchestrations[owner] = orchestration
            if shard.get("resume"):
                restored = await orchestration.restore()
                self._restored[owner] = restored
                ready = self._child_restore_ready.get(owner)
                if ready is not None:
                    ready.set()
                await session.commit()
                fields = (
                    await session.scalars(
                        select(Field).where(
                            Field.run_id == self._run.id,
                            Field.orchestrator_agent_id == owner,
                        )
                    )
                ).all()
                shard["ceiling"] = len(fields)
                shard["fields"] = [
                    {
                        "id": field.id,
                        "focus": field.focus,
                        "rationale": field.rationale,
                    }
                    for field in fields
                ]
            else:
                restored = {}
            message = self._child_message(shard, restored)
            try:
                result = await self._drive(
                    runtime, child, message, resume_existing=bool(shard.get("resume"))
                )
                if result.stopped == "supervisor_gave_up":
                    child.state = "failed"
                    await session.commit()
                pending = (
                    await session.scalars(
                        select(Refutation).where(
                            Refutation.run_id == self._run.id,
                            Refutation.state == "pending",
                        ).join(
                            Agent, Refutation.researcher_agent_id == Agent.id
                        ).where(Agent.parent_agent_id == owner)
                    )
                ).all()
                await interrupt_pending_refutations(
                    session, pending, "shard finished before the exchange completed"
                )
                await session.commit()
                return result
            finally:
                await orchestration.shutdown()

    def _child_message(self, shard: dict, restored: dict) -> str:
        fields = shard.get("fields") or []
        listed = "\n".join(
            f"- field_id={field['id']}: {field.get('focus', '')}\n"
            f"  rationale: {field.get('rationale') or ''}"
            for field in fields
        )
        message = (
            f"Shard {shard.get('shard', 1)} of this run. Work only on these fields.\n"
            f"Local research ceiling: {shard.get('ceiling', len(fields))}.\n"
            f"Focus and rationale:\n{listed}\n\n"
            f"Brief: {shard.get('brief') or ''}"
        )
        if getattr(self._run, "seeded", False):
            message += (
                "\n\nThis is a seeded run. Do not split or add fields. Dispatch every "
                "assigned pending field, refute every researcher return, collect the "
                "exchanges, and write an entry for every assigned field, including "
                "noise and insufficient."
            )
        if shard.get("resume"):
            message += (
                f"\n\nThis is a resumed shard. Returns waiting: "
                f"{restored.get('returns_replayed', 0)}. Continue from your history."
            )
        return message

    async def _drive(
        self,
        runtime: AgentRuntime,
        agent: Any,
        message: str,
        resume_existing: bool = False,
    ) -> AgentResult:
        last: Exception | None = None
        self._restart_counts[agent.id] = 0
        for attempt in range(self._max_restarts + 1):
            try:
                if attempt == 0 and not resume_existing:
                    return await runtime.run(agent, message, max_steps=self._max_steps)
                return await runtime.resume(agent.id, message, max_steps=self._max_steps)
            except Exception as exc:
                outage = isinstance(exc, AgentFailed) and isinstance(
                    exc.__cause__, LlmUnavailable
                )
                if outage and attempt < self._max_restarts:
                    emit(
                        self._sink,
                        "model_outage_wait",
                        agent_id=agent.id,
                        attempt=attempt + 1,
                        wait_s=OUTAGE_WAIT_S * (attempt + 1),
                        error=str(exc)[:200],
                    )
                    await asyncio.sleep(OUTAGE_WAIT_S * (attempt + 1))
                if isinstance(exc, AgentFailed) and not outage:
                    emit(
                        self._sink,
                        "agent_failed",
                        agent_id=agent.id,
                        reason=exc.reason,
                        error=str(exc)[:200],
                    )
                    return AgentResult(
                        agent.id, None, 0, f"failed:{exc.reason}", 0
                    )
                last = exc
                if attempt == self._max_restarts:
                    emit(
                        self._sink,
                        "supervisor_gave_up",
                        agent_id=agent.id,
                        attempts=attempt + 1,
                        error=f"{type(exc).__name__}: {exc}"[:200],
                    )
                    return AgentResult(agent.id, None, 0, "supervisor_gave_up", 0)
                self._restart_counts[agent.id] += 1
                try:
                    recovered_calls = await runtime.recover_session(agent.id, exc)
                except Exception as recovery_exc:
                    recovered_calls = 0
                    emit(
                        self._sink,
                        "supervisor_recovery_failed",
                        agent_id=agent.id,
                        error=f"{type(recovery_exc).__name__}: {recovery_exc}"[:200],
                    )
                emit(
                    self._sink,
                    "supervisor_restart",
                    agent_id=agent.id,
                    attempt=attempt + 1,
                    error=f"{type(exc).__name__}: {exc}"[:200],
                )
                message = (
                    f"Предыдущий вызов оборвался: {exc}. "
                    f"Сессия базы сброшена; незавершённых вызовов инструментов закрыто: "
                    f"{recovered_calls}. Твоя история цела: посмотри, что уже сделано, "
                    "повтори прерванный вызов при необходимости и продолжай."
                )
        raise RuntimeError(str(last))

    async def _time_inflight(self) -> dict[str, int]:
        orchestrations = [self._orchestration, *self._child_orchestrations.values()]
        async with self._sessionmaker() as session:
            unwritten = await session.scalar(
                select(func.count()).select_from(Field).where(
                    Field.run_id == self._run.id, Field.state == "dispatched",
                )
            )
            reviews = await session.scalar(
                select(func.count()).select_from(Refutation).where(
                    Refutation.run_id == self._run.id,
                    or_(Refutation.state == "pending", Refutation.collected.is_(False)),
                )
            )
        return {
            "agents_running": sum(item.inflight for item in orchestrations),
            "returns_uncollected": sum(item._returns.qsize() for item in orchestrations),
            "fields_unwritten": unwritten or 0,
            "refutations_unfinished": reviews or 0,
        }

    async def _extend_time(self) -> bool:
        budget = self._orchestration._budget
        counts = await self._time_inflight()
        if not budget.extend(counts):
            return False
        emit(
            self._sink, "research_extended", run_id=self._run.id,
            extension=budget.extensions, grace_s=budget.grace_s, inflight=counts,
        )
        return True

    async def wait(self) -> tuple[str | None, int, str]:
        budget = getattr(self._orchestration, "_budget", None)
        if budget is None:
            return await self._wait()
        completion = asyncio.create_task(self._wait())
        try:
            while True:
                done, _ = await asyncio.wait(
                    {completion}, timeout=max(0, budget.remaining_s),
                )
                if done:
                    return completion.result()
                if await self._extend_time():
                    continue
                self._stop_reason = "active-time budget exhausted"
                emit(
                    self._sink, "time_limit_reached", run_id=self._run.id,
                    extensions=budget.extensions, reason=self._stop_reason,
                )
                await self.shutdown()
                return None, sum(self._restart_counts.values()), "time_limit_reached"
        finally:
            if not completion.done():
                completion.cancel()
            await asyncio.gather(completion, return_exceptions=True)

    async def _wait(self) -> tuple[str | None, int, str]:
        if self._primary is None and self._primary_agent is None:
            raise RuntimeError("fleet was not started")
        if self._primary is None:
            agent = await self._session.get(Agent, self._primary_agent.id)
            content = await self._orchestration._final_answer(self._primary_agent.id)
            if agent and agent.state == "done":
                content = content or "Orchestrator finished."
            stopped = (
                "supervisor_gave_up"
                if agent and agent.state == "failed"
                else "answered"
                if content
                else "max_steps"
            )
            primary = AgentResult(self._primary_agent.id, content, 0, stopped, 0)
        else:
            primary = await self._primary
        await self._children_started.wait()
        results = list(
            await asyncio.gather(*self._children.values(), return_exceptions=True)
        )
        restarts = sum(self._restart_counts.values())
        for owner, result in zip(self._children, results, strict=True):
            if not isinstance(result, AgentResult):
                emit(
                    self._sink,
                    "shard_failed",
                    orchestrator_agent_id=owner,
                    error=str(result)[:200],
                )
        if self._end_run_reason is not None:
            return None, restarts, "run_ended"
        failed = (
            primary.stopped == "supervisor_gave_up"
            or primary.stopped.startswith("failed:")
            or any(
                not isinstance(result, AgentResult)
                or result.stopped == "supervisor_gave_up"
                or result.stopped.startswith("failed:")
                for result in results
            )
        )
        cost_budget = getattr(self, "_cost_budget", None)
        cost_limit_reached = primary.stopped == "cost_limit_reached" or any(
            isinstance(result, AgentResult) and result.stopped == "cost_limit_reached"
            for result in results
        ) or (cost_budget is not None and cost_budget.reason is not None)
        budget_exhausted = primary.stopped == "call_budget_exhausted" or any(
            isinstance(result, AgentResult) and result.stopped == "call_budget_exhausted"
            for result in results
        )
        unfinished = (
            primary.stopped != "answered"
            or any(
                isinstance(result, AgentResult) and result.stopped != "answered"
                for result in results
            )
            or bool(await self._unfinished_owned_states())
        )
        if failed:
            stopped = "supervisor_gave_up"
        elif cost_limit_reached:
            stopped = "cost_limit_reached"
            self._stop_reason = (
                cost_budget.reason
                if cost_budget is not None
                else "run cost limit reached"
            )
        elif budget_exhausted:
            stopped = "call_budget_exhausted"
            self._stop_reason = (
                primary.content
                if primary.stopped == "call_budget_exhausted"
                else next(
                    result.content
                    for result in results
                    if isinstance(result, AgentResult)
                    and result.stopped == "call_budget_exhausted"
                )
            )
        elif unfinished:
            stopped = "max_steps"
        else:
            stopped = "answered"
        return primary.content, restarts, stopped

    async def _unfinished_owned_states(self) -> list[str]:
        rows = (
            await self._session.scalars(
                select(Field.state).where(
                    Field.run_id == self._run.id,
                    Field.orchestrator_agent_id.is_not(None),
                    Field.state.in_(("pending", "dispatched")),
                    or_(
                        Field.state == "dispatched",
                        Field.orchestrator_agent_id.not_in(
                            select(Agent.id).where(Agent.state == "done")
                        ),
                    ),
                )
            )
        ).all()
        return list(rows)

    @property
    def inflight(self) -> int:
        return (
            self._orchestration.inflight
            + sum(child.inflight for child in self._child_orchestrations.values())
            + sum(1 for task in self._children.values() if not task.done())
            + int(self._primary is not None and not self._primary.done())
        )

    async def shutdown(self) -> None:
        await self._orchestration.shutdown()
        for orchestration in self._child_orchestrations.values():
            await orchestration.shutdown()
        tasks = list(self._children.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._primary is not None and not self._primary.done():
            self._primary.cancel()
            await asyncio.gather(self._primary, return_exceptions=True)

    @property
    def restored(self) -> dict:
        return self._restored

    @property
    def partitioned(self) -> bool:
        return self._partition_manifest is not None
