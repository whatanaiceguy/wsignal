from __future__ import annotations

import textwrap
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from wsignal.config import get_settings
from wsignal.inference.accounting import context_sources, per_agent, per_run, per_source
from wsignal.inference.scoring import entry_ordering
from wsignal.interface.schemas import Citation as CitationOut
from wsignal.interface.schemas import Entry as EntryOut
from wsignal.interface.schemas import Pattern as PatternOut
from wsignal.interface.schemas import Refutation as RefutationOut
from wsignal.interface.schemas import SearchResponse, SearchStats
from wsignal.models import (
    Agent,
    Citation,
    Document,
    Entry,
    EntryEdit,
    EntryPattern,
    Field,
    FieldRename,
    Refutation,
    Run,
)
from wsignal.parsing.base import tier_name

WIDTH = 78
RULE = "=" * WIDTH
THIN = "-" * WIDTH


async def entry_edits_for(session: AsyncSession, entry_ids: list[int]) -> dict[int, list[dict]]:
    edits: dict[int, list[dict]] = {}
    if entry_ids:
        for edit in await session.scalars(
            select(EntryEdit).where(EntryEdit.entry_id.in_(entry_ids))
            .order_by(EntryEdit.created_at, EntryEdit.id)
        ):
            edits.setdefault(edit.entry_id, []).append({
                "created_at": edit.created_at,
                "reason": edit.reason,
                "changed": list(edit.changes),
            })
    return edits


async def build_response(session: AsyncSession, run_id: int) -> SearchResponse:

    run = await session.get(Run, run_id)
    if run is None:
        raise LookupError(f"нет запуска {run_id}")

    entries = (
        await session.scalars(
            select(Entry).where(Entry.run_id == run_id).order_by(
                *entry_ordering(Entry.weak_score, Entry.score, Entry.id)
            )
        )
    ).all()
    ids = [e.id for e in entries]
    edits = await entry_edits_for(session, ids)
    field_ids = {entry.field_id for entry in entries if entry.field_id is not None}
    renames_by_field: dict[int, list[FieldRename]] = {}
    if field_ids:
        for rename in await session.scalars(
            select(FieldRename).where(FieldRename.field_id.in_(field_ids))
            .order_by(FieldRename.created_at, FieldRename.id)
        ):
            renames_by_field.setdefault(rename.field_id, []).append(rename)

    cited: dict[int, list[tuple[Citation, Document]]] = {}
    if ids:
        rows = (
            await session.execute(
                select(Citation, Document)
                .join(Document, Document.id == Citation.document_id)
                .where(Citation.entry_id.in_(ids))
                .order_by(Citation.entry_id, Citation.id)
            )
        ).all()
        for citation, document in rows:
            cited.setdefault(citation.entry_id, []).append((citation, document))

    marks: dict[int, list[EntryPattern]] = {}
    if ids:
        for mark in await session.scalars(
            select(EntryPattern).where(EntryPattern.entry_id.in_(ids)).order_by(EntryPattern.id)
        ):
            marks.setdefault(mark.entry_id, []).append(mark)

    refuter_ids = {entry.refuter_agent_id for entry in entries if entry.refuter_agent_id}
    refutations = {}
    if refuter_ids:
        for refutation in await session.scalars(
            select(Refutation).where(
                Refutation.run_id == run_id, Refutation.refuter_agent_id.in_(refuter_ids)
            ).order_by(Refutation.id)
        ):
            refutations[refutation.refuter_agent_id] = refutation

    out: list[EntryOut] = []
    for rank, entry in enumerate(entries, start=1):
        pairs = cited.get(entry.id, [])
        at = {citation.id: i for i, (citation, _) in enumerate(pairs)}
        out.append(
            EntryOut(
                id=entry.id,
                edits=edits.get(entry.id, []),
                rank=rank,
                refutation=_refutation(entry, refutations.get(entry.refuter_agent_id)),
                name_ru=entry.name_ru,
                name_en=entry.name_en,
                corpus_query=entry.corpus_query,
                forced_reason=getattr(entry, "forced_reason", None),
                topic_original_focus=(
                    renames_by_field[entry.field_id][0].previous_focus
                    if entry.field_id in renames_by_field
                    else None
                ),
                topic_rename_history=[
                    {
                        "previous_focus": rename.previous_focus,
                        "new_focus": rename.new_focus,
                        "reason": rename.reason,
                        "verdict_on_previous": rename.verdict_on_previous,
                        "created_at": rename.created_at,
                    }
                    for rename in renames_by_field.get(entry.field_id, [])
                ],
                transition_ru=entry.transition_ru,
                score=entry.score,
                weak_score=getattr(entry, "weak_score", None),
                signal_class=getattr(entry, "signal_class", None),
                substance=getattr(entry, "substance", None),
                momentum=getattr(entry, "momentum", None),
                faintness=getattr(entry, "faintness", None),
                state=entry.state,
                why_ru=entry.why_ru,
                current_state_ru=entry.current_state_ru,
                dynamics_ru=entry.dynamics_ru,
                what_would_refute_ru=entry.what_would_refute_ru,
                searches_run=entry.searches_run,
                sources_checked=entry.sources_checked,
                problem_ru=entry.problem_ru,
                advantage_ru=entry.advantage_ru,
                case_example_ru=entry.case_example_ru,
                patterns=[
                    PatternOut(
                        kind=mark.kind,
                        pattern=mark.pattern,
                        citation_index=at[mark.citation_id],
                        strength=getattr(mark, "strength", None),
                    )
                    for mark in marks.get(entry.id, [])
                    if mark.citation_id in at
                ],
                citations=[_citation(c, d) for c, d in pairs],
            )
        )

    return SearchResponse(
        run_id=run.id,
        state=run.state,
        query=run.query,
        generated_at=datetime.now(UTC),
        stats=await _stats(session, run, out),
        top_n=getattr(run, "top_n", None) or getattr(get_settings(), "top_n", None),
        entries=out,
        models_used=await _models(session, run_id),
    )


def _refutation(entry: Entry, refutation: Refutation | None) -> RefutationOut | None:
    if refutation is None:
        return None
    outcome = refutation.outcome or {}
    attack = outcome.get("attack")
    rebuttal = outcome.get("rebuttal")
    attack = attack if isinstance(attack, dict) else {}
    rebuttal = rebuttal if isinstance(rebuttal, dict) else {}
    return RefutationOut(
        state=refutation.state,
        rebutted=bool(entry.rebutted),
        claims=attack.get("claims") or [],
        responses=rebuttal.get("rebuttal_responses") or [],
        queries_and_venues=attack.get("queries_and_venues"),
    )


def _citation(citation: Citation, document: Document) -> CitationOut:
    return CitationOut(
        url=document.url,
        quote=citation.quote,
        verified=citation.verified,
        name=document.source_name,
        published_at=document.published_at,
        type=document.source_type,
        lang=document.source_lang,
        tier=tier_name(document.source_tier),
        title_original=document.title if document.source_lang != "ru" else None,
        summary_ru=citation.summary_ru,
        summary_is_generated=bool(citation.summary_ru and citation.summary_ru.strip()),
    )


SIGNAL_FLOOR = 0.5
LIKELY_FROM = 0.65
VERY_HIGH_FROM = 0.9
TZ_CONFIDENCE = 0.75
EXCLUDED_CLASSES = ("strong", "noise")


def signal_bands(scores: list[float]) -> dict[str, int]:
    return {
        "high_confidence_count": sum(1 for s in scores if s > TZ_CONFIDENCE),
        "signals_count": sum(1 for s in scores if s >= SIGNAL_FLOOR),
        "probable_count": sum(1 for s in scores if SIGNAL_FLOOR <= s < LIKELY_FROM),
        "likely_count": sum(1 for s in scores if LIKELY_FROM <= s < VERY_HIGH_FROM),
        "very_high_count": sum(1 for s in scores if s >= VERY_HIGH_FROM),
    }


def band_scores(entries: list[EntryOut]) -> list[float]:
    return [e.score for e in entries if e.signal_class not in EXCLUDED_CLASSES]


async def _stats(session: AsyncSession, run: Run, entries: list[EntryOut]) -> SearchStats:
    scores = band_scores(entries)
    quotes = [c for e in entries for c in e.citations]

    processed = (
        await session.scalar(
            select(func.count(func.distinct(Document.id)))
            .join(Agent, Agent.id == Document.fetched_by_agent_id)
            .where(Agent.run_id == run.id)
        )
    ) or 0

    agents = (
        await session.scalar(select(func.count(Agent.id)).where(Agent.run_id == run.id))
    ) or 0

    finished = run.finished_at or datetime.now(UTC)
    elapsed = (finished - run.started_at).total_seconds() if run.started_at else 0.0

    return SearchStats(
        candidates_found=len(entries),
        sources_processed=processed,
        confidence_threshold=TZ_CONFIDENCE,
        **signal_bands(scores),
        agents_spawned=agents,
        citations_verified=sum(1 for c in quotes if c.verified),
        citations_total=len(quotes),
        elapsed_seconds=round(elapsed, 1),
    )


async def _models(session: AsyncSession, run_id: int) -> dict[str, str]:

    rows = await session.execute(
        select(Agent.role, Agent.model).where(Agent.run_id == run_id).distinct()
    )
    return {role: model for role, model in rows}


async def unbacked_patterns(session: AsyncSession, run_id: int) -> int:
    return (
        await session.scalar(
            select(func.count(EntryPattern.id))
            .join(Entry, Entry.id == EntryPattern.entry_id)
            .where(Entry.run_id == run_id, EntryPattern.citation_id.is_(None))
        )
    ) or 0


async def field_counts(session: AsyncSession, run_id: int) -> tuple[int, int]:
    total = (
        await session.scalar(select(func.count(Field.id)).where(Field.run_id == run_id))
    ) or 0
    pending = (
        await session.scalar(
            select(func.count(Field.id)).where(Field.run_id == run_id, Field.state == "pending")
        )
    ) or 0
    return total, pending


def _wrap(text: str, indent: str = "    ") -> str:
    out = []
    for para in (text or "").strip().split("\n"):
        para = para.strip()
        if not para:
            out.append("")
            continue
        out.append(
            textwrap.fill(
                para,
                width=WIDTH,
                initial_indent=indent,
                subsequent_indent=indent,
                break_long_words=False,
                break_on_hyphens=False,
            )
        )
    return "\n".join(out)


def _field(label: str, value: str | None) -> list[str]:
    if not value or not value.strip():
        return []
    return [f"    {label}", _wrap(value, indent="      "), ""]


def render(response: SearchResponse, top: int, run_id: int | None = None) -> str:
    stats = response.stats
    lines = [
        RULE,
        f"ЗАПРОС   {response.query}",
        RULE,
        "",
        f"  сформировано   {response.generated_at:%Y-%m-%d %H:%M} UTC"
        + (f"   ·   запуск {run_id}" if run_id is not None else ""),
        f"  проработано    {stats.candidates_found} кандидатов, "
        f"слабых сигналов (от 0.50) {stats.signals_count}: вероятно {stats.probable_count}, "
        f"скорее всего {stats.likely_count}, очень высокая уверенность {stats.very_high_count}",
        f"  уверенность   более {stats.confidence_threshold:.0%} — {stats.high_confidence_count}",
        f"  источников     {stats.sources_processed} загружено за запуск",
        f"  цитат          {stats.citations_total}, подтверждено дословно "
        f"{stats.citations_verified}",
        f"  время          {stats.elapsed_seconds:.0f} с",
        f"  модели         {response.models_used}",
        "",
    ]

    if not response.entries:
        lines += ["  Ни одного кандидата не записано.", ""]
        return "\n".join(lines)

    for entry in response.entries:
        if entry.rank == top + 1:
            lines += [
                "",
                THIN,
                f"НИЖЕ ТОП-{top}. Это и есть демонстрация логики исключения: "
                "записано всё",
                "проработанное, включая шум, и у каждого сказано, что именно "
                "не подтвердилось.",
                THIN,
                "",
            ]
        lines += _entry_block(entry)

    return "\n".join(lines)


def _entry_block(entry: EntryOut) -> list[str]:
    weak_score = f"[{entry.weak_score:.3f}]" if entry.weak_score is not None else "[—]"
    signal_class = f"[{entry.signal_class}]" if entry.signal_class is not None else "[—]"
    head = (
        f"{entry.rank:>3}. {weak_score} [{entry.score:.2f}] "
        f"{signal_class} [{entry.state}]  {entry.name_ru}"
    )
    lines = [THIN, head]
    if entry.name_en:
        lines.append(f"     {entry.name_en}")
    if entry.forced_reason:
        lines.append(_wrap(f"НЕПРОВЕРЕНО: запись сделана без исследования. {entry.forced_reason}"))
    if entry.topic_original_focus:
        lines.append(f"    Исходная тема: {entry.topic_original_focus}")
        for rename in entry.topic_rename_history:
            lines.append(
                f"    Переименование: {rename['previous_focus']} → {rename['new_focus']}"
            )
            lines.append(_wrap(f"Причина: {rename['reason']}"))
            lines.append(_wrap(f"Вердикт по прежнему названию: {rename['verdict_on_previous']}"))
    lines += ["", _wrap(entry.why_ru), ""]
    lines += _field(
        "Измерения (вещество / импульс / слабость сигнала):",
        f"{entry.substance} / {entry.momentum} / {entry.faintness}"
        if entry.substance is not None
        or entry.momentum is not None
        or entry.faintness is not None
        else None,
    )

    lines += _field("Что опровергло бы:", entry.what_would_refute_ru)
    lines += _field("Текущее состояние:", entry.current_state_ru)
    lines += _field("Динамика:", entry.dynamics_ru)
    lines += _field("Какую проблему решает:", entry.problem_ru)
    lines += _field("Потенциальное преимущество:", entry.advantage_ru)
    lines += _field("Кейс:", entry.case_example_ru)

    if entry.patterns:
        marks = ", ".join(
            f"{p.kind}:{p.pattern} strength={p.strength} [{p.citation_index + 1}]"
            for p in entry.patterns
        )
        lines += [_wrap(f"Признаки: {marks}"), ""]

    lines.append(
        f"    Поиск: {entry.searches_run} запросов по "
        f"{entry.sources_checked} площадкам."
    )

    if not entry.citations:
        lines += ["    Источники: ни одного.", ""]
        return lines

    ok = sum(1 for c in entry.citations if c.verified)
    lines.append(f"    Источники: {len(entry.citations)}, подтверждено {ok}.")
    lines.append("")
    for i, c in enumerate(entry.citations, start=1):
        date = c.published_at.isoformat() if c.published_at else "дата неизвестна"
        mark = "подтв" if c.verified else "НЕ НАЙДЕНА"
        lines.append(f"    [{i}] {c.name} · {c.type} · {c.lang} · {c.tier} · {date}  [{mark}]")
        if c.title_original:
            lines.append(_wrap(c.title_original, indent="        "))
        lines.append(f"        {c.url}")
        lines.append(_wrap(f"«{c.quote}»", indent="        "))
        if c.summary_ru:
            lines.append(_wrap(c.summary_ru, indent="        "))
        lines.append("")
    return lines


def render_meta(
    *,
    accounting: dict,
    agents: list[dict],
    context: list[dict],
    sources: list[dict],
    run_state: str | None = None,
    restarts: int = 0,
    prompts: dict | None = None,
    http_requests: int | None = None,
    fields: tuple[int, int] | None = None,
    unbacked: int = 0,
) -> str:

    acc = accounting or {}
    lines = ["", RULE, "СЛУЖЕБНОЕ", RULE, ""]

    if run_state:
        lines.append(f"  состояние запуска  {run_state}")
    if restarts:
        lines.append(f"  рестартов          {restarts}")
    if fields:
        total, pending = fields
        lines.append(
            f"  поля               {total} всего, {pending} осталось в pending "
            "— это задел, а не потеря"
        )
    if unbacked:
        lines.append(
            f"  признаков без цитаты {unbacked} — в ответ не попали, "
            "признак без документа ничего не значит"
        )
    if prompts:
        lines.append(f"  промпты            {({k: v['sha256'] for k, v in prompts.items()})}")

    lines += ["", THIN, "счёт за запуск"]
    prompt = acc.get("prompt_tokens", 0)
    completion = acc.get("completion_tokens", 0)
    lines.append(
        f"  агентов {acc.get('agents', 0)}, вызовов модели {acc.get('model_calls', 0)}, "
        f"инструментов {acc.get('tool_calls', 0)}"
        + (f", http {http_requests}" if http_requests is not None else "")
    )
    lines.append(
        f"  ВХОД  {prompt:>12,}  из них из кэша {acc.get('cached_tokens', 0):>12,} "
        f"({acc.get('cache_hit_pct', 0)}%), свежих {acc.get('uncached_tokens', 0):>12,}"
    )
    lines.append(
        f"  ВЫХОД {completion:>12,}  из них reasoning {acc.get('reasoning_tokens', 0):>12,}"
    )
    if acc.get("cache_writes"):
        lines.append(f"  запись в кэш {acc['cache_writes']:>10,}")
    if completion:
        lines.append(f"  на один токен ответа пришлось {prompt / completion:.0f} токенов входа")
    lines.append(f"  время: модель {acc.get('model_s', 0)}s, инструменты {acc.get('tool_s', 0)}s")
    lines.append(f"  СТОИМОСТЬ ЗАПУСКА  ${acc.get('cost_usd', 0.0):.4f}")
    for role, bucket in sorted(acc.get("by_role", {}).items()):
        lines.append(
            f"    {role:14} {bucket['agents']:>2} агентов  "
            f"{bucket['model_calls']:>3} вызовов  "
            f"кэш {bucket['cache_hit_pct']:>5}%  ${bucket['cost_usd']:.4f}"
        )

    if sources:
        lines += ["", THIN, "по источникам (source_events)"]
        lines.append(
            f"    {'адаптер':<12} {'вызовов':>7} {'ok':>5} {'пусто':>6} {'сбоев':>6} "
            f"{'записей':>8} {'запросов':>9} {'МБ':>7} {'сред.':>6}"
        )
        for s in sources:
            lines.append(
                f"    {s['adapter']:<12} {s['calls']:>7} {s['ok']:>5} {s['empty']:>6} "
                f"{s['failed']:>6} {s['items']:>8} {s['requests']:>9} "
                f"{s['bytes'] / 1_048_576:>7.1f} {s['avg_s']:>5.1f}s"
            )
        lines.append(
            "    «пусто» — успешный вызов, вернувший ноль. Отказ, прикинувшийся "
            "успехом,"
        )
        lines.append("    выглядит именно так, а на нуле здесь строятся утверждения об отсутствии.")

    if agents:
        lines += ["", THIN, "по агентам"]
        for a in agents:
            field = (a["field"] or "")[:34]
            lines.append(
                f"    #{a['agent_id']:<4} {a['role']:<13} {a['state']:<7} "
                f"вызовов {a['model_calls']:>3}/{a['tool_calls']:<3} "
                f"кэш {a['cache_hit_pct']:>5}%  "
                f"{a['model_s']:>6}s+{a['tool_s']:<6}s  ${a['cost_usd']:.4f}  {field}"
            )

    if context:
        lines += ["", THIN, "что занесло в контексты больше всего (один возврат инструмента)"]
        for row in context:
            lines.append(
                f"    #{row['agent_id']:<4} {row['role']:<13} шаг {row['seq']:<3} "
                f"{row['tool'] or '?':<10} {row['chars']:>9,} символов "
                f"(~{row['chars'] // 4:,} токенов)"
            )

    lines.append("")
    return "\n".join(lines)


async def assemble(
    session: AsyncSession,
    run_id: int,
    top: int | None = None,
    full: bool = False,
) -> tuple[SearchResponse, str]:

    settings = get_settings()
    top = settings.top_n if top is None else top

    response = await build_response(session, run_id)
    text = render(response, top, run_id=run_id)
    if full:
        run = await session.get(Run, run_id)
        text += render_meta(
            accounting=await per_run(session, run_id),
            agents=await per_agent(session, run_id),
            context=await context_sources(session, run_id),
            sources=await per_source(session, run_id),
            run_state=run.state if run else None,
            fields=await field_counts(session, run_id),
            unbacked=await unbacked_patterns(session, run_id),
        )
    return response, text


def save(
    run_id: int,
    text: str,
    response: SearchResponse | None = None,
    out_dir: str | Path | None = None,
    suffix: str = "",
) -> list[Path]:

    directory = Path(out_dir or get_settings().report_dir)
    directory.mkdir(parents=True, exist_ok=True)
    written = [directory / f"{run_id}{suffix}.txt"]
    written[0].write_text(text, encoding="utf-8")
    if response is not None:
        path = directory / f"{run_id}.json"
        path.write_text(
            response.model_dump_json(indent=2, exclude_none=False), encoding="utf-8"
        )
        written.append(path)
    return written
