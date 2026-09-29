import json
from datetime import date
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy.dialects import postgresql

import wsignal.corpus as corpus_module
import wsignal.inference.tools as tools
from wsignal.corpus import Corpus
from wsignal.inference.tools import (
    ROLE_TOOLS,
    Toolbox,
    effort_for,
    render_tool_result,
    tools_for,
)
from wsignal.inference.verify import quote_appears_in, verify_run
from wsignal.models import Document
from wsignal.parsing.tiering import host_tier


class FakeCorpus:
    def __init__(self, url="postgresql://corpus", failure=None):
        self.url = url
        self.failure = failure
        self.calls = []
        self.rows = []
        self.series_result = None
        self.articles = {}

    def _maybe_fail(self):
        if self.failure is not None:
            raise self.failure

    async def search(
        self, query, since, until, lang, limit, oldest_first=False, agent_timeout=False,
    ):
        self.calls.append((
            "search", query, since, until, lang, limit, oldest_first, agent_timeout,
        ))
        self._maybe_fail()
        return self.rows[:limit]

    async def series(self, terms, months=36, agent_timeout=False):
        self.calls.append(("series", terms, months, agent_timeout))
        self._maybe_fail()
        return [self.series_result]

    async def article(self, article_id, agent_timeout=False):
        self.calls.append(("article", article_id, agent_timeout))
        self._maybe_fail()
        return self.articles.get(article_id)


class Session:
    def __init__(self, role="researcher"):
        self.role = role
        self.documents = []

    async def scalar(self, statement):
        if "agents.role" in str(statement):
            return self.role
        return self.documents[-1] if self.documents else None

    async def execute(self, statement):
        values = statement.compile(dialect=postgresql.dialect()).params
        self.documents.append(Document(id=len(self.documents) + 1, **values))

    async def rollback(self):
        pass


@pytest.fixture
def corpus(monkeypatch):
    fake = FakeCorpus()
    events = []

    async def record(**event):
        events.append(event)

    monkeypatch.setattr(tools, "get_corpus", lambda: fake)
    monkeypatch.setattr(tools, "record_source_event", record)
    fake.events = events
    return fake


def test_corpus_tool_reaches_every_role_and_only_counts_reaches_the_orchestrator():
    for role in ("researcher", "refuter", "orchestrator", "assistant"):
        assert "corpus" in ROLE_TOOLS[role]
    assistant = next(
        tool["function"] for tool in tools_for("assistant")
        if tool["function"]["name"] == "corpus"
    )
    assert assistant["parameters"]["properties"]["action"]["enum"] == [
        "search", "counts", "read"
    ]
    researcher = next(
        tool["function"] for tool in tools_for("researcher")
        if tool["function"]["name"] == "corpus"
    )
    assert researcher["parameters"]["properties"]["action"]["enum"] == [
        "search", "counts", "read"
    ]
    assert researcher["parameters"]["required"] == ["action"]
    orchestrator = next(
        tool["function"] for tool in tools_for("orchestrator")
        if tool["function"]["name"] == "corpus"
    )
    assert orchestrator["parameters"]["properties"]["action"]["enum"] == ["counts"]
    assert set(orchestrator["parameters"]["properties"]) == {
        "action", "query", "query_alt", "months"
    }
    assert researcher["parameters"]["properties"]["action"]["enum"] == [
        "search", "counts", "read"
    ]


@pytest.mark.asyncio
async def test_search_returns_small_hits_and_the_total(corpus):
    corpus.rows = [
        {
            "id": index,
            "title": "Co-packaged optics " + "x" * 400,
            "host": "example.test",
            "published": date(2026, 9, 25 - index),
            "lang": "en",
            "matched": 2406,
            "matched_at_least": False,
            "snippet": "lead " * 60 + "**co-packaged** **optics** " + "tail " * 60,
        }
        for index in range(1, 25)
    ]
    result = await Toolbox(Session(), None).call(
        "corpus",
        {
            "action": "search", "query": "  co-packaged optics  OR CPO ",
            "since": "2025-01-01", "until": "2026-09-30", "lang": "EN",
            "order": "oldest", "limit": 50,
        },
        7,
    )
    assert corpus.calls == [(
        "search", "co-packaged optics OR CPO", date(2025, 1, 1), date(2026, 9, 30),
        "en", 25, True, True,
    )]
    assert result["matched"] == 2406
    assert result["matched_at_least"] is False
    assert result["shown"] == 24
    assert result["since"] == "2025-01-01"
    hit = result["hits"][0]
    assert set(hit) == {"id", "title", "host", "date", "lang", "snippet"}
    assert hit["date"] == "2026-09-24"
    assert len(hit["title"]) == 160
    assert len(hit["snippet"]) <= 200
    assert "co-packaged optics" in hit["snippet"]
    assert "**" not in hit["snippet"]
    assert len(render_tool_result(result, "corpus", {"action": "search"})) < 12000
    assert corpus.events[0]["adapter"] == "corpus"
    assert corpus.events[0]["ok"] is True


@pytest.mark.asyncio
async def test_capped_search_total_is_returned_as_a_lower_bound(corpus):
    corpus.rows = [{
        "id": 1,
        "title": "Broad match",
        "host": "example.test",
        "published": date(2026, 9, 25),
        "lang": "en",
        "matched": 10000,
        "matched_at_least": True,
        "snippet": "AI research",
    }]
    result = await Toolbox(Session(), None).call(
        "corpus", {"action": "search", "query": "AI"}, 7,
    )
    assert result["matched"] == 10000
    assert result["matched_at_least"] is True
    assert result["shown"] == 1


@pytest.mark.asyncio
async def test_empty_search_says_what_it_measured(corpus):
    result = await Toolbox(Session(), None).call(
        "corpus", {"action": "search", "query": "neuromorphic dust"}, 7
    )
    assert result["matched"] == 0
    assert result["hits"] == []
    assert "not the web" in result["note"]
    assert corpus.calls[0][5] == 25
    assert corpus.calls[0][6] is False
    assert corpus.calls[0][7] is True


@pytest.mark.asyncio
async def test_counts_reports_shares_ratio_and_too_broad(corpus):
    months = [
        {"m": f"{2023 + (index + 9) // 12}-{(index + 9) % 12 + 1:02d}",
         "n": 2 * (index + 1), "total": 1000 * (index + 1)}
        for index in range(36)
    ]
    months[-1]["n"] = 4 * 36
    corpus.series_result = {"months": months, "matched_total": 1000, "too_broad": True}
    result = await Toolbox(Session(), None).call(
        "corpus", {"action": "counts", "query": "CPO"}, 7
    )
    assert corpus.calls == [("series", ["CPO"], 36, True)]
    assert result["from"] == "2023-10"
    assert result["to"] == "2026-09"
    assert len(result["matches"]) == len(result["totals"]) == len(result["share_per_10k"]) == 36
    assert result["share_per_10k"][0] == 20.0
    assert result["share_per_10k"][-1] == 40.0
    assert result["recent_months"] == 12
    recent = sum(month["n"] for month in months[-12:]) * 10000 / sum(
        month["total"] for month in months[-12:]
    )
    assert result["recent_share_per_10k"] == round(recent, 2)
    assert result["previous_share_per_10k"] == 20.0
    assert result["share_ratio"] == round(round(recent, 2) / 20.0, 2)
    assert result["too_broad"] is True
    assert "too_broad" in result["note"]
    assert len(render_tool_result(result, "corpus", {"action": "counts"})) < 2000


@pytest.mark.asyncio
async def test_counts_without_a_previous_share_has_no_ratio(corpus):
    corpus.series_result = {
        "months": [
            {"m": "2026-08", "n": 0, "total": 0},
            {"m": "2026-09", "n": 3, "total": 100},
        ],
        "matched_total": 3,
        "too_broad": False,
    }
    result = await Toolbox(Session(), None).call(
        "corpus", {"action": "counts", "query": "CPO", "months": 2}, 7
    )
    assert result["share_per_10k"] == [None, 300.0]
    assert result["recent_months"] == 1
    assert result["share_ratio"] is None


@pytest.mark.asyncio
async def test_read_stores_the_article_as_a_fetched_page_a_citation_verifies_against(corpus):
    body = "Lead paragraph about co-packaged optics shipping in switches. " + "More. " * 5000
    corpus.articles[3054] = {
        "id": 3054,
        "url": "https://www.example.test/cpo",
        "host": "www.example.test",
        "lang": "en",
        "published": date(2026, 9, 25),
        "date_source": "jsonld",
        "title": "Vendor ships CPO switch",
        "text": body,
    }
    session = Session()
    result = await Toolbox(session, None).call("corpus", {"action": "read", "id": 3054}, 7)

    stored = session.documents[0]
    assert stored.url == "https://www.example.test/cpo"
    assert stored.title == "Vendor ships CPO switch"
    assert stored.content == "Vendor ships CPO switch\n\n" + body
    assert stored.content_sha == tools.content_sha(stored.content)
    assert stored.retrieved is True
    assert stored.source_type == "page"
    assert stored.source_name == "www.example.test"
    assert stored.source_lang == "en"
    assert stored.source_tier == host_tier("www.example.test")
    assert stored.published_at == date(2026, 9, 25)
    assert stored.date_source == "jsonld"
    assert stored.fetched_by_agent_id == 7
    assert stored.adapter is None

    assert result["stored"] is True
    assert result["id"] == 3054
    assert result["url"] == stored.url
    assert len(result["content"]) == 20000
    assert result["next_offset"] == 20000
    assert "call corpus action=read id=3054 with offset=20000" in result["note"]
    assert "from_store" not in result

    quote = "Lead paragraph about co-packaged optics shipping in switches."
    assert quote in result["content"]
    citation = SimpleNamespace(quote=quote, verified=False, verified_at=None)

    class VerifySession:
        async def execute(self, _statement):
            return SimpleNamespace(all=lambda: [(citation, stored.content, stored.source_type)])

        async def commit(self):
            pass

    assert await verify_run(VerifySession(), 1) == {"checked": 1, "verified": 1, "failed": 0}
    assert citation.verified is True
    assert not quote_appears_in("a sentence the article never wrote", stored.content)

    second = await Toolbox(session, None).call(
        "corpus", {"action": "read", "id": 3054, "offset": 20000}, 7
    )
    assert second["chars_from"] == 20000


@pytest.mark.asyncio
async def test_read_of_a_missing_article_stores_nothing(corpus):
    session = Session()
    result = await Toolbox(session, None).call("corpus", {"action": "read", "id": 5}, 7)
    assert result["action"] == "read"
    assert result["error"].startswith("no corpus article with id 5;")
    assert session.documents == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    corpus_module.CorpusUnavailable("Корпус временно недоступен"),
    OSError("host down"),
    TimeoutError(),
])
@pytest.mark.parametrize("arguments", [
    {"action": "search", "query": "CPO"},
    {"action": "counts", "query": "CPO"},
    {"action": "read", "id": 1},
])
async def test_a_corpus_that_is_down_answers_with_an_error_the_agent_can_read(
    corpus, failure, arguments
):
    corpus.failure = failure
    session = Session()
    result = await Toolbox(session, None).call("corpus", arguments, 7)
    assert result["error"] == "the research corpus did not answer"
    assert "not an absence" in result["note"]
    assert "host down" not in json.dumps(result, ensure_ascii=False)
    assert session.documents == []
    assert corpus.events[-1]["ok"] is False


@pytest.mark.asyncio
async def test_an_unconfigured_corpus_is_reported_without_a_query(corpus):
    corpus.url = ""
    result = await Toolbox(Session(), None).call(
        "corpus", {"action": "counts", "query": "CPO"}, 7
    )
    assert "not configured" in result["error"]
    assert corpus.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("arguments", "message"), [
    ({"action": "find", "query": "CPO"}, "is not one of search, counts or read"),
    ({"action": "search", "query": " "}, "query must be nonblank"),
    ({"action": "search", "query": "x" * 201}, "at most 200"),
    ({"action": "search", "query": "CPO", "since": "last year"}, "since must be a date"),
    ({"action": "search", "query": "CPO", "since": "2026-02-01", "until": "2026-01-01"},
     "since must not be later"),
    ({"action": "search", "query": "CPO", "order": "random"}, "order must be"),
    ({"action": "counts", "query": "CPO", "months": 121}, "months must be"),
    ({"action": "read"}, "id must be"),
    ({"action": "read", "id": "abc"}, "invalid literal"),
])
async def test_bad_arguments_are_refused_before_the_corpus_is_asked(corpus, arguments, message):
    result = await Toolbox(Session(), None).call("corpus", arguments, 7)
    assert message in result["error"]
    assert corpus.calls == []


@pytest.mark.asyncio
async def test_the_orchestrator_may_only_count(corpus):
    corpus.series_result = {
        "months": [{"m": "2026-09", "n": 1, "total": 10}],
        "matched_total": 1,
        "too_broad": False,
    }
    toolbox = Toolbox(Session(role="orchestrator"), None)
    refused = await toolbox.call("corpus", {"action": "read", "id": 1}, 3)
    assert refused["error"].startswith("the orchestrator may use only action=counts")
    counted = await toolbox.call("corpus", {"action": "counts", "query": "CPO", "months": 1}, 3)
    assert counted["matches"] == [1]
    assert "recent_months" not in counted


@pytest.mark.asyncio
async def test_corpus_queries_count_as_searches_and_reads_do_not():
    contents = [
        {"name": "search", "arguments": {"adapter": "brave", "query": "CPO"}},
        {"name": "corpus", "arguments": {"action": "search", "query": "CPO"}},
        {"name": "corpus", "arguments": {"action": "counts", "query": "CPO"}},
        {"name": "corpus", "arguments": {"action": "read", "id": 1}},
    ]

    class EffortSession:
        async def execute(self, _statement):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: contents))

    assert await effort_for(EffortSession(), [1]) == {
        "searches_run": 3, "sources_checked": 2, "tool_calls": 4,
    }


@pytest.mark.asyncio
async def test_corpus_client_sends_search_series_and_read_over_http():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, body))
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json={"results": [
                {"id": 1, "published": "2026-02-03", "matched": 4, "snippet": "s"},
            ]})
        if request.url.path.endswith("/series"):
            return httpx.Response(200, json={"series": [
                {"months": [], "matched_total": 0, "too_broad": False},
            ]})
        if request.url.path.endswith("/article/9"):
            return httpx.Response(404, json={"detail": "not found"})
        return httpx.Response(200, json={"article": {"id": 2, "published": None}})

    corpus = Corpus("https://corpus.test/v1/corpus")
    corpus._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    hits = await corpus.search("CPO", date(2026, 1, 1), None, "en", 8)
    assert hits == [{"id": 1, "published": date(2026, 2, 3), "matched": 4, "snippet": "s"}]
    await corpus.search("CPO", None, None, None, 8, oldest_first=True)
    assert len(await corpus.series(["CPO"], 12)) == 1
    assert await corpus.article(9) is None
    assert (await corpus.article(2))["id"] == 2
    assert calls[0] == ("POST", "/v1/corpus/search", {
        "query": "CPO", "since": "2026-01-01", "until": None, "lang": "en",
        "limit": 8, "oldest_first": False,
    })
    assert calls[1][2]["oldest_first"] is True
    assert calls[2] == ("POST", "/v1/corpus/series", {"terms": ["CPO"], "months": 12})
    assert calls[3][:2] == ("GET", "/v1/corpus/article/9")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response,message",
    [
        (httpx.Response(503, json={"detail": "Запрос к корпусу превысил лимит времени"}),
         "Запрос к корпусу превысил лимит времени"),
        (httpx.Response(429, json={"detail": "slow down"}),
         "Лимит запросов к корпусу исчерпан, повторите позже"),
        (httpx.Response(502, text="bad gateway"), "Корпус временно недоступен"),
    ],
)
async def test_corpus_client_errors_become_corpus_unavailable(response, message):
    corpus = Corpus("https://corpus.test/v1/corpus")
    corpus._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: response))
    with pytest.raises(corpus_module.CorpusUnavailable, match=message):
        await corpus.series(["CPO"])


@pytest.mark.asyncio
async def test_corpus_client_without_a_url_is_unavailable():
    with pytest.raises(corpus_module.CorpusUnavailable, match="Корпус не настроен"):
        await Corpus("").search("CPO", None, None, None, 8)
