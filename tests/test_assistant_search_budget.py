from types import SimpleNamespace

import pytest

from wsignal.inference.tools import Toolbox
from wsignal.parsing.base import HarvestResult, Surface


class _Scalars:
    def __init__(self, rows):
        self.rows = rows

    def scalars(self):
        return self

    def all(self):
        return self.rows


class _Session:
    def __init__(self, role, searches, source_events=0, automatic_events=0):
        self.role = role
        self.searches = searches
        self.source_events = source_events
        self.automatic_events = automatic_events
        self.web_calls = 0

    async def scalar(self, statement):
        query = str(statement).lower()
        if "source_events" in query:
            params = statement.compile().params
            if "web_call" in params.values():
                return self.web_calls
            return self.automatic_events if "via is not null" in query else self.source_events
        return self.role

    async def execute(self, _statement):
        return _Scalars([{"name": "search"} for _ in range(self.searches)])

    async def rollback(self):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize(("searches", "allowed"), [(4, True), (9, False)])
async def test_assistant_search_budget_boundary(monkeypatch, searches, allowed):
    monkeypatch.setattr(
        "wsignal.inference.tools.get_settings",
        lambda: SimpleNamespace(assistant_max_searches=8),
    )
    called = False

    async def harvest(*_args, **_kwargs):
        nonlocal called
        called = True
        return HarvestResult(surface=Surface(adapter="arxiv", query="q"))

    monkeypatch.setattr(Toolbox, "_harvest", harvest)
    toolbox = Toolbox(_Session("assistant", searches), None)

    result = await toolbox._search({"adapter": "arxiv", "query": "q"}, 1)

    if allowed:
        assert result["count"] == 0
        assert called is True
    else:
        assert result["error"].startswith("your web search budget of 8 is spent")
        assert "cut the direction into fields now" in result["hint"]
        assert "adapter=store" in result["hint"]
        assert called is False


@pytest.mark.asyncio
async def test_assistant_store_search_is_never_capped(monkeypatch):
    monkeypatch.setattr(
        "wsignal.inference.tools.get_settings",
        lambda: SimpleNamespace(assistant_max_searches=8),
    )
    searched = []

    async def store(_self, arguments, _agent_id):
        searched.append(arguments["query"])
        return {"count": 0}

    monkeypatch.setattr(Toolbox, "_search_store", store)
    toolbox = Toolbox(_Session("assistant", 50), None)

    result = await toolbox._search({"adapter": "store", "query": "q"}, 1)

    assert result == {"count": 0}
    assert searched == ["q"]


@pytest.mark.asyncio
async def test_store_search_and_stored_fetch_spend_no_web_calls(monkeypatch):
    from datetime import UTC, datetime

    from wsignal.config import Settings

    settings = Settings(researcher_web_calls=2)
    monkeypatch.setattr("wsignal.inference.tools.get_settings", lambda: settings)
    session = _Session("researcher", 0)
    session.web_calls = 2

    async def record(**_kwargs):
        session.web_calls += 1

    monkeypatch.setattr("wsignal.inference.tools.record_source_event", record)
    toolbox = Toolbox(session, None)
    handled = []

    async def search(arguments, _agent_id):
        handled.append(arguments["adapter"])
        return {"count": 0}

    async def fetch(arguments, _agent_id, via=None):
        handled.append(arguments["url"])
        return {"content": "page"}

    async def newest(url):
        if url == "https://stored.test":
            return SimpleNamespace(fetched_at=datetime.now(UTC))
        return None

    monkeypatch.setattr(toolbox, "_search", search)
    monkeypatch.setattr(toolbox, "_fetch", fetch)
    monkeypatch.setattr(toolbox, "_newest_fetchable", newest)

    store = await toolbox.call("search", {"adapter": "store", "query": "q"}, 1)
    stored = await toolbox.call("fetch", {"url": "https://stored.test"}, 1)
    live = await toolbox.call("fetch", {"url": "https://stored.test", "live": True}, 1)
    network = await toolbox.call("search", {"adapter": "arxiv", "query": "q"}, 1)

    assert store == {"count": 0, "web_calls_remaining": 0}
    assert stored == {"content": "page", "web_calls_remaining": 0}
    assert "web-call budget is spent" in live["error"]
    assert "web-call budget is spent" in network["error"]
    assert handled == ["store", "https://stored.test"]
    assert session.web_calls == 2


@pytest.mark.asyncio
async def test_researcher_search_is_not_capped(monkeypatch):
    monkeypatch.setattr(
        "wsignal.inference.tools.get_settings",
        lambda: SimpleNamespace(assistant_max_searches=8),
    )
    called = False

    async def harvest(_self, _adapter, _source, surface, _agent_id, via=None):
        nonlocal called
        called = True
        return HarvestResult(surface=surface)

    monkeypatch.setattr(Toolbox, "_harvest", harvest)
    toolbox = Toolbox(_Session("researcher", 100), None)

    result = await toolbox._search({"adapter": "arxiv", "query": "q"}, 1)

    assert result["count"] == 0
    assert called is True


@pytest.mark.asyncio
async def test_assistant_budget_setting_defaults_to_eight():
    from wsignal.config import Settings

    assert Settings().assistant_max_searches == 8
    assert Settings().assistant_lookup_max_steps == 10
    assert Settings().assistant_lookup_web_calls == 15
    assert Settings().assistant_lookup_answer_chars == 2500


@pytest.mark.parametrize("role", ["researcher", "refuter"])
@pytest.mark.parametrize("adapter", ["brave", "web_search"])
@pytest.mark.parametrize("at_limit", [False, True])
@pytest.mark.asyncio
async def test_brave_quota_boundary_uses_settings_allowance(
    monkeypatch, role, adapter, at_limit
):
    from wsignal.config import Settings

    settings = Settings()
    allowance = settings.quota_for("brave", role)
    source_events = allowance if at_limit else allowance - 1
    monkeypatch.setattr("wsignal.inference.tools.get_settings", lambda: settings)
    called = False

    async def harvest(_self, _adapter, _source, surface, _agent_id, via=None):
        nonlocal called
        called = True
        return HarvestResult(surface=surface)

    monkeypatch.setattr(Toolbox, "_harvest", harvest)
    fetcher = SimpleNamespace(queue_estimate=lambda _host: 0)
    toolbox = Toolbox(_Session(role, 0, source_events=source_events), fetcher)

    result = await toolbox._search({"adapter": adapter, "query": "q"}, 1)

    assert called is not at_limit
    if at_limit and adapter == "brave":
        assert result["error"] == "quota spent for 'brave'"
        assert "web_search" not in result["hint"]
    elif at_limit:
        assert "brave" in result["sources_not_asked"]
    elif adapter == "brave":
        assert result["count"] == 0
    else:
        assert result["sources_answered"] == ["brave"]


@pytest.mark.asyncio
async def test_list_sources_reports_brave_calls_left(monkeypatch):
    from wsignal.config import Settings

    monkeypatch.setattr("wsignal.inference.tools.get_settings", Settings)
    settings = Settings()
    toolbox = Toolbox(
        _Session("researcher", 0, source_events=settings.quota_for("brave", "researcher") - 1),
        None,
    )

    result = await toolbox._list_sources({}, 1)

    brave = next(row for row in result["sources"] if row["adapter"] == "brave")
    assert brave["calls_left_this_run"] == 1


@pytest.mark.asyncio
async def test_web_call_cap_counts_stored_events_and_refuses_without_request(monkeypatch):
    from wsignal.config import Settings

    settings = Settings(researcher_web_calls=2)
    monkeypatch.setattr("wsignal.inference.tools.get_settings", lambda: settings)
    session = _Session("researcher", 0)
    session.web_calls = 1

    async def record(**_kwargs):
        session.web_calls += 1

    monkeypatch.setattr("wsignal.inference.tools.record_source_event", record)
    toolbox = Toolbox(session, None)
    requests = []

    async def search(_arguments, _agent_id):
        requests.append("search")
        return {"count": 0}

    monkeypatch.setattr(toolbox, "_search", search)

    first = await toolbox.call("search", {"adapter": "arxiv", "query": "q"}, 1)
    refused = await toolbox.call("fetch", {"url": "https://example.test"}, 1)

    assert first["web_calls_remaining"] == 0
    assert refused["web_calls_remaining"] == 0
    assert "web-call budget is spent" in refused["error"]
    assert "no request was made" in refused["error"]
    assert requests == ["search"]
    assert session.web_calls == 2


@pytest.mark.asyncio
async def test_web_call_remaining_is_returned_and_decreases(monkeypatch):
    from wsignal.config import Settings

    settings = Settings(refuter_web_calls=3)
    monkeypatch.setattr("wsignal.inference.tools.get_settings", lambda: settings)
    session = _Session("refuter", 0)

    async def record(**_kwargs):
        session.web_calls += 1

    monkeypatch.setattr("wsignal.inference.tools.record_source_event", record)
    toolbox = Toolbox(session, None)

    async def fetch(_arguments, _agent_id):
        return {"content": "page"}

    monkeypatch.setattr(toolbox, "_fetch", fetch)
    first = await toolbox.call("fetch", {"url": "https://example.test"}, 1)
    second = await toolbox.call("fetch", {"url": "https://example.test"}, 1)

    assert first["web_calls_remaining"] == 2
    assert second["web_calls_remaining"] == 1


class _CorpusFirstSession(_Session):
    def __init__(self, role, asked_corpus):
        super().__init__(role, 0)
        self.asked_corpus = asked_corpus
        self.corpus_lookups = 0

    async def scalar(self, statement):
        query = str(statement).lower()
        if "source_events.id" in query:
            self.corpus_lookups += 1
            assert "corpus" in statement.compile().params.values()
            return 41 if self.asked_corpus else None
        return await super().scalar(statement)


def _corpus_first_toolbox(monkeypatch, role, asked_corpus, corpus_url="postgresql://corpus"):
    from wsignal.config import Settings

    settings = Settings(_env_file=None)
    monkeypatch.setattr("wsignal.inference.tools.get_settings", lambda: settings)
    monkeypatch.setattr(
        "wsignal.inference.tools.get_corpus", lambda: SimpleNamespace(url=corpus_url)
    )
    harvested = []

    async def harvest(_self, adapter, _source, surface, _agent_id, via=None):
        harvested.append(adapter)
        return HarvestResult(surface=surface)

    monkeypatch.setattr(Toolbox, "_harvest", harvest)
    session = _CorpusFirstSession(role, asked_corpus)
    return Toolbox(session, None), session, harvested


@pytest.mark.asyncio
async def test_assistant_web_search_is_refused_until_the_corpus_was_asked(monkeypatch):
    toolbox, session, harvested = _corpus_first_toolbox(monkeypatch, "assistant", False)

    result = await toolbox._search({"adapter": "arxiv", "query": "q"}, 1)

    assert result["error"] == "search our research corpus first"
    assert "action=search" in result["hint"]
    assert harvested == []
    assert session.corpus_lookups == 1


@pytest.mark.asyncio
async def test_assistant_web_search_opens_after_a_corpus_call(monkeypatch):
    toolbox, _session, harvested = _corpus_first_toolbox(monkeypatch, "assistant", True)

    result = await toolbox._search({"adapter": "arxiv", "query": "q"}, 1)

    assert result["count"] == 0
    assert harvested == ["arxiv"]


@pytest.mark.asyncio
async def test_the_corpus_first_rule_spares_researchers_and_unconfigured_corpora(monkeypatch):
    toolbox, session, harvested = _corpus_first_toolbox(monkeypatch, "researcher", False)
    await toolbox._search({"adapter": "arxiv", "query": "q"}, 1)
    assert harvested == ["arxiv"]
    assert session.corpus_lookups == 0

    toolbox, session, harvested = _corpus_first_toolbox(
        monkeypatch, "assistant", False, corpus_url=""
    )
    await toolbox._search({"adapter": "arxiv", "query": "q"}, 1)
    assert harvested == ["arxiv"]
    assert session.corpus_lookups == 0


def test_the_assistant_has_the_corpus_tool_and_its_prompts_put_it_first():
    from pathlib import Path as _Path

    from wsignal.inference.tools import ROLE_TOOLS

    assert "corpus" in ROLE_TOOLS["assistant"]
    root = _Path(__file__).resolve().parents[1] / "prompts"
    assert "Start with `corpus`" in (root / "assistant.md").read_text(encoding="utf-8")
    assert "(`corpus`) first" in (root / "assistant_lookup.md").read_text(encoding="utf-8")



@pytest.mark.asyncio
async def test_metered_counts_skip_calls_refused_before_any_request(monkeypatch):
    from wsignal.config import Settings

    monkeypatch.setattr("wsignal.inference.tools.get_settings", Settings)
    statements = []

    class Session:
        async def scalar(self, statement):
            statements.append(str(statement))
            return "researcher" if "agents.role" in str(statement) else 0

    toolbox = Toolbox(Session(), None)
    assert await toolbox._over_quota("brave", 1) is None
    await toolbox._automatic_spent("brave", 1)
    await toolbox._list_sources({}, 1)
    counts = [sql for sql in statements if "count(source_events.id)" in sql]
    assert len(counts) >= 3
    for sql in counts:
        assert "source_events.ok IS true" in sql
        assert "coalesce(source_events.requests" in sql


@pytest.mark.asyncio
async def test_a_tool_exception_does_not_roll_back_the_shared_session(monkeypatch):
    from wsignal.config import Settings

    settings = Settings(researcher_web_calls=5)
    monkeypatch.setattr("wsignal.inference.tools.get_settings", lambda: settings)
    session = _Session("researcher", 0)
    rollbacks = []

    async def rollback():
        rollbacks.append(True)

    session.rollback = rollback

    async def record(**_kwargs):
        session.web_calls += 1

    monkeypatch.setattr("wsignal.inference.tools.record_source_event", record)
    toolbox = Toolbox(session, None)

    async def search(_arguments, _agent_id):
        raise RuntimeError("X_BEARER_TOKEN is not set")

    monkeypatch.setattr(toolbox, "_search", search)
    result = await toolbox.call("search", {"adapter": "arxiv", "query": "q"}, 1)

    assert "X_BEARER_TOKEN" in result["error"]
    assert rollbacks == []
    assert session.web_calls == 1


@pytest.mark.asyncio
async def test_a_refusal_before_any_request_spends_nothing(monkeypatch):
    from wsignal.config import Settings

    settings = Settings(researcher_web_calls=5)
    monkeypatch.setattr("wsignal.inference.tools.get_settings", lambda: settings)
    session = _Session("researcher", 0)

    async def record(**_kwargs):
        session.web_calls += 1

    monkeypatch.setattr("wsignal.inference.tools.record_source_event", record)
    toolbox = Toolbox(session, None)

    async def search(_arguments, _agent_id):
        return {"no_request": True, "error": "search our research corpus first"}

    monkeypatch.setattr(toolbox, "_search", search)
    result = await toolbox.call("search", {"adapter": "arxiv", "query": "q"}, 1)

    assert result["web_calls_remaining"] == 5
    assert session.web_calls == 0


def test_off_routes_are_refused_without_a_request(monkeypatch):
    from wsignal.inference import tools

    monkeypatch.setattr("wsignal.routes.resolve", lambda service: "off")
    refusal = tools._unavailable("x")
    assert refusal["no_request"] is True
    assert "not configured in this deployment" in refusal["error"]
    assert tools._unavailable("arxiv") is None
    monkeypatch.setattr("wsignal.routes.resolve", lambda service: "relay")
    assert tools._unavailable("web_search") is None


def test_a_402_from_brave_closes_brave_and_web_search_for_a_while(monkeypatch):
    import time as _time

    from wsignal.inference import tools

    monkeypatch.setattr("wsignal.routes.resolve", lambda service: "relay")
    monkeypatch.setattr(
        tools, "_CLOSED", {"brave": (_time.monotonic(), "19:40", "payment")}
    )
    for adapter in ("brave", "web_search"):
        refusal = tools._unavailable(adapter)
        assert refusal["no_request"] is True
        assert "402 Payment Required: its key has no credit at 19:40" in refusal["error"]
    assert tools._unavailable("epo") is None
    monkeypatch.setattr(
        tools, "_CLOSED", {"brave": (_time.monotonic() - 4000, "18:00", "payment")}
    )
    assert tools._unavailable("brave") is None


def test_two_ddg_challenges_in_a_row_close_ddg(monkeypatch):
    from wsignal.inference import tools

    monkeypatch.setattr(tools, "_CLOSED", {})
    monkeypatch.setattr(tools, "_ddg_challenges", 0)
    tools._note_ddg(challenged=True)
    tools._note_ddg(challenged=False)
    tools._note_ddg(challenged=True)
    assert tools._unavailable("ddg") is None
    tools._note_ddg(challenged=True)
    assert "bot challenge" in tools._unavailable("ddg")["error"]


@pytest.mark.asyncio
async def test_an_empty_query_is_refused_before_any_source(monkeypatch):
    monkeypatch.setattr(
        "wsignal.inference.tools.get_settings",
        lambda: SimpleNamespace(assistant_max_searches=8),
    )
    result = await Toolbox(_Session("researcher", 0), None)._search(
        {"adapter": "brave", "query": "  "}, 1
    )
    assert result["no_request"] is True
    assert "query is required" in result["error"]
