from types import SimpleNamespace

import pytest

from wsignal.inference.orchestration import ORCHESTRATOR_TOOLS, Orchestration
from wsignal.inference.prompts import load_assistant_lookup_prompt
from wsignal.inference.tools import Toolbox
from wsignal.inference.verify import quote_appears_in, verify_run


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows

    def scalars(self):
        return self


class _LookupSession:
    def __init__(self, role="assistant"):
        self.role = role
        self.web_calls = 0
        self.turns = []
        self.turn_query = None

    async def scalar(self, statement):
        query = str(statement).lower()
        if "source_events" in query:
            return self.web_calls
        return self.role

    async def execute(self, statement):
        self.execute_statement = statement
        return _Rows([])

    async def scalars(self, statement):
        self.turn_query = statement
        return _Rows(self.turns)

    async def rollback(self):
        return None


@pytest.mark.asyncio
async def test_assistant_lookup_returns_capped_answer_urls_and_only_runs_lookup_agent(
    monkeypatch,
):
    from wsignal.config import Settings

    settings = Settings(
        assistant_lookup_max_steps=4,
        assistant_lookup_web_calls=6,
        assistant_lookup_answer_chars=12,
    )
    monkeypatch.setattr("wsignal.inference.orchestration.get_settings", lambda: settings)
    session = _LookupSession()
    session.turns = [SimpleNamespace(content={"payload": {"records": [{
        "url": "https://source.test/page"
    }]}}), SimpleNamespace(content={"payload": {"content": "private page body"}})]
    created = {}

    class ParentRuntime:
        async def create(self, **kwargs):
            created.update(kwargs)
            return SimpleNamespace(id=70)

    class LookupRuntime:
        def __init__(self, _session, _llm, toolbox, max_steps, _sink):
            created["runtime_steps"] = max_steps
            created["lookup_toolbox"] = toolbox

        async def run(self, agent, question, partial_on_failure):
            created["run_agent_id"] = agent.id
            created["question"] = question
            created["partial_on_failure"] = partial_on_failure
            return SimpleNamespace(content="A sufficiently long answer.", stopped="answered")

    monkeypatch.setattr("wsignal.inference.orchestration.AgentRuntime", LookupRuntime)
    orchestration = object.__new__(Orchestration)
    orchestration._runtime = ParentRuntime()
    orchestration._session = session
    orchestration._run = SimpleNamespace(id=9)
    orchestration._llm = object()
    orchestration._fetcher = object()
    orchestration._cache = object()
    orchestration._sink = None

    result = await orchestration._ask_assistant({"question": "Find the result"}, 20)

    assert result == {
        "answer": "A sufficient",
        "urls": ["https://source.test/page"],
        "stopped": "answered",
    }
    assert created["role"] == "assistant"
    assert created["system"] == load_assistant_lookup_prompt()
    assert created["runtime_steps"] == 4
    assert created["run_agent_id"] == 70
    assert created["question"] == "Find the result"
    assert created["lookup_toolbox"]._web_call_limits == {70: 6}
    assert "private page body" not in str(result)
    assert "documents.fetched_by_agent_id = :fetched_by_agent_id_1" in str(
        session.execute_statement
    )
    assert 70 in session.execute_statement.compile().params.values()
    schema = next(
        tool["function"] for tool in ORCHESTRATOR_TOOLS
        if tool["function"]["name"] == "ask_assistant"
    )
    assert schema["parameters"]["required"] == ["question"]


@pytest.mark.asyncio
async def test_assistant_lookup_web_call_cap_counts_fetch_and_search(monkeypatch):
    from wsignal.config import Settings

    monkeypatch.setattr(
        "wsignal.inference.tools.get_settings",
        lambda: Settings(assistant_lookup_web_calls=2),
    )
    session = _LookupSession()

    async def record(**event):
        if event["adapter"] == "web_call":
            session.web_calls += 1

    monkeypatch.setattr("wsignal.inference.tools.record_source_event", record)
    toolbox = Toolbox(session, None)
    toolbox.configure_assistant_lookup(8, 2)
    assert [schema["function"]["name"] for schema in toolbox.schemas_for("assistant")] == [
        "corpus", "search", "fetch", "list_sources", "harness_note"
    ]
    called = []

    async def search(_arguments, _agent_id):
        called.append("search")
        return {"records": []}

    async def fetch(_arguments, _agent_id, via=None):
        called.append("fetch")
        assert via == "assistant_lookup"
        return {"url": "https://source.test/page"}

    toolbox._search = search
    toolbox._fetch = fetch

    first = await toolbox.call("search", {"query": "q"}, 8)
    second = await toolbox.call("fetch", {"url": "https://source.test/page"}, 8)
    refused = await toolbox.call("fetch", {"url": "https://source.test/other"}, 8)

    assert first["web_calls_remaining"] == 1
    assert second["web_calls_remaining"] == 0
    assert "no request was made" in refused["error"]
    assert called == ["search", "fetch"]
    assert session.web_calls == 2


@pytest.mark.asyncio
async def test_unstored_citation_is_autofetched_and_quote_verifies(monkeypatch):
    page = SimpleNamespace(
        id=88,
        url="https://source.test/page",
        source_lang="ru",
        content="A verbatim quote from the retrieved page.",
        source_type="page",
    )
    lookups = []

    async def newest(_session, url):
        lookups.append(url)
        return None if len(lookups) == 1 else page

    async def fetch(url, writer_id):
        assert url == page.url
        assert writer_id == 20
        return page

    monkeypatch.setattr(
        "wsignal.inference.orchestration.newest_fetchable_document", newest
    )
    orchestration = object.__new__(Orchestration)
    orchestration._session = object()
    orchestration._fetch_citation_page = lambda url, writer_id: fetch(url, writer_id)

    resolved, unresolved, error = await orchestration._resolve_entry_citations(
        [{"url": page.url, "quote": "A verbatim quote from the retrieved page"}], 20
    )

    assert error is None
    assert unresolved == []
    assert resolved == [
        ({"url": page.url, "quote": "A verbatim quote from the retrieved page"}, page)
    ]
    assert quote_appears_in(resolved[0][0]["quote"], page.content)


@pytest.mark.asyncio
async def test_fetch_failure_document_cannot_verify_quote():
    citation = SimpleNamespace(
        quote="Citation page retrieval failed: connection refused",
        verified=True,
        verified_at=None,
        document_id=91,
    )

    class VerifySession:
        async def execute(self, _statement):
            return _Rows([(citation, citation.quote, "fetch_failure")])

        async def commit(self):
            return None

    result = await verify_run(VerifySession(), 4)

    assert result == {"checked": 1, "verified": 0, "failed": 1}
    assert citation.verified is False
    assert citation.verified_at is not None
