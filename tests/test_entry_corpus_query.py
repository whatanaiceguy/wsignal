from types import SimpleNamespace

import pytest

from tests.test_citation_summary import _arguments, _orchestration, _WriteSession
from wsignal.corpus import validate_corpus_query
from wsignal.inference.orchestration import ORCHESTRATOR_TOOLS
from wsignal.inference.prompts import load_prompts
from wsignal.interface.corpus_api import run_corpus
from wsignal.models import Entry


def test_new_entry_schema_and_prompt_require_a_short_technology_query():
    schema = next(
        tool["function"]["parameters"] for tool in ORCHESTRATOR_TOOLS
        if tool["function"]["name"] == "write_entry"
    )
    assert "corpus_query" in schema["required"]
    assert schema["properties"]["corpus_query"]["maxLength"] == 200
    assert Entry.__table__.c.corpus_query.nullable is True
    prompt = load_prompts()["orchestrator"]
    assert "Every `write_entry` call must include `corpus_query`" in prompt
    assert "MCP security OR tool poisoning" in prompt


@pytest.mark.parametrize("query", [
    "CPO", '"optical circuit switching" OR OCS', "HBM -graphics",
    "MCP security OR tool poisoning", '"AND OR gates"',
])
def test_specific_websearch_queries_are_accepted(query):
    assert validate_corpus_query(query) == query


@pytest.mark.asyncio
@pytest.mark.parametrize("query", [
    None, "", " ", "AI", "photonic processors OR AI", "API OR KV", "-optics",
    "CPO OR", "OR CPO", "CPO OR HBM OR OCS OR SSD OR photonics", '"unclosed',
    "Память HBM", "x" * 201,
])
async def test_writer_rejects_missing_or_invalid_query_without_writes(query):
    session = _WriteSession("ru")
    arguments = _arguments(session.document.url)
    arguments["corpus_query"] = query
    result = await _orchestration(session)._write_entry(arguments, 20)
    assert "corpus_query" in result["error"]
    assert session.added == []


@pytest.mark.asyncio
async def test_writer_persists_normalized_agent_query(monkeypatch):
    async def effort_for(_session, _agent_ids):
        return {"searches_run": 0, "sources_checked": 0}

    monkeypatch.setattr("wsignal.inference.orchestration.effort_for", effort_for)
    session = _WriteSession("ru")
    arguments = _arguments(session.document.url)
    arguments["corpus_query"] = "  photonic   processors OR optical processors  "
    result = await _orchestration(session)._write_entry(arguments, 20)
    assert "error" not in result
    entry = next(row for row in session.added if isinstance(row, Entry))
    assert entry.corpus_query == "photonic processors OR optical processors"
    assert session.commits == 1


@pytest.mark.asyncio
async def test_report_corpus_prefers_agent_query_and_labels_legacy_fallback():
    class Session:
        async def get(self, *_args):
            return object()

        async def scalars(self, _statement):
            return SimpleNamespace(all=lambda: [
                Entry(id=5, name_en="AI inference", name_ru="Инференс", corpus_query="CPO"),
                Entry(id=9, name_en="Optical circuit switching (OCS)", name_ru="Оптика"),
                Entry(id=7, name_ru="Память HBM для ИИ", corpus_query="  "),
            ])

    class Corpus:
        async def series(self, terms, months):
            assert terms == ["CPO", "Optical circuit switching OR OCS", "HBM"]
            assert months == 36
            return [
                {"months": [{"m": "2026-09", "n": 4, "total": 100}],
                 "matched_total": 4, "too_broad": True}
                for _ in terms
            ]

    result = await run_corpus(21, Session(), Corpus(), 36)
    assert [(row.entry_id, row.entry_rank, row.source) for row in result] == [
        (5, 1, "agent"), (9, 2, "derived"), (7, 3, "derived"),
    ]
    assert all(row.matched_total == 4 and row.too_broad for row in result)
