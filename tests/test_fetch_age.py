from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

import wsignal.inference.tools as tools
from wsignal.inference.tools import Toolbox


class _Session:

    def __init__(self, *documents):
        self.documents = list(documents)
        self.statements = []

    async def scalar(self, statement):
        self.statements.append(statement)
        return self.documents[0] if self.documents else None


class _Fetcher:
    def __init__(self):
        self.calls = 0

    @asynccontextmanager
    async def measure(self):
        yield {"requests": 1, "bytes": 10}

    async def get_text(self, _url):
        self.calls += 1
        return "<html><body>fresh</body></html>"

    async def get_text_with_metadata(self, _url):
        self.calls += 1
        return "<html><body>fresh</body></html>", False


def _document(source_type, fetched_at, content="stored", adapter=None, retrieved=True):
    return SimpleNamespace(
        url="https://example.test/page",
        title="Title",
        content=content,
        fetched_at=fetched_at,
        source_type=source_type,
        adapter=adapter,
        retrieved=retrieved,
        published_at=None,
        source_name="Example",
        source_tier=1,
        links_to=None,
    )


async def _enable_network_fetch(monkeypatch, toolbox, refreshed):
    async def process(_raw, url=""):
        return "fresh", None, {}

    async def store_raw(*_args):
        return refreshed

    async def record_event(**_kwargs):
        return None

    monkeypatch.setattr(tools, "keyed_reader", lambda _url: None)
    monkeypatch.setattr(tools, "run_page_processing", process)
    monkeypatch.setattr(tools, "record_source_event", record_event)
    monkeypatch.setattr(toolbox, "_store_raw", store_raw)


@pytest.mark.asyncio
async def test_newest_fetchable_document_compiles_all_eligibility_and_order_predicates():
    session = _Session()
    await tools.newest_fetchable_document(session, "https://example.test/page")
    sql = str(
        session.statements[0].compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "documents.retrieved IS true" in sql
    assert "documents.source_type NOT IN ('paper', 'patent')" in sql
    assert "documents.adapter IS DISTINCT FROM 'hn'" in sql
    assert (
        "documents.adapter NOT IN "
        "('arxiv', 'epo', 'openalex', 'rospatent', 'brave', 'ddg')"
    ) in sql
    assert "ORDER BY documents.fetched_at DESC" in sql
    assert "LIMIT 1" in sql


@pytest.mark.asyncio
async def test_fetch_refetches_when_selector_returns_no_document(monkeypatch):
    refreshed = _document("page", datetime.now(UTC), "fresh content")
    fetcher = _Fetcher()
    toolbox = Toolbox(_Session(), fetcher)
    await _enable_network_fetch(monkeypatch, toolbox, refreshed)
    result = await toolbox._fetch({"url": refreshed.url}, None)
    assert fetcher.calls == 1
    assert result["from_store"] is False
    assert result["content"] == "fresh content"


@pytest.mark.asyncio
async def test_fetch_reuses_the_document_returned_by_selector():
    stored = _document("page", datetime.now(UTC) - timedelta(hours=12), "full page")
    fetcher = _Fetcher()
    result = await Toolbox(_Session(stored), fetcher)._fetch({"url": stored.url}, None)
    assert fetcher.calls == 0
    assert result["from_store"] is True
    assert result["content"] == "full page"
    assert result["fetched_at"] == stored.fetched_at.isoformat()


@pytest.mark.asyncio
async def test_fetch_returns_parts_and_serves_later_part_without_http(monkeypatch):
    monkeypatch.setattr(
        tools,
        "get_settings",
        lambda: SimpleNamespace(
            fetch_part_chars=3, document_max_age_hours=720, researcher_web_calls=50
        ),
    )
    fetcher = _Fetcher()

    class CallSession(_Session):
        def __init__(self):
            super().__init__()
            self.web_calls = 0

        async def scalar(self, statement):
            if "agents.role" in str(statement):
                return "researcher"
            if "source_events" in str(statement):
                return self.web_calls
            return await super().scalar(statement)

    session = CallSession()
    toolbox = Toolbox(session, fetcher)
    stored = _document("page", datetime.now(UTC), "abcdefgh")
    selected = None

    async def newest(_url):
        return selected

    async def process(_raw, url=""):
        return stored.content, None, {}

    async def store_raw(*_args):
        nonlocal selected
        selected = stored
        return stored

    async def record_event(**kwargs):
        if kwargs.get("adapter") == "web_call":
            session.web_calls += 1

    monkeypatch.setattr(toolbox, "_newest_fetchable", newest)
    monkeypatch.setattr(toolbox, "_store_raw", store_raw)
    monkeypatch.setattr(tools, "run_page_processing", process)
    monkeypatch.setattr(tools, "keyed_reader", lambda _url: None)
    monkeypatch.setattr(tools, "record_source_event", record_event)

    first = await toolbox.call("fetch", {"url": stored.url}, 1)
    second = await toolbox.call(
        "fetch", {"url": stored.url, "offset": first["next_offset"]}, 1
    )

    assert first["content"] == "abc"
    assert first["chars_total"] == 8
    assert (first["part"], first["parts_total"]) == (1, 3)
    assert first["next_offset"] == 3
    assert "offset=3" in first["note"]
    assert second["content"] == "def"
    assert (second["part"], second["parts_total"]) == (2, 3)
    assert second["from_store"] is True
    assert first["web_calls_remaining"] == 49
    assert second["web_calls_remaining"] == 49
    assert session.web_calls == 1
    assert fetcher.calls == 1


def test_fetch_offset_past_end_clamps_to_end():
    stored = SimpleNamespace(
        url="https://example.test/page", title="t", content="short", source_name="x",
        source_tier=1, links_to=None, published_at=None, fetched_at=datetime.now(UTC),
    )
    result = Toolbox(_Session(stored), _Fetcher())._window(stored, 99, True)
    assert result["content"] == ""
    assert result["chars_from"] == len(stored.content)
    assert result["chars_to"] == len(stored.content)


@pytest.mark.asyncio
async def test_live_fetch_refetches_and_bypasses_in_run_retrieval_cache(monkeypatch):
    url = "https://example.test/page"
    stored = _document("page", datetime.now(UTC), "stored")
    refreshed = _document("page", datetime.now(UTC), "fresh")
    fetcher = _Fetcher()
    toolbox = Toolbox(_Session(stored), fetcher)
    await _enable_network_fetch(monkeypatch, toolbox, refreshed)

    first = await toolbox._fetch({"url": url}, None)
    assert first["from_store"] is True
    assert fetcher.calls == 0

    result = await toolbox._fetch({"url": url, "live": True}, None)
    assert fetcher.calls == 1
    assert result["content"] == "fresh"
    assert result["from_store"] is False


@pytest.mark.asyncio
async def test_empty_keyed_reader_result_is_a_retrieval_failure(monkeypatch):
    url = "https://searchplatform.rospatent.gov.ru/docs/RU123C1_20260101"
    fetcher = _Fetcher()
    toolbox = Toolbox(_Session(), fetcher)
    events = []

    async def read_empty(_url, _fetcher):
        return "  \n"

    async def record_event(**kwargs):
        events.append(kwargs)

    async def store_raw(*_args):
        pytest.fail("empty keyed-reader content must not be stored")

    monkeypatch.setattr(tools, "keyed_reader", lambda _url: read_empty)
    monkeypatch.setattr(tools, "record_source_event", record_event)
    monkeypatch.setattr(toolbox, "_store_raw", store_raw)
    result = await toolbox._fetch({"url": url}, None)
    assert result["stored"] is False
    assert "retrieval failure" in result["note"]
    assert "empty content" in result["error"]
    assert not any(event.get("ok") for event in events)
    assert fetcher.calls == 0


@pytest.mark.asyncio
async def test_fetcher_decodes_gzip_json_response():
    import gzip
    import json

    import httpx

    from wsignal.parsing.http import Fetcher

    expected = {"message": "compressed response"}
    compressed = gzip.compress(json.dumps(expected).encode("utf-8"))

    def handler(request):
        return httpx.Response(
            200,
            headers={
                "Content-Encoding": "gzip",
                "Content-Length": str(len(compressed)),
            },
            content=compressed,
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = Fetcher(client)
    try:
        response = await fetcher._request("GET", "https://example.test/data")
    finally:
        await fetcher.aclose()
    assert response.json() == expected


@pytest.mark.asyncio
async def test_fetcher_truncates_gzip_response_over_byte_ceiling():
    import gzip

    import httpx

    from wsignal.parsing.http import MAX_RESPONSE_BYTES, Fetcher

    body = b'{"data":"' + b"a" * MAX_RESPONSE_BYTES + b'"}'
    compressed = gzip.compress(body)

    def handler(request):
        return httpx.Response(
            200,
            headers={
                "Content-Encoding": "gzip",
                "Content-Length": str(len(compressed)),
            },
            content=compressed,
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = Fetcher(client)
    try:
        text, truncated = await fetcher.get_text_with_metadata("https://example.test/data")
    finally:
        await fetcher.aclose()
    assert len(text.encode("utf-8")) == MAX_RESPONSE_BYTES
    assert truncated is True
    assert fetcher.last_response_truncated is True


@pytest.mark.asyncio
async def test_fetcher_truncates_and_marks_response_over_byte_ceiling():
    import httpx

    from wsignal.parsing.http import MAX_RESPONSE_BYTES, Fetcher

    class OversizedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"a" * MAX_RESPONSE_BYTES
            yield b"extra"

    def handler(request):
        return httpx.Response(200, stream=OversizedStream(), request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = Fetcher(client)
    try:
        body, truncated = await fetcher.get_text_with_metadata("https://example.test/page")
    finally:
        await fetcher.aclose()
    assert len(body.encode("utf-8")) == MAX_RESPONSE_BYTES
    assert truncated is True
    assert fetcher.last_response_truncated is True


@pytest.mark.asyncio
async def test_fetch_carries_truncation_marker_into_returned_and_stored_content(monkeypatch):
    from wsignal.parsing.http import MAX_RESPONSE_BYTES

    fetcher = _Fetcher()

    async def get_text_with_metadata(_url):
        fetcher.calls += 1
        return "raw page", True

    fetcher.get_text_with_metadata = get_text_with_metadata
    toolbox = Toolbox(_Session(), fetcher)
    stored_content = []

    async def process(_raw, url=""):
        return "extracted page", None, {}

    async def store_raw(url, _host, content, _agent_id, _raw, _metadata):
        stored_content.append(content)
        return _document("page", datetime.now(UTC), content)

    async def record_event(**_kwargs):
        return None

    monkeypatch.setattr(tools, "keyed_reader", lambda _url: None)
    monkeypatch.setattr(tools, "run_page_processing", process)
    monkeypatch.setattr(tools, "record_source_event", record_event)
    monkeypatch.setattr(toolbox, "_store_raw", store_raw)
    result = await toolbox._fetch({"url": "https://example.test/page"}, None)
    marker = f"[Response truncated at {MAX_RESPONSE_BYTES} bytes.]"
    assert marker in result["content"]
    assert marker in stored_content[0]
    assert result["truncated"] is True


@pytest.mark.asyncio
async def test_fetched_at_is_present_on_store_hit():
    stored = _document("page", datetime.now(UTC) - timedelta(hours=719))
    fetcher = _Fetcher()
    result = await Toolbox(_Session(stored), fetcher)._fetch({"url": stored.url}, None)
    assert fetcher.calls == 0
    assert result["from_store"] is True
    assert result["fetched_at"] == stored.fetched_at.isoformat()
    assert result["content"] == "stored"


@pytest.mark.asyncio
async def test_a_copy_older_than_the_reuse_window_is_refetched(monkeypatch):
    stored = _document("page", datetime.now(UTC) - timedelta(hours=721))
    refreshed = _document("page", datetime.now(UTC), "fresh content")
    fetcher = _Fetcher()
    toolbox = Toolbox(_Session(stored), fetcher)
    await _enable_network_fetch(monkeypatch, toolbox, refreshed)
    result = await toolbox._fetch({"url": stored.url}, None)
    assert fetcher.calls == 1
    assert result["from_store"] is False
    assert result["content"] == "fresh content"


@pytest.mark.asyncio
async def test_a_copy_within_the_reuse_window_is_reused():
    from wsignal.config import get_settings

    max_age = get_settings().document_max_age_hours
    stored = _document("page", datetime.now(UTC) - timedelta(hours=max_age - 0.01))
    fetcher = _Fetcher()
    result = await Toolbox(_Session(stored), fetcher)._fetch({"url": stored.url}, None)
    assert fetcher.calls == 0
    assert result["from_store"] is True
