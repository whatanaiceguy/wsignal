from time import perf_counter
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from wsignal.corpus import Corpus, CorpusUnavailable, entry_term, get_corpus
from wsignal.db import get_session
from wsignal.inference.scoring import entry_ordering
from wsignal.models import Entry, Run

router = APIRouter(tags=["corpus"])
CorpusDep = Annotated[Corpus, Depends(get_corpus)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
Months = Annotated[int, Query(ge=1, le=120)]


class Month(BaseModel):
    m: str
    n: int
    total: int


class Series(BaseModel):
    q: str
    months: list[Month]
    matched_total: int
    too_broad: bool
    elapsed_ms: float


class EntrySeries(BaseModel):
    entry_id: int
    entry_rank: int
    source: Literal["agent", "derived"]
    term: str
    months: list[Month]
    matched_total: int
    too_broad: bool


async def _series(corpus: Corpus, terms: list[str], months: int) -> list[dict]:
    try:
        return await corpus.series(terms, months)
    except CorpusUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/api/corpus/series", response_model=Series)
async def corpus_series(
    corpus: CorpusDep,
    q: Annotated[str, Query(min_length=1, max_length=500)],
    months: Months = 36,
) -> Series:
    started = perf_counter()
    q = q.strip()
    if not q:
        raise HTTPException(status_code=422, detail="Укажите поисковый запрос")
    series = await _series(corpus, [q], months)
    return Series(q=q, **series[0], elapsed_ms=round((perf_counter() - started) * 1000, 2))


@router.get("/api/search/{run_id}/corpus", response_model=list[EntrySeries])
async def run_corpus(
    run_id: int, session: SessionDep, corpus: CorpusDep, months: Months = 36,
) -> list[EntrySeries]:
    if await session.get(Run, run_id) is None:
        raise HTTPException(status_code=404, detail="Запуск не найден")
    entries = (await session.scalars(
        select(Entry).where(Entry.run_id == run_id).order_by(
            *entry_ordering(Entry.weak_score, Entry.score, Entry.id)
        )
    )).all()
    queries = [(entry.corpus_query or "").strip() for entry in entries]
    terms = [
        query or entry_term(entry.name_en, entry.name_ru)
        for query, entry in zip(queries, entries, strict=True)
    ]
    series = await _series(corpus, terms, months)
    return [
        EntrySeries(
            entry_id=entry.id, entry_rank=rank, term=term,
            source="agent" if queries[rank - 1] else "derived", **points,
        )
        for rank, (entry, term, points) in enumerate(
            zip(entries, terms, series, strict=True), start=1
        )
    ]
