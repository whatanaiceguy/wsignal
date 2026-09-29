from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from wsignal.config import get_settings
from wsignal.db import get_sessionmaker
from wsignal.inference.accounting import per_agent, per_run
from wsignal.inference.agent import AgentRuntime, RunCostBudget
from wsignal.inference.events import Sink, emit, wrap_sink
from wsignal.inference.fleet import OrchestratorFleet
from wsignal.inference.llm import LlmClient
from wsignal.inference.operator_messages import queue_message
from wsignal.inference.orchestration import Orchestration, interrupt_pending_refutations
from wsignal.inference.prompts import describe, load_prompts
from wsignal.inference.seeded import (
    metadata_from_rationale,
    seed_metadata,
    seed_rationale,
    supplied_claim,
)
from wsignal.inference.tools import RetrievalCache, Toolbox
from wsignal.inference.verify import verify_run
from wsignal.models import Agent, Alias, Entry, Field, Refutation, Run, Technology, Turn
from wsignal.parsing.http import Fetcher
from wsignal.parsing.page_worker import shutdown_page_processor


@dataclass(slots=True)
class RunOutcome:
    run_id: int
    state: str
    reason: str | None
    summary: str | None
    top_n: int
    max_research: int
    restarts: int
    verification: dict[str, int]
    models: dict[str, str]
    prompts: dict[str, dict[str, str | int]]
    accounting: dict
    agents: list[dict]
    http_requests: int


def interruption_reason(exc: BaseException) -> str:
    if isinstance(exc, asyncio.CancelledError) and exc.args and exc.args[0]:
        return str(exc.args[0])
    return f"run interrupted: {type(exc).__name__}"


async def mark_interrupted_run(
    sessionmaker: async_sessionmaker[AsyncSession], run_id: int, reason: str
) -> None:
    async with sessionmaker() as session:
        run = await session.get(Run, run_id)
        if run is None or run.state != "running":
            return
        pending = list(
            (
                await session.scalars(
                    select(Refutation).where(
                        Refutation.run_id == run_id, Refutation.state == "pending"
                    )
                )
            ).all()
        )
        await interrupt_pending_refutations(session, pending, reason)
        await session.execute(
            update(Agent)
            .where(Agent.run_id == run_id, Agent.state == "running")
            .values(state="failed")
        )
        run.state = "failed"
        run.finished_at = datetime.now(UTC)
        await session.commit()


async def run_query(
    query: str,
    top: int | None = None,
    max_research: int | None = None,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
    max_restarts: int = 2,
    max_steps: int | None = None,
    sink: Sink | None = None,
    resume_run_id: int | None = None,
    operator_message: str | None = None,
    shard_size: int | None = None,
    max_cost_usd: float | None = None,
    on_run_id: Callable[[int], None] | None = None,
    seed: list[dict] | None = None,
    seed_filename: str | None = None,
) -> RunOutcome:
    settings = get_settings()
    max_steps = settings.agent_max_steps if max_steps is None else max_steps
    sessionmaker = sessionmaker or get_sessionmaker()
    prompts = load_prompts()
    top_n = settings.top_n if top is None else top
    research = (
        len(seed)
        if seed is not None and max_research is None
        else settings.max_research if max_research is None else max_research
    )
    if seed is not None and max_research is not None and max_research < len(seed):
        raise ValueError("max_research cannot be lower than the seeded field count")
    configured_shard_size = settings.shard_size
    persisted_sink = wrap_sink(sink, sessionmaker, run_id=resume_run_id)
    run_sink: Sink = persisted_sink
    cost_budget = RunCostBudget(max_cost_usd)

    llm = LlmClient()
    fetcher = Fetcher()
    cache = RetrievalCache()
    restarts = 0
    summary: str | None = None
    run_id: int | None = None
    beat: asyncio.Task | None = None
    fleet: OrchestratorFleet | None = None

    try:
        async with sessionmaker() as session:
            toolbox = Toolbox(session, fetcher, cache)
            runtime = AgentRuntime(session, llm, toolbox, max_steps, run_sink)
            runtime._cost_budget = cost_budget

            seeded = seed is not None
            if resume_run_id is None:
                effective_shard_size = (
                    configured_shard_size if shard_size is None else shard_size
                )
                if seeded:
                    query = f"Seeded run from {seed_filename or 'seed file'}"
                    if not seed:
                        raise ValueError("a seeded run requires at least one technology")
                run = await runtime.create_run(
                    query, research, top_n, shard_size=effective_shard_size
                )
                run.max_cost_usd = max_cost_usd
                _record_orchestrator_budget(run, settings.orchestrator_max_steps)
                run.seeded = seeded
                if seeded:
                    run.query = (
                        f"Seeded run from {seed_filename or 'seed file'}; "
                        "task: assess the given technologies"
                    )
                seeded_fields = await _create_seed_fields(session, run, seed or [])
            else:
                run = await _existing_run(session, resume_run_id)
                await _resume_cost_budget(session, run, max_cost_usd, cost_budget)
                _record_orchestrator_budget(run, settings.orchestrator_max_steps)
                query = run.query
                if seed is not None:
                    raise ValueError("seed data cannot be supplied when resuming a run")
                seeded = bool(getattr(run, "seeded", False))
                seeded_fields = []
                stored_research, stored_top = await _stored_run_limits(
                    session, run, settings.max_research, settings.top_n
                )
                research = stored_research if max_research is None else max_research
                top_n = stored_top if top is None else top
                effective_shard_size = _stored_shard_size(
                    run, shard_size, configured_shard_size
                )
                run.max_research = research
                run.top_n = top_n
                run.state = "running"
                run.finished_at = None
                if operator_message:
                    queue_message(session, run.id, operator_message)
            await session.commit()
            run_id = run.id
            if on_run_id is not None:
                on_run_id(run_id)
            emit(run_sink, "run_started", run_id=run_id, query=query)
            set_event_sink = getattr(llm, "set_event_sink", None)
            if set_event_sink is not None:
                set_event_sink(run_sink)
            probe_providers = getattr(llm, "probe_providers", None)
            if probe_providers is not None:
                await probe_providers()

            if resume_run_id is None:
                orchestrator = await runtime.create(
                    run_id=run_id, role="orchestrator", system=prompts["orchestrator"]
                )
            else:
                orchestrator = await _existing_orchestrator(session, run_id)
            runtime.operator_agent_id = orchestrator.id
            await session.commit()

            orchestration = Orchestration(
                session=session,
                sessionmaker=sessionmaker,
                runtime=runtime,
                llm=llm,
                fetcher=fetcher,
                run=run,
                prompts=prompts,
                budget_minutes=settings.run_budget_minutes,
                max_research=research,
                top_n=top_n,
                shard_size=effective_shard_size,
                cache=cache,
                sink=run_sink,
                cost_budget=cost_budget,
                owner_agent_id=orchestrator.id,
                initial_agent_id=orchestrator.id,
            )
            orchestration.install(toolbox)
            fleet = OrchestratorFleet(
                session=session,
                sessionmaker=sessionmaker,
                runtime=runtime,
                orchestration=orchestration,
                llm=llm,
                fetcher=fetcher,
                cache=cache,
                run=run,
                top_n=top_n,
                prompts=prompts,
                max_steps=run.orchestrator_max_steps,
                max_restarts=max_restarts,
                sink=run_sink,
            )

            beat = asyncio.create_task(
                _heartbeat(
                    fleet, run_sink, settings.heartbeat_s, sessionmaker, run_id,
                    write_s=settings.heartbeat_write_s,
                ),
                name="heartbeat",
            )
            restored: dict = {}
            if resume_run_id is not None and cost_budget.reason is None:
                restored = await fleet.restore(orchestrator)
                emit(run_sink, "run_restored", run_id=run_id, **restored)
            elif cost_budget.reason is not None:
                fleet._stop_reason = cost_budget.reason
                emit(run_sink, "cost_limit_reached", run_id=run_id, reason=cost_budget.reason)

            if resume_run_id is not None and seeded:
                seeded_fields = await _seeded_resume_fields(session, run_id)
            if resume_run_id is None:
                opening = (
                    _seeded_opening_message(
                        query, research, top_n, effective_shard_size, seeded_fields
                    )
                    if seeded
                    else _opening_message(query, research, top_n, effective_shard_size)
                )
            else:
                opening = (
                    _seeded_resume_message(
                        query, research, top_n, restored, effective_shard_size, seeded_fields
                    )
                    if seeded
                    else _resume_message(query, research, top_n, restored, effective_shard_size)
                )
            await fleet.start(orchestrator, opening)
            try:
                summary, restarts, stopped = await fleet.wait()
            finally:
                beat.cancel()
                await asyncio.gather(beat, return_exceptions=True)

            try:
                await fleet.shutdown()
            except Exception as exc:
                emit(run_sink, "tail_failed", stage="shutdown", error=str(exc)[:200])

            verification = {"checked": 0, "verified": 0, "failed": 0}
            try:
                emit(run_sink, "verifying")
                verification = await verify_run(session, run_id)
                emit(run_sink, "verified", **verification)
            except Exception as exc:
                await _rollback(session)
                emit(run_sink, "tail_failed", stage="verify", error=str(exc)[:200])

            state = _run_state(summary, stopped)
            try:
                await persisted_sink.flush()
                if fleet._stop_reason:
                    emit(
                        run_sink,
                        "run_stopped",
                        run_id=run_id,
                        reason=fleet._stop_reason,
                        state=run.state if stopped == "run_ended" else state,
                    )
                await persisted_sink.flush()
                if stopped != "run_ended":
                    run.state = state
                run.finished_at = datetime.now(UTC)
                await session.commit()
            except Exception as exc:
                await _rollback(session)
                emit(run_sink, "tail_failed", stage="finish", error=str(exc)[:200])

            await persisted_sink.flush()
            accounting: dict = {}
            agents: list[dict] = []
            try:
                accounting = await per_run(session, run_id)
                agents = await per_agent(session, run_id)
            except Exception as exc:
                await _rollback(session)
                emit(run_sink, "tail_failed", stage="accounting", error=str(exc)[:200])

            return RunOutcome(
                run_id=run_id,
                state=state,
                reason=getattr(fleet, "_stop_reason", None),
                summary=summary,
                top_n=top_n,
                max_research=research,
                restarts=restarts,
                verification=verification,
                models=llm.models_used,
                prompts=describe(),
                accounting=accounting,
                agents=agents,
                http_requests=fetcher.stats.requests,
            )
    except BaseException as exc:
        if beat is not None:
            beat.cancel()
            await asyncio.gather(beat, return_exceptions=True)
        if fleet is not None:
            try:
                await fleet.shutdown()
            except Exception as cleanup_exc:
                emit(run_sink, "tail_failed", stage="shutdown", error=str(cleanup_exc)[:200])
        if run_id is not None:
            reason = interruption_reason(exc)
            try:
                await mark_interrupted_run(sessionmaker, run_id, reason)
                emit(run_sink, "run_stopped", run_id=run_id, state="failed", reason=reason)
            except Exception as cleanup_exc:
                emit(run_sink, "tail_failed", stage="interrupted", error=str(cleanup_exc)[:200])
        raise
    finally:
        try:
            await persisted_sink.drain()
        finally:
            await shutdown_page_processor()
        for close in (llm.aclose, fetcher.aclose):
            try:
                await close()
            except Exception:
                pass

async def _seeded_resume_fields(session: AsyncSession, run_id: int) -> list[dict]:
    fields = (
        await session.scalars(
            select(Field)
            .where(
                Field.run_id == run_id,
                ~select(Entry.id)
                .where(Entry.run_id == run_id, Entry.field_id == Field.id)
                .exists(),
            )
            .order_by(Field.ordinal.nulls_last(), Field.id)
        )
    ).all()
    return [
        {
            "id": field.id,
            "focus": field.focus,
            "technology_id": field.technology_id,
            "metadata": metadata_from_rationale(getattr(field, "rationale", None)),
        }
        for field in fields
    ]


def _seed_technology_aliases(title: str) -> list[tuple[str, str]]:
    forms: list[tuple[str, str]] = []

    def add(value: str, lang: str) -> None:
        value = value.strip(" \t\n\r-–—:;,.)/")
        if value.count("(") > value.count(")"):
            value = value.rsplit("(", 1)[0].strip(" \t\n\r-–—:;,./")
        if value and (value, lang) not in forms:
            forms.append((value, lang))

    add(title.split(":", 1)[0], "ru")
    add(re.split(r"\s*:\s*|\s+-\s+|\s*[–—]\s*", title, maxsplit=1)[0], "ru")
    add(title.split("(", 1)[0], "ru")
    add(title, "ru")
    for parenthetical in re.findall(r"\(([^)]*)\)", title):
        if re.search(r"[A-Za-z]", parenthetical):
            add(parenthetical, "en")
    for match in re.finditer(r"[A-Za-z][A-Za-z0-9.+/#-]*(?:\s+[A-Za-z][A-Za-z0-9.+/#-]*)+", title):
        add(match.group(), "en")
    return forms


async def _create_seed_fields(
    session: AsyncSession, run: Run, seeds: list[dict]
) -> list[dict]:
    fields = []
    for ordinal, item in enumerate(seeds, start=1):
        label = item["n"]
        focus = str(item["tech"]).strip()
        area = str(item["domain"]).strip()
        technology = await session.scalar(
            select(Technology)
            .where(Technology.canonical_name_ru == focus)
            .limit(1)
        )
        if technology is None:
            technology = Technology(canonical_name_ru=focus)
            session.add(technology)
            await session.flush()
            technology_id = technology.id
        else:
            technology_id = technology.id
        aliases = _seed_technology_aliases(focus)
        for surface_form, lang in aliases:
            existing_alias = await session.scalar(
                select(Alias).where(
                    func.lower(Alias.surface_form) == surface_form.lower(),
                    Alias.lang == lang,
                )
            )
            if existing_alias is None:
                session.add(
                    Alias(
                        technology_id=technology_id,
                        surface_form=surface_form,
                        lang=lang,
                    )
                )
        metadata = seed_metadata(item)
        field = Field(
            run_id=run.id,
            area=area,
            focus=focus,
            rationale=seed_rationale(label, metadata),
            technology_id=technology_id,
            ordinal=ordinal,
            state="pending",
        )
        session.add(field)
        await session.flush()
        fields.append(
            {
                "id": field.id,
                "focus": field.focus,
                "technology_id": field.technology_id,
                "metadata": metadata,
            }
        )
    return fields


def _seeded_opening_message(
    query: str, research: int, top_n: int, shard_size: int, fields: list[dict]
) -> str:
    listed = "\n".join(
        f"- field_id={field['id']} technology_id={field['technology_id']}: {field['focus']}"
        + (
            f"\n  {supplied_claim(field.get('metadata') or {})}"
            if field.get("metadata")
            else ""
        )
        for field in fields
    )
    shard_instruction = (
        "Call summon_orchestrators with every listed field first, then each shard "
        "must dispatch its fields."
        if len(fields) > shard_size
        else "Dispatch every listed field with dispatch_researchers."
    )
    return (
        f"Направление: {query}\n\n"
        "Задача этого запуска: оценить каждую из заданных технологий ниже, "
        "не искать и не добавлять другие.\n"
        f"Поля для исследования ({len(fields)}):\n{listed}\n\n"
        f"Предел исследования: {research}; размер шарда: {shard_size}. {shard_instruction} "
        "Не вызывай split_direction или open_technology: список технологий замкнут, "
        "поля уже созданы и добавлять технологии нельзя. "
        "После каждого возврата создай refute; дождись и собери все ответы, затем "
        "напиши запись для каждого поля. "
        "Записывай каждое, включая state noise и insufficient. Не оставляй ни "
        "одного поля без записи. "
        f"Показаны будут лучшие {top_n}."
    )


def _seeded_resume_message(
    query: str, research: int, top_n: int, restored: dict, shard_size: int, fields: list[dict]
) -> str:
    message = _resume_message(query, research, top_n, restored, shard_size)
    listed = "\n".join(
        f"- field_id={field['id']} technology_id={field['technology_id']}: {field['focus']}"
        + (
            f"\n  {supplied_claim(field.get('metadata') or {})}"
            if field.get("metadata")
            else ""
        )
        for field in fields
    )
    return (
        message
        + "\n\nЭтот запуск seeded: продолжай только поля ниже.\n"
        + listed
        + "\nНе вызывай split_direction или open_technology. Направь все перечисленные "
        "поля без записей в работу, refute каждый возврат и запиши "
        "каждое, включая noise и insufficient."
    )


def _record_orchestrator_budget(run: Run, current: int) -> int:
    budget = getattr(run, "orchestrator_max_steps", None)
    if budget is None:
        run.orchestrator_max_steps = current
    return run.orchestrator_max_steps


def _stored_shard_size(run: Run, argument: int | None, default: int) -> int:
    value = argument if argument is not None else getattr(run, "shard_size", None)
    value = default if value is None else value
    run.shard_size = value
    return value


def _run_state(summary: str | None, stopped: str) -> str:
    if stopped == "supervisor_gave_up":
        return "failed"
    if stopped == "run_ended":
        return "failed"
    if stopped in ("call_budget_exhausted", "cost_limit_reached"):
        return "exhausted"
    if stopped == "answered" and summary is not None:
        return "finished"
    return "exhausted"


async def _existing_run(session: AsyncSession, run_id: int) -> Run:
    run = await session.get(Run, run_id)
    if run is None:
        raise ValueError(f"no run {run_id} to resume")
    return run


async def _resume_cost_budget(
    session: AsyncSession,
    run: Run,
    override: float | None,
    budget: RunCostBudget,
) -> None:
    if override is not None:
        run.max_cost_usd = override
    budget.limit_usd = getattr(run, "max_cost_usd", None)
    budget.spent_usd = float(
        await session.scalar(
            select(func.coalesce(func.sum(Turn.cost_usd), 0.0))
            .join(Agent, Turn.agent_id == Agent.id)
            .where(Agent.run_id == run.id)
        )
        or 0.0
    )


_RESEARCH_LIMIT = re.compile(r"исследовать не более\s+(\d+)\s+технолог", re.IGNORECASE)
_TOP_LIMIT = re.compile(r"Показаны будут лучшие\s+(\d+)", re.IGNORECASE)


def _limits_from_opening_message(message: str) -> tuple[int | None, int | None]:
    research_match = _RESEARCH_LIMIT.search(message)
    top_match = _TOP_LIMIT.search(message)
    return (
        int(research_match.group(1)) if research_match else None,
        int(top_match.group(1)) if top_match else None,
    )


async def _stored_run_limits(
    session: AsyncSession, run: Run, default_research: int, default_top: int
) -> tuple[int, int]:

    research = run.max_research
    top = run.top_n
    if research is not None and top is not None:
        return research, top

    opening = await session.scalar(
        select(Turn.content)
        .join(Agent, Turn.agent_id == Agent.id)
        .where(
            Agent.run_id == run.id,
            Agent.role == "orchestrator",
            Turn.kind == "user",
        )
        .order_by(Turn.seq)
        .limit(1)
    )
    text = str(opening.get("content") or "") if isinstance(opening, dict) else ""
    prompt_research, prompt_top = _limits_from_opening_message(text)

    if research is None:
        if prompt_research is not None:
            research = prompt_research
        else:
            consumed = await session.scalar(
                select(func.count()).select_from(Field).where(
                    Field.run_id == run.id,
                    Field.state.in_(("dispatched", "done")),
                )
            )
            research = int(consumed or 0) or default_research
    if top is None:
        top = prompt_top or default_top
    return research, top


async def _existing_orchestrator(session: AsyncSession, run_id: int) -> Agent:
    orchestrator = await session.scalar(
        select(Agent)
        .where(
            Agent.run_id == run_id,
            Agent.role == "orchestrator",
            Agent.parent_agent_id.is_(None),
        )
        .order_by(Agent.id)
        .limit(1)
    )
    if orchestrator is None:
        raise ValueError(f"run {run_id} has no orchestrator to resume")
    return orchestrator


def _opening_message(
    query: str,
    research: int,
    top_n: int,
    shard_size: int = 10,
) -> str:
    return (
        f"Направление: {query}\n\n"
        f"Задача этого запуска: исследовать не более {research} технологий. "
        f"Предельный размер шарда оркестратора: {shard_size} полей. "
        "Ассистент может выдать сколько угодно полей — это задел, они все "
        "останутся в базе. Ты решаешь, какие именно взять в работу, и это "
        "решение главное в запуске. Предел жёсткий: диспетчер откажет сверх него.\n\n"
        f"Критики и ответы на них в этот предел НЕ входят: атакуй всё, что стоит "
        "защищать. Записывай всё исследованное, включая шум и слишком тонкие "
        f"случаи; показаны будут лучшие {top_n}, остальные останутся ниже черты "
        "как демонстрация логики исключения."
    )


def _resume_message(
    query: str,
    research: int,
    top_n: int,
    restored: dict,
    shard_size: int = 10,
) -> str:
    replayed = int(restored.get("returns_replayed") or 0)
    written = int(restored.get("entries_already_written") or 0)
    researched = int(restored.get("fields_counted_against_the_ceiling") or 0)
    empty = restored.get("agents_with_nothing_to_replay") or []
    lines = [
        f"Этот запуск был прерван и продолжается. Направление то же: {query}. "
        "Твоя история цела — посмотри, что уже сделано.",
        f"Возвратов исследователей ждёт в очереди: {replayed}. "
        "Это работа, за которую уже заплачено и которая нигде не записана: "
        "начни с collect, потом решай.",
        f"Записей уже написано: {written}. "
        f"Технологий взято в работу: {researched} из {research}. "
        f"Показаны будут лучшие {top_n}. "
        f"Размер шарда оркестратора: {shard_size} полей.",
    ]
    if empty:
        lines.append(
            "Агенты, от которых не осталось ничего (оборвались до ответа): "
            + ", ".join(str(agent_id) for agent_id in empty)
            + ". Их поля можно отправить заново, но это стоит места в пределе."
        )
    return "\n\n".join(lines)


async def _heartbeat(
    orchestration: Any,
    sink: Sink | None,
    every_s: float,
    sessionmaker: async_sessionmaker[AsyncSession],
    run_id: int,
    write_s: float | None = None,
) -> None:
    started = time.monotonic()
    tick = min(every_s, write_s) if write_s else every_s
    emitted: float | None = None
    while True:
        async with sessionmaker() as session:
            await session.execute(
                update(Run).where(Run.id == run_id).values(heartbeat_at=func.now())
            )
            await session.commit()
        now = time.monotonic()
        if emitted is None or now - emitted >= every_s:
            emit(
                sink,
                "heartbeat",
                elapsed_s=round(now - started),
                inflight=orchestration.inflight,
            )
            emitted = now
        await asyncio.sleep(tick)


async def _rollback(session: AsyncSession) -> None:
    try:
        await session.rollback()
    except Exception:
        pass
