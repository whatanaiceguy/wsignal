from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from wsignal.inference.orchestration import Orchestration
from wsignal.interface.report import _citation as citation_out
from wsignal.models import Agent, Citation, Field, Technology


class _Rows:
    def __init__(self, values=()):
        self.values = values

    def all(self):
        return self.values


class _Result:
    def all(self):
        return []


class _WriteSession:
    def __init__(self, language):
        self.writer = SimpleNamespace(id=20, run_id=9, role="orchestrator")
        self.field = SimpleNamespace(
            id=1,
            run_id=9,
            agent_id=30,
            technology_id=40,
            orchestrator_agent_id=None,
            state="dispatched",
        )
        self.researcher = SimpleNamespace(
            id=30, run_id=9, role="researcher", parent_agent_id=20, state="idle"
        )
        self.technology = SimpleNamespace(id=40)
        self.document = SimpleNamespace(
            id=50,
            url=f"https://source.test/{language}",
            source_lang=language,
            title="Source title",
            source_name="Source",
            source_type="page",
            source_tier=2,
            published_at=None,
            fetched_at=datetime.now(UTC),
        )
        self.added = []
        self.commits = 0
        self.next_id = 100

    async def get(self, model, identity):
        if model is Agent and identity == 20:
            return self.writer
        if model is Agent and identity == 30:
            return self.researcher
        if model is Field and identity == 1:
            return self.field
        if model is Technology and identity == 40:
            return self.technology
        return None

    async def scalar(self, statement):
        if "documents" in str(statement):
            return self.document
        return None

    async def scalars(self, statement):
        if "refutations" in str(statement):
            return _Rows()
        return _Rows()

    async def execute(self, _statement):
        return _Result()

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        for row in self.added:
            if getattr(row, "id", None) is None:
                row.id = self.next_id
                self.next_id += 1

    async def commit(self):
        self.commits += 1


def _arguments(url, summary=None):
    citation = {"url": url, "quote": "verbatim quote"}
    if summary is not None:
        citation["summary_ru"] = summary
    return {
        "field_id": 1,
        "researched_by_agent_id": 30,
        "technology_id": 40,
        "name_ru": "Название",
        "transition_ru": "Переход",
        "corpus_query": "photonic processors",
        "state": "noise",
        "substance": 0.2,
        "momentum": 0.2,
        "faintness": 0.8,
        "score": 0.99,
        "why_ru": "Причина",
        "current_state_ru": "Состояние",
        "dynamics_ru": "Динамика",
        "what_would_refute_ru": "Опровержение",
        "problem_ru": "Проблема",
        "advantage_ru": "Преимущество",
        "case_example_ru": "Пример",
        "review_incomplete_reason": "Проверка не завершена",
        "citations": [citation],
    }


def _orchestration(session):
    orchestration = object.__new__(Orchestration)
    orchestration._session = session
    orchestration._run = SimpleNamespace(id=9)
    orchestration._partition_worker = False
    orchestration._inflight = {}
    orchestration._sink = None
    return orchestration


@pytest.mark.asyncio
async def test_foreign_citation_without_summary_is_refused_without_writes(monkeypatch):
    async def effort_for(_session, _agent_ids):
        return {"searches_run": 0, "sources_checked": 0}

    monkeypatch.setattr("wsignal.inference.orchestration.effort_for", effort_for)
    session = _WriteSession("en")
    result = await _orchestration(session)._write_entry(
        _arguments(session.document.url), 20
    )

    assert result["urls"] == [session.document.url]
    assert session.document.url in result["error"]
    assert "summary_ru" in result["error"]
    assert session.added == []
    assert session.commits == 0


@pytest.mark.asyncio
async def test_foreign_citation_summary_is_stored(monkeypatch):
    async def effort_for(_session, _agent_ids):
        return {"searches_run": 0, "sources_checked": 0}

    monkeypatch.setattr("wsignal.inference.orchestration.effort_for", effort_for)
    session = _WriteSession("en")
    summary = "Источник описывает систему. В нём приведены данные по утверждению."
    result = await _orchestration(session)._write_entry(
        _arguments(session.document.url, summary), 20
    )

    stored = next(row for row in session.added if isinstance(row, Citation))
    assert result["citations_stored"] == 1
    assert stored.summary_ru == summary
    assert session.commits == 1


@pytest.mark.asyncio
async def test_russian_citation_without_summary_is_accepted(monkeypatch):
    async def effort_for(_session, _agent_ids):
        return {"searches_run": 0, "sources_checked": 0}

    monkeypatch.setattr("wsignal.inference.orchestration.effort_for", effort_for)
    session = _WriteSession("ru")
    result = await _orchestration(session)._write_entry(
        _arguments(session.document.url), 20
    )

    stored = next(row for row in session.added if isinstance(row, Citation))
    assert result["citations_stored"] == 1
    assert stored.summary_ru is None
    assert session.commits == 1


def test_report_maps_original_title_and_generated_summary_by_language():
    english_document = SimpleNamespace(
        url="https://source.test/en",
        source_lang="en",
        title="English source title",
        source_name="Source",
        source_type="page",
        source_tier=2,
        published_at=None,
    )
    russian_document = SimpleNamespace(
        **{
            **english_document.__dict__,
            "url": "https://source.test/ru",
            "source_lang": "ru",
        }
    )
    english = citation_out(
        SimpleNamespace(quote="quote", verified=True, summary_ru="Краткое изложение"),
        english_document,
    )
    russian = citation_out(
        SimpleNamespace(quote="quote", verified=True, summary_ru=None),
        russian_document,
    )
    whitespace = citation_out(
        SimpleNamespace(quote="quote", verified=True, summary_ru="  \n"),
        english_document,
    )

    assert english.title_original == "English source title"
    assert english.summary_ru == "Краткое изложение"
    assert english.summary_is_generated is True
    assert russian.title_original is None
    assert russian.summary_ru is None
    assert russian.summary_is_generated is False
    assert whitespace.summary_is_generated is False
