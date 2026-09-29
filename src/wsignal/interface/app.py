import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from wsignal import __version__
from wsignal.config import Settings, get_settings
from wsignal.db import get_session, get_sessionmaker
from wsignal.interface.corpus_api import router as corpus_router
from wsignal.interface.dev_api import router as dev_router
from wsignal.interface.jobs import RunAlreadyActive, iter_events, runner
from wsignal.interface.report import build_response
from wsignal.interface.run_history import is_live, run_history
from wsignal.interface.schemas import RunSummary, SearchResponse
from wsignal.models import Agent, Document, Entry, Run, RunEvent
from wsignal.routes import log_routes, routes

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    log_routes(settings)
    reaper = None
    if not settings.resume_on_startup:
        reaper = asyncio.create_task(
            runner.reap_dead_runs(
                max(settings.dead_run_wait_s, 3 * settings.heartbeat_write_s)
            ),
            name="dead-run-reaper",
        )
        reaper.add_done_callback(_log_reaper_failure)
    await runner.resume_orphan()
    try:
        yield
    finally:
        if reaper is not None and not reaper.done():
            reaper.cancel()
            await asyncio.gather(reaper, return_exceptions=True)


def _log_reaper_failure(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception() is not None:
        logger.error("Dead-run reaper failed", exc_info=task.exception())


app = FastAPI(
    lifespan=lifespan,
    title="wsignal",
    version=__version__,
    description=(
        "Сервис автоматизированного сбора и анализа зарождающихся "
        "технологических трендов (слабых сигналов) по открытым источникам"
    ),
)

app.include_router(dev_router)
app.include_router(corpus_router)

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


class Health(BaseModel):
    status: str
    version: str
    database: str
    pg_trgm: str | None
    agents: int
    documents: int
    entries: int
    routes: dict[str, str]


@app.get("/health", response_model=Health, tags=["service"])
async def health(session: SessionDep) -> Health:

    pg_trgm_version = (
        await session.execute(
            text("SELECT extversion FROM pg_extension WHERE extname = 'pg_trgm'")
        )
    ).scalar_one_or_none()

    agents = (await session.execute(select(func.count()).select_from(Agent))).scalar_one()
    documents = (
        await session.execute(select(func.count()).select_from(Document))
    ).scalar_one()
    entries = (await session.execute(select(func.count()).select_from(Entry))).scalar_one()

    return Health(
        status="ok",
        version=__version__,
        database="ok",
        pg_trgm=pg_trgm_version,
        agents=agents,
        documents=documents,
        entries=entries,
        routes=routes(),
    )


@app.get("/api/runs", response_model=list[RunSummary], tags=["search"])
async def runs(session: SessionDep) -> list[RunSummary]:
    return [RunSummary.model_validate(row) for row in await run_history(session)]


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query must not be blank")
        return value
    top: int | None = Field(default=None, ge=1)
    max_research: int | None = Field(default=None, ge=1)
    shard_size: int | None = Field(default=None, ge=1)
    max_cost_usd: float | None = Field(default=None, ge=0)


@app.post("/api/search", status_code=status.HTTP_202_ACCEPTED, tags=["search"])
async def search(body: SearchRequest) -> dict[str, int]:
    try:
        run_id = await runner.start(
            body.query,
            top=body.top,
            max_research=body.max_research,
            shard_size=body.shard_size,
            max_cost_usd=body.max_cost_usd,
        )
    except RunAlreadyActive:
        raise HTTPException(
            status_code=409, detail="Другое исследование уже выполняется"
        ) from None
    return {"run_id": run_id}


class ResumeRequest(BaseModel):
    max_cost_usd: float | None = Field(default=None, ge=0)


@app.post(
    "/api/search/{run_id}/resume", status_code=status.HTTP_202_ACCEPTED, tags=["search"]
)
async def resume_search(run_id: int, body: ResumeRequest | None = None) -> dict[str, int]:
    async with get_sessionmaker()() as session:
        run = await session.get(Run, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Исследование не найдено")
        if run.state == "finished":
            raise HTTPException(status_code=409, detail="Исследование уже завершено")
        if is_live(run.state, run.heartbeat_at):
            raise HTTPException(status_code=409, detail="Исследование ещё выполняется")
    try:
        await runner.start(
            "", resume_run_id=run_id, max_cost_usd=body.max_cost_usd if body else None,
        )
    except RunAlreadyActive:
        raise HTTPException(
            status_code=409, detail="Другое исследование уже выполняется"
        ) from None
    return {"run_id": run_id}


@app.post("/api/search/{run_id}/stop", tags=["search"])
async def stop_search(run_id: int) -> dict[str, int | str | None]:
    async with get_sessionmaker()() as session:
        if await session.get(Run, run_id) is None:
            raise HTTPException(status_code=404, detail="Исследование не найдено")
    if not await runner.stop(run_id):
        raise HTTPException(
            status_code=409, detail="Это исследование сейчас не выполняется на этом сервере"
        )
    async with get_sessionmaker()() as session:
        run = await session.get(Run, run_id)
        return {"run_id": run_id, "state": run.state if run is not None else None}


@app.get("/api/search/{run_id}/stop-reason", tags=["search"])
async def search_stop_reason(run_id: int) -> dict[str, str | None]:
    async with get_sessionmaker()() as session:
        run = await session.get(Run, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Исследование не найдено")
        reason = await session.scalar(
            select(RunEvent.payload["reason"].as_string())
            .where(RunEvent.run_id == run_id, RunEvent.kind == "run_stopped")
            .order_by(RunEvent.id.desc())
            .limit(1)
        )
        return {"state": run.state, "reason": reason}


@app.get("/api/search/{run_id}", response_model=SearchResponse, tags=["search"])
async def search_result(run_id: int) -> SearchResponse:
    async with get_sessionmaker()() as session:
        try:
            return await build_response(session, run_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/search/{run_id}/events", tags=["search"])
async def search_events(
    run_id: int,
    request: Request,
    after: int = Query(default=0, ge=0),
) -> StreamingResponse:
    header = request.headers.get("last-event-id")
    cursor = int(header) if header and header.isdigit() else after
    async with get_sessionmaker()() as session:
        if await session.get(Run, run_id) is None:
            raise HTTPException(status_code=404, detail="Исследование не найдено")

    async def stream():
        async for event in iter_events(get_sessionmaker(), run_id, cursor):
            yield event

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


API_PREFIXES = ("api/", "health", "docs", "redoc", "openapi.json")


def mount_web(target: FastAPI, dist: Path) -> bool:
    index = dist / "index.html"
    if not index.is_file():
        return False
    root = dist.resolve()
    if (dist / "assets").is_dir():
        target.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @target.get("/{path:path}", include_in_schema=False)
    async def web(path: str) -> FileResponse:
        if path == "api" or path.startswith(API_PREFIXES):
            raise HTTPException(status_code=404, detail="Not Found")
        candidate = (root / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(root):
            return FileResponse(candidate)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    return True


mount_web(app, Path(get_settings().web_dist))
