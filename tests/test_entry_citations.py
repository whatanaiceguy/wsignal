from types import SimpleNamespace

import pytest

from tests.test_fleet_tools import _citation_arguments, _citation_writer
from wsignal.inference.orchestration import ORCHESTRATOR_TOOLS
from wsignal.models import Citation, EntryPattern


def test_write_entry_schema_requires_citations():
    schema = next(
        tool["function"]["parameters"] for tool in ORCHESTRATOR_TOOLS
        if tool["function"]["name"] == "write_entry"
    )
    assert "citations" in schema["required"]


def _reviewed_writer(monkeypatch, state):
    async def effort_for(_session, _agent_ids):
        return {"searches_run": 3, "sources_checked": 2}

    monkeypatch.setattr("wsignal.inference.orchestration.effort_for", effort_for)
    orchestration, session, document = _citation_writer("ru")
    session.refutation_rows = [
        SimpleNamespace(
            id=12, state="completed", refuter_agent_id=44,
            rebuttal_required=False, outcome={},
        )
    ]

    async def delivered(_review, _writer_id):
        return True

    orchestration._review_delivered = delivered
    arguments = _citation_arguments(document.url)
    arguments["state"] = state
    arguments["patterns"] = [
        {"kind": "substance", "pattern": "regulatory_trigger", "strength": 0.65}
    ]
    return orchestration, session, arguments


@pytest.mark.parametrize("state", ["banked", "parked", "noise", "insufficient"])
async def test_omitted_citations_refused_before_writes(monkeypatch, state):
    orchestration, session, arguments = _reviewed_writer(monkeypatch, state)
    del arguments["citations"]

    result = await orchestration._write_entry(arguments, 20)

    assert "omitting citations is forbidden" in result["error"]
    assert "collect" in result["error"]
    assert "summary_ru" in result["error"]
    assert session.added == []
    assert session.commits == 0
    assert session.values[1].state == "dispatched"


@pytest.mark.parametrize(
    ("citations", "message"),
    [
        (None, "citations must be an array"),
        ({}, "citations must be an array"),
        ("[]", "citations must be an array"),
        ([None], "citations[0] must be an object"),
        ([{}], "citations[0].url"),
        ([{"url": " ", "quote": "quote"}], "citations[0].url"),
        ([{"url": 123, "quote": "quote"}], "citations[0].url"),
        ([{"url": "https://source.test"}], "citations[0].quote"),
        ([{"url": "https://source.test", "quote": " "}], "citations[0].quote"),
        ([{"url": "https://source.test", "quote": 123}], "citations[0].quote"),
    ],
)
async def test_invalid_citations_refused_before_writes(monkeypatch, citations, message):
    orchestration, session, arguments = _reviewed_writer(monkeypatch, "banked")
    arguments["citations"] = citations

    result = await orchestration._write_entry(arguments, 20)

    assert message in result["error"]
    assert session.added == []
    assert session.commits == 0


@pytest.mark.parametrize("state", ["banked", "parked", "noise", "insufficient"])
async def test_explicit_empty_citations_are_allowed_for_every_state(monkeypatch, state):
    orchestration, session, arguments = _reviewed_writer(monkeypatch, state)
    arguments["citations"] = []
    arguments["patterns"] = []
    arguments["why_ru"] = "Не установлено: источники недоступны."

    result = await orchestration._write_entry(arguments, 20)

    assert result["citations_stored"] == 0
    assert session.commits == 1


async def test_retry_with_full_citation_persists_evidence_and_pattern_link(monkeypatch):
    orchestration, session, arguments = _reviewed_writer(monkeypatch, "banked")
    citations = arguments.pop("citations")

    refused = await orchestration._write_entry(arguments, 20)
    assert "omitting citations is forbidden" in refused["error"]
    assert session.added == []
    assert session.commits == 0

    arguments["citations"] = citations
    arguments["patterns"][0]["quote"] = citations[0]["quote"]
    result = await orchestration._write_entry(arguments, 20)

    citation = next(row for row in session.added if isinstance(row, Citation))
    pattern = next(row for row in session.added if isinstance(row, EntryPattern))
    assert result["citations_stored"] == 1
    assert citation.quote == citations[0]["quote"]
    assert pattern.citation_id == citation.id
    assert session.values[1].state == "done"
    assert session.commits == 1
