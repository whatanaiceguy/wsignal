from __future__ import annotations

import json
from datetime import date, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, StringConstraints
from pydantic import Field as PydanticField
from sqlalchemy import Text, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from wsignal.db import get_session
from wsignal.inference.accounting import context_sources, per_agent, per_run, per_source
from wsignal.inference.operator_messages import queue_message
from wsignal.inference.scoring import entry_ordering
from wsignal.interface.jobs import RunAlreadyActive, runner
from wsignal.interface.report import _models, field_counts, unbacked_patterns
from wsignal.interface.run_history import is_live as _is_live
from wsignal.interface.run_history import run_history
from wsignal.models import (
    Agent,
    Citation,
    Document,
    Entry,
    EntryPattern,
    FieldRename,
    HarnessNote,
    Refutation,
    Run,
    RunEvent,
    SourceEvent,
    Turn,
)

router = APIRouter(prefix="/api/dev", tags=["dev"])
SessionDep = Annotated[AsyncSession, Depends(get_session)]


OperatorText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20000)
]


class MessageRequest(BaseModel):
    text: OperatorText


class ResumeRequest(BaseModel):
    message: OperatorText | None = None
    max_cost_usd: float | None = PydanticField(default=None, ge=0)


@router.post("/runs/{run_id}/message", status_code=202)
async def send_message(run_id: int, body: MessageRequest, session: SessionDep) -> dict[str, int]:
    run = await session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if not _is_live(run.state, run.heartbeat_at):
        raise HTTPException(status_code=409, detail="Run is not live; resume it with a message")
    event = queue_message(session, run_id, body.text)
    await session.commit()
    return {"run_id": run_id, "message_id": event.id}


@router.post("/runs/{run_id}/resume", status_code=202)
async def resume_run(
    run_id: int, session: SessionDep, body: ResumeRequest | None = None,
) -> dict[str, int]:
    run = await session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if _is_live(run.state, run.heartbeat_at):
        raise HTTPException(status_code=409, detail="Run is still live")
    try:
        await runner.start(
            "", resume_run_id=run_id,
            max_cost_usd=body.max_cost_usd if body else None,
            operator_message=body.message if body else None,
        )
    except RunAlreadyActive:
        raise HTTPException(status_code=409, detail="Another run is already active") from None
    return {"run_id": run_id}


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class HarnessNoteOut(ORMModel):
    id: int
    run_id: int | None
    agent_id: int | None
    role: str | None
    model: str | None
    kind: str
    tool: str | None
    text: str
    meta: dict
    created_at: datetime


@router.get("/notes", response_model=list[HarnessNoteOut])
async def harness_notes(
    session: SessionDep,
    run_id: int | None = None,
    kind: str | None = None,
    tool: str | None = None,
    limit: Annotated[int, Query(ge=1, le=2000)] = 500,
) -> list[HarnessNoteOut]:
    query = select(HarnessNote).order_by(HarnessNote.id.desc()).limit(limit)
    if run_id is not None:
        query = query.where(HarnessNote.run_id == run_id)
    if kind:
        query = query.where(HarnessNote.kind == kind)
    if tool:
        query = query.where(HarnessNote.tool == tool)
    return [HarnessNoteOut.model_validate(row) for row in await session.scalars(query)]


class Counts(BaseModel):
    agents: int
    entries: int
    turns: int
    documents: int
    citations: int
    cost_usd: float


class RunSummary(ORMModel):
    id: int
    query: str
    direction_ru: str | None
    direction_en: str | None
    max_research: int | None
    top_n: int | None
    state: str
    started_at: datetime
    finished_at: datetime | None
    heartbeat_at: datetime | None
    is_live: bool
    elapsed_s: float
    counts: Counts


class RunRow(ORMModel):
    id: int
    query: str
    direction_ru: str | None
    direction_en: str | None
    max_research: int | None
    top_n: int | None
    state: str
    started_at: datetime
    finished_at: datetime | None
    heartbeat_at: datetime | None
    is_live: bool


class Meta(BaseModel):
    accounting: dict[str, Any]
    agents: list[dict[str, Any]]
    context_sources: list[dict[str, Any]]
    per_source: list[dict[str, Any]]
    field_counts: tuple[int, int]
    unbacked_patterns: int
    models_used: dict[str, str]


class RunDetail(BaseModel):
    run: RunRow
    meta: Meta
    stop_reason: str | None = None


class AgentMetrics(BaseModel):
    turns: int
    model_calls: int
    model_call_budget: int | None
    cost_usd: float
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    wall_time_s: float
    error_turns: int


class AgentOut(ORMModel):
    id: int
    parent_agent_id: int | None
    role: str
    model: str
    field: str | None
    brief: str | None
    state: str
    created_at: datetime
    last_called_at: datetime | None
    returned_at: datetime | None
    metrics: AgentMetrics
    run_id: int | None = None
    query: str | None = None
    is_live: bool


class DocumentOut(ORMModel):
    id: int
    url: str
    title: str | None
    source_name: str
    source_type: str
    source_lang: str
    source_tier: int
    published_at: date | None
    adapter: str | None
    retrieved: bool


class CitationOut(ORMModel):
    id: int
    entry_id: int
    document_id: int
    quote: str
    summary_ru: str | None
    verified: bool
    verified_at: datetime | None
    document: DocumentOut


class EntryPatternOut(ORMModel):
    id: int
    entry_id: int
    kind: str
    pattern: str
    strength: float | None
    citation_id: int | None


class EntryOut(ORMModel):
    edits: list[dict[str, Any]] = []
    id: int
    run_id: int
    field_id: int | None
    researched_by_agent_id: int | None
    written_by_agent_id: int | None
    technology_id: int | None
    name_ru: str
    name_en: str | None
    corpus_query: str | None = None
    topic_original_focus: str | None = None
    topic_rename_history: list[dict[str, Any]] = []
    transition_ru: str
    state: str
    score: float
    weak_score: float | None = None
    signal_class: str | None = None
    substance: float | None
    momentum: float | None
    faintness: float | None
    why_ru: str
    current_state_ru: str | None
    dynamics_ru: str | None
    what_would_refute_ru: str | None
    searches_run: int
    sources_checked: int
    problem_ru: str | None
    advantage_ru: str | None
    case_example_ru: str | None
    refuter_agent_id: int | None
    rebutted: bool
    created_at: datetime
    citations: list[CitationOut]
    entry_patterns: list[EntryPatternOut]


class RefutationOut(ORMModel):
    id: int
    run_id: int
    researcher_agent_id: int
    refuter_agent_id: int
    rebuttal_required: bool
    state: str
    outcome: dict | None
    collected: bool
    delivery_version: int
    created_at: datetime
    completed_at: datetime | None


class RunEntries(BaseModel):
    entries: list[EntryOut]
    refutations: list[RefutationOut]


class SourceEventOut(ORMModel):
    id: int
    ts: datetime
    adapter: str
    host: str | None
    query: str | None
    via: str | None
    ok: bool
    items: int | None
    error: str | None
    duration_ms: int | None
    bytes: int | None
    requests: int | None
    cached: bool
    agent_id: int | None


class TurnOut(ORMModel):
    id: int
    agent_id: int
    seq: int
    kind: str
    content: dict
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_tokens: int | None
    cache_write_tokens: int | None
    reasoning_tokens: int | None
    cost_usd: float | None
    duration_ms: int | None
    attempts: int | None
    provider: str | None
    generation_id: str | None
    created_at: datetime


class AgentTurns(BaseModel):
    agent: AgentOut
    turns: list[TurnOut]


class SearchHit(BaseModel):
    id: int
    seq: int
    kind: str
    agent_id: int
    role: str
    run_id: int
    created_at: datetime
    snippet: str


ErrorKind = Literal[
    "source", "agent", "model", "tool", "refutation", "run", "fetch", "citation"
]


class RunError(BaseModel):
    id: str
    kind: ErrorKind
    created_at: datetime
    agent_id: int | None = None
    role: str | None = None
    title: str
    message: str
    turn_id: int | None = None
    turn_seq: int | None = None
    source_event_id: int | None = None
    refutation_id: int | None = None
    run_event_id: int | None = None
    document_id: int | None = None
    citation_id: int | None = None
    entry_id: int | None = None


class RunErrors(BaseModel):
    errors: list[RunError]
    counts: dict[str, int]
    total: int


def _agent_metrics(row: Any) -> AgentMetrics:
    return AgentMetrics(
        turns=row.turns,
        model_calls=row.model_calls,
        model_call_budget=None,
        cost_usd=float(row.cost_usd or 0),
        prompt_tokens=row.prompt_tokens or 0,
        completion_tokens=row.completion_tokens or 0,
        cached_tokens=row.cached_tokens or 0,
        wall_time_s=round((row.wall_ms or 0) / 1000, 3),
        error_turns=row.error_turns,
    )


async def _metrics(session: AsyncSession, agent_ids: list[int]) -> dict[int, AgentMetrics]:
    if not agent_ids:
        return {}
    rows = (
        await session.execute(
            select(
                Turn.agent_id,
                func.count(Turn.id).label("turns"),
                func.count(Turn.id).filter(
                    Turn.kind == "assistant", Turn.cost_usd.is_not(None)
                ).label("model_calls"),
                func.coalesce(func.sum(Turn.cost_usd), 0.0).label("cost_usd"),
                func.coalesce(func.sum(Turn.prompt_tokens), 0).label("prompt_tokens"),
                func.coalesce(func.sum(Turn.completion_tokens), 0).label("completion_tokens"),
                func.coalesce(func.sum(Turn.cached_tokens), 0).label("cached_tokens"),
                func.coalesce(func.sum(Turn.duration_ms), 0).label("wall_ms"),
                func.count(Turn.id).filter(Turn.content["error"].as_string().is_not(None)).label(
                    "error_turns"
                ),
            )
            .where(Turn.agent_id.in_(agent_ids))
            .group_by(Turn.agent_id)
        )
    ).all()
    return {row.agent_id: _agent_metrics(row) for row in rows}


async def _agent_out(
    session: AsyncSession, agents: list[Agent], include_run: bool = False
) -> list[AgentOut]:
    metrics = await _metrics(session, [agent.id for agent in agents])
    run_liveness = {}
    if agents:
        run_liveness = {
            run_id: _is_live(state, heartbeat_at)
            for run_id, state, heartbeat_at in (
                await session.execute(
                    select(Run.id, Run.state, Run.heartbeat_at).where(
                        Run.id.in_({agent.run_id for agent in agents})
                    )
                )
            ).all()
        }
    run_budgets = {}
    if agents:
        run_budgets = {
            run_id: budget
            for run_id, budget in (
                await session.execute(
                    select(Run.id, Run.orchestrator_max_steps).where(
                        Run.id.in_({agent.run_id for agent in agents})
                    )
                )
            ).all()
        }
    queries: dict[int, str] = {}
    if include_run and agents:
        queries = {
            run_id: query
            for run_id, query in (
                await session.execute(
                    select(Run.id, Run.query).where(Run.id.in_({agent.run_id for agent in agents}))
                )
            ).all()
        }
    return [
        AgentOut(
            id=agent.id,
            parent_agent_id=agent.parent_agent_id,
            role=agent.role,
            model=agent.model,
            field=agent.field,
            brief=agent.brief,
            state=agent.state,
            created_at=agent.created_at,
            last_called_at=agent.last_called_at,
            returned_at=agent.returned_at,
            metrics=AgentMetrics.model_validate(
                {
                    **metrics.get(
                        agent.id,
                        AgentMetrics(
                            turns=0,
                            model_calls=0,
                            model_call_budget=None,
                            cost_usd=0,
                            prompt_tokens=0,
                            completion_tokens=0,
                            cached_tokens=0,
                            wall_time_s=0,
                            error_turns=0,
                        ),
                    ).model_dump(),
                    "model_call_budget": (
                        run_budgets.get(agent.run_id)
                        if agent.role == "orchestrator"
                        else None
                    ),
                }
            ),
            run_id=agent.run_id if include_run else None,
            query=queries.get(agent.run_id) if include_run else None,
            is_live=run_liveness.get(agent.run_id, False),
        )
        for agent in agents
    ]


@router.get("/runs", response_model=list[RunSummary])
async def runs(session: SessionDep) -> list[RunSummary]:
    return [RunSummary.model_validate(row) for row in await run_history(session)]


@router.get("/runs/{run_id}", response_model=RunDetail)
async def run_detail(run_id: int, session: SessionDep) -> RunDetail:
    run = await session.get(Run, run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    return RunDetail(
        run=RunRow.model_validate(
            {**run.__dict__, "is_live": _is_live(run.state, run.heartbeat_at)}
        ),
        meta=Meta(
            accounting=await per_run(session, run_id),
            agents=await per_agent(session, run_id),
            context_sources=await context_sources(session, run_id),
            per_source=await per_source(session, run_id),
            field_counts=await field_counts(session, run_id),
            unbacked_patterns=await unbacked_patterns(session, run_id),
            models_used=await _models(session, run_id),
        ),
        stop_reason=await session.scalar(
            select(RunEvent.payload["reason"].as_string())
            .where(RunEvent.run_id == run_id, RunEvent.kind == "run_stopped")
            .order_by(RunEvent.id.desc())
            .limit(1)
        ),
    )


async def _require_run(session: AsyncSession, run_id: int) -> None:
    if await session.get(Run, run_id) is None:
        raise HTTPException(404, "run not found")


@router.get("/runs/{run_id}/agents", response_model=list[AgentOut])
async def run_agents(run_id: int, session: SessionDep) -> list[AgentOut]:
    await _require_run(session, run_id)
    agents = (
        await session.scalars(select(Agent).where(Agent.run_id == run_id).order_by(Agent.id))
    ).all()
    return await _agent_out(session, agents)


@router.get("/runs/{run_id}/entries", response_model=RunEntries)
async def run_entries(run_id: int, session: SessionDep) -> RunEntries:
    await _require_run(session, run_id)
    entries = (
        await session.scalars(
            select(Entry).where(Entry.run_id == run_id).order_by(
                *entry_ordering(Entry.weak_score, Entry.score, Entry.id)
            )
        )
    ).all()
    from wsignal.interface.report import entry_edits_for

    entry_ids = [entry.id for entry in entries]
    edits = await entry_edits_for(session, entry_ids)
    field_ids = {entry.field_id for entry in entries if entry.field_id is not None}
    renames_by_field: dict[int, list[FieldRename]] = {}
    if field_ids:
        for rename in await session.scalars(
            select(FieldRename).where(FieldRename.field_id.in_(field_ids))
            .order_by(FieldRename.created_at, FieldRename.id)
        ):
            renames_by_field.setdefault(rename.field_id, []).append(rename)
    citations: dict[int, list[CitationOut]] = {}
    patterns: dict[int, list[EntryPatternOut]] = {}
    if entry_ids:
        citation_rows = (
            await session.execute(
                select(Citation, Document).join(Document, Document.id == Citation.document_id)
                .where(Citation.entry_id.in_(entry_ids)).order_by(Citation.id)
            )
        ).all()
        for citation, document in citation_rows:
            citations.setdefault(citation.entry_id, []).append(
                CitationOut.model_validate({**citation.__dict__, "document": document})
            )
        for pattern in await session.scalars(
            select(EntryPattern).where(EntryPattern.entry_id.in_(entry_ids)).order_by(EntryPattern.id)
        ):
            patterns.setdefault(pattern.entry_id, []).append(
                EntryPatternOut.model_validate(pattern)
            )
    output = []
    for entry in entries:
        output.append(
            EntryOut.model_validate(
                {
                    **entry.__dict__,
                    "topic_original_focus": (
                        renames_by_field[entry.field_id][0].previous_focus
                        if entry.field_id in renames_by_field
                        else None
                    ),
                    "topic_rename_history": [
                        {
                            "previous_focus": rename.previous_focus,
                            "new_focus": rename.new_focus,
                            "reason": rename.reason,
                            "verdict_on_previous": rename.verdict_on_previous,
                            "created_at": rename.created_at,
                        }
                        for rename in renames_by_field.get(entry.field_id, [])
                    ],
                    "edits": edits.get(entry.id, []),
                    "citations": citations.get(entry.id, []),
                    "entry_patterns": patterns.get(entry.id, []),
                }
            )
        )
    refutations = (
        await session.scalars(
            select(Refutation).where(Refutation.run_id == run_id).order_by(Refutation.id)
        )
    ).all()
    return RunEntries(
        entries=output,
        refutations=[RefutationOut.model_validate(row) for row in refutations],
    )


@router.get("/runs/{run_id}/turns", response_model=list[TurnOut])
async def run_turns(
    run_id: int, session: SessionDep, since_id: int | None = None
) -> list[TurnOut]:
    await _require_run(session, run_id)
    stmt = (
        select(Turn)
        .join(Agent, Agent.id == Turn.agent_id)
        .where(Agent.run_id == run_id)
        .order_by(Turn.created_at, Turn.id)
    )
    if since_id is not None:
        stmt = stmt.where(Turn.id > since_id)
    return [TurnOut.model_validate(turn) for turn in (await session.scalars(stmt)).all()]


@router.get("/runs/{run_id}/source-events", response_model=list[SourceEventOut])
async def run_source_events(
    run_id: int, session: SessionDep, limit: int = Query(2000, ge=1, le=5000)
) -> list[SourceEventOut]:
    await _require_run(session, run_id)
    rows = (
        await session.scalars(
            select(SourceEvent).join(Agent, Agent.id == SourceEvent.agent_id)
            .where(Agent.run_id == run_id)
            .order_by(SourceEvent.ts.desc(), SourceEvent.id.desc())
            .limit(limit)
        )
    ).all()
    return [SourceEventOut.model_validate(row) for row in rows]


@router.get("/runs/{run_id}/errors", response_model=RunErrors)
async def run_errors(
    run_id: int,
    session: SessionDep,
    kind: Annotated[list[ErrorKind] | None, Query()] = None,
    role: str | None = None,
    agent_id: int | None = None,
    q: str | None = None,
    limit: Annotated[int, Query(ge=1, le=5000)] = 500,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RunErrors:
    run = await session.get(Run, run_id)
    if run is None:
        raise HTTPException(404, "run not found")

    agents = (
        await session.scalars(select(Agent).where(Agent.run_id == run_id))
    ).all()
    agents_by_id = {agent.id: agent for agent in agents}
    agent_ids = list(agents_by_id)
    turns = (
        await session.scalars(select(Turn).where(Turn.agent_id.in_(agent_ids)))
    ).all() if agent_ids else []
    sources = (
        await session.scalars(
            select(SourceEvent).where(SourceEvent.agent_id.in_(agent_ids))
        )
    ).all() if agent_ids else []
    refutations = (
        await session.scalars(select(Refutation).where(Refutation.run_id == run_id))
    ).all()
    events = (
        await session.scalars(select(RunEvent).where(RunEvent.run_id == run_id))
    ).all()
    documents = (
        await session.scalars(
            select(Document).where(Document.fetched_by_agent_id.in_(agent_ids))
        )
    ).all() if agent_ids else []
    citation_rows = (
        await session.execute(
            select(Citation, Entry)
            .join(Entry, Entry.id == Citation.entry_id)
            .where(Entry.run_id == run_id)
        )
    ).all()

    errors: list[dict[str, Any]] = []

    def add(
        error_id: str,
        error_kind: ErrorKind,
        created_at: datetime,
        title: str,
        message: str,
        *,
        agent: Agent | None = None,
        **ids: Any,
    ) -> None:
        errors.append({
            "id": error_id,
            "kind": error_kind,
            "created_at": created_at,
            "agent_id": agent.id if agent is not None else None,
            "role": agent.role if agent is not None else None,
            "title": title,
            "message": message,
            "_sort_id": int(error_id.rsplit(":", 1)[-1]),
            **ids,
        })

    def message_text(value: Any) -> str:
        if isinstance(value, dict):
            for key in ("error", "reason", "message"):
                if value.get(key):
                    return str(value[key])
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        return str(value) if value is not None else ""

    failed_turn_messages: dict[int, str] = {}
    for turn in turns:
        content = turn.content or {}
        if turn.kind == "assistant" and (content.get("error") or content.get("reason")):
            parts = [str(content[key]) for key in ("reason", "error") if content.get(key)]
            message = ": ".join(parts)
            agent = agents_by_id.get(turn.agent_id)
            add(
                f"model:{turn.id}", "model", turn.created_at,
                agent.model if agent else "model", message, agent=agent,
                turn_id=turn.id, turn_seq=turn.seq,
            )
            failed_turn_messages[turn.agent_id] = message
        if turn.kind == "tool_result":
            payload = content.get("payload") or {}
            if isinstance(payload, dict) and payload.get("error"):
                agent = agents_by_id.get(turn.agent_id)
                add(
                    f"tool:{turn.id}", "tool", turn.created_at,
                    str(content.get("name") or "tool"), message_text(payload["error"]),
                    agent=agent, turn_id=turn.id, turn_seq=turn.seq,
                )

    for agent in agents:
        if agent.state == "failed":
            add(
                f"agent:{agent.id}", "agent", agent.returned_at or agent.created_at,
                agent.role, failed_turn_messages.get(agent.id, "Agent state is failed"),
                agent=agent,
            )

    for source in sources:
        if not source.ok:
            agent = agents_by_id.get(source.agent_id)
            add(
                f"source:{source.id}", "source", source.ts, source.adapter,
                source.error or "Source request failed", agent=agent,
                source_event_id=source.id,
            )

    for refutation in refutations:
        if refutation.state in {"failed", "cancelled", "interrupted"}:
            agent = agents_by_id.get(refutation.refuter_agent_id)
            add(
                f"refutation:{refutation.id}", "refutation",
                refutation.completed_at or refutation.created_at,
                f"refutation #{refutation.id}",
                message_text(refutation.outcome) or f"Refutation {refutation.state}",
                agent=agent, refutation_id=refutation.id,
            )

    for event in events:
        payload = event.payload or {}
        carries_error = isinstance(payload, dict) and bool(payload.get("error"))
        if carries_error or event.kind in {"run_stopped", "agent_failed"}:
            event_agent_id = payload.get("agent_id") if isinstance(payload, dict) else None
            agent = agents_by_id.get(event_agent_id)
            error_kind: ErrorKind = (
                "agent" if event.kind == "agent_failed"
                else "model" if event.kind == "first_token_timeout"
                else "run"
            )
            event_message = message_text(payload.get("error") or payload.get("reason"))
            add(
                f"run_event:{event.id}", error_kind, event.ts, event.kind,
                event_message or f"{event.kind} event", agent=agent,
                run_event_id=event.id,
            )

    if run.state == "failed":
        add(
            f"run:{run.id}", "run", run.finished_at or run.heartbeat_at or run.started_at,
            "run failed", "Run state is failed",
        )

    for document in documents:
        if document.source_type == "fetch_failure":
            agent = agents_by_id.get(document.fetched_by_agent_id)
            add(
                f"fetch:{document.id}", "fetch", document.fetched_at,
                document.title or document.url, document.content,
                agent=agent, document_id=document.id,
            )

    for citation, entry in citation_rows:
        if not citation.verified:
            agent = agents_by_id.get(entry.written_by_agent_id)
            add(
                f"citation:{citation.id}", "citation",
                citation.verified_at or entry.created_at,
                f"citation #{citation.id}", "Citation verification failed",
                agent=agent, citation_id=citation.id, entry_id=entry.id,
            )

    kinds = ("source", "agent", "model", "tool", "refutation", "run", "fetch", "citation")
    kind_counts = {item: sum(error["kind"] == item for error in errors) for item in kinds}
    filtered = errors
    if kind:
        selected_kinds = set(kind)
        filtered = [error for error in filtered if error["kind"] in selected_kinds]
    if role is not None:
        filtered = [error for error in filtered if error["role"] == role]
    if agent_id is not None:
        filtered = [error for error in filtered if error["agent_id"] == agent_id]
    if q:
        needle = q.casefold()
        filtered = [error for error in filtered if needle in error["message"].casefold()]
    filtered.sort(key=lambda error: (error["created_at"], error["_sort_id"]), reverse=True)
    total = len(filtered)
    page = filtered[offset : offset + limit]
    return RunErrors(
        errors=[
            RunError.model_validate(
                {key: value for key, value in error.items() if key != "_sort_id"}
            )
            for error in page
        ],
        counts=kind_counts,
        total=total,
    )


@router.get("/agents", response_model=list[AgentOut])
async def all_agents(
    session: SessionDep,
    run_id: int | None = None,
    role: str | None = None,
    state: str | None = None,
    model: str | None = None,
    has_error: bool | None = None,
    q: str | None = None,
    limit: int = Query(200, ge=1),
    offset: int = Query(0, ge=0),
) -> list[AgentOut]:
    stmt = select(Agent).order_by(Agent.created_at.desc(), Agent.id.desc())
    if run_id is not None:
        stmt = stmt.where(Agent.run_id == run_id)
    if role is not None:
        stmt = stmt.where(Agent.role == role)
    if state is not None:
        stmt = stmt.where(Agent.state == state)
    if model is not None:
        stmt = stmt.where(Agent.model == model)
    if q is not None:
        pattern = f"%{q}%"
        stmt = stmt.where(
            func.lower(Agent.field).like(func.lower(pattern))
            | func.lower(Agent.brief).like(func.lower(pattern))
        )
    if has_error is not None:
        error_agents = select(Turn.agent_id).where(
            Turn.content["error"].as_string().is_not(None)
            | (
                (Turn.kind == "tool_result")
                & Turn.content["payload"]["error"].as_string().is_not(None)
            )
        )
        stmt = stmt.where(
            Agent.id.in_(error_agents) if has_error else Agent.id.not_in(error_agents)
        )
    agents = (await session.scalars(stmt.offset(offset).limit(limit))).all()
    return await _agent_out(session, agents, include_run=True)


@router.get("/agents/{agent_id}/turns", response_model=AgentTurns)
async def agent_turns(
    agent_id: int, session: SessionDep, since_seq: int | None = None
) -> AgentTurns:
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(404, "agent not found")
    turns_stmt = select(Turn).where(Turn.agent_id == agent_id).order_by(Turn.seq)
    if since_seq is not None:
        turns_stmt = turns_stmt.where(Turn.seq > since_seq)
    turns = (await session.scalars(turns_stmt)).all()
    return AgentTurns(
        agent=(await _agent_out(session, [agent]))[0],
        turns=[TurnOut.model_validate(turn) for turn in turns],
    )


@router.get("/search", response_model=list[SearchHit])
async def debug_search(
    session: SessionDep, q: str = Query(..., min_length=1), run_id: int | None = None,
    kind: str | None = None, limit: int = Query(200, ge=1),
) -> list[SearchHit]:
    stmt = (
        select(Turn, Agent, Run)
        .join(Agent, Agent.id == Turn.agent_id)
        .join(Run, Run.id == Agent.run_id)
        .where(cast(Turn.content, Text).ilike(f"%{q}%"))
        .order_by(Turn.created_at.desc(), Turn.id.desc()).limit(limit)
    )
    if run_id is not None:
        stmt = stmt.where(Run.id == run_id)
    if kind is not None:
        stmt = stmt.where(Turn.kind == kind)
    hits = []
    for turn, agent, run in (await session.execute(stmt)).all():
        content = json.dumps(turn.content, ensure_ascii=False, separators=(",", ":"))
        match = content.casefold().find(q.casefold())
        start = max(0, match - 120)
        snippet = content[start : start + 240]
        hits.append(
            SearchHit(
                id=turn.id,
                seq=turn.seq,
                kind=turn.kind,
                agent_id=agent.id,
                role=agent.role,
                run_id=run.id,
                created_at=turn.created_at,
                snippet=snippet,
            )
        )
    return hits
