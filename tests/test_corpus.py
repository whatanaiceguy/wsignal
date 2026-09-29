import json
from datetime import date
from types import SimpleNamespace

import httpx
import pytest

from wsignal.config import Settings
from wsignal.corpus import Corpus, entry_term, get_corpus, month_window
from wsignal.db import get_session
from wsignal.interface.app import app


@pytest.mark.parametrize(("en", "ru", "expected"), [
    ("Co-packaged optics (CPO)", "Оптика", "Co-packaged optics OR CPO"),
    ("High bandwidth memory (HBM, stacked memory)", "Память", "High bandwidth memory OR HBM"),
    ("CPO (co-packaged optics)", "Оптика", "CPO"),
    (None, "Фотонные вычисления (оптические процессоры)", "Фотонные вычисления"),
    (" ", "Память HBM (пояснение)", "HBM"),
    ("Optics (explanation (nested))", "Оптика", "Optics"),
])
def test_entry_terms_keep_abbreviation_alternatives(en, ru, expected):
    assert entry_term(en, ru) == expected


def test_corpus_url_reads_the_unprefixed_environment(monkeypatch):
    monkeypatch.setenv("CORPUS_URL", "https://corpus.own")
    assert Settings(_env_file=None).corpus_url == "https://corpus.own"


def series_payload(terms):
    return {"series": [
        {"months": [{"m": "2026-01", "n": 0, "total": 100}, {"m": "2026-02", "n": 3, "total": 200}],
         "matched_total": 3, "too_broad": False}
        for _ in terms
    ]}


def fake_corpus(handler) -> Corpus:
    corpus = Corpus("https://corpus.test/v1/corpus")
    corpus._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return corpus


@pytest.fixture
def api_overrides():
    yield
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_corpus_endpoints_pass_terms_to_the_corpus_server(api_overrides):
    sent = []

    def handler(request):
        payload = json.loads(request.content)
        sent.append(payload)
        return httpx.Response(200, json=series_payload(payload["terms"]))

    app.dependency_overrides[get_corpus] = lambda: fake_corpus(handler)

    class Session:
        async def get(self, _model, run_id):
            return object() if run_id == 7 else None

        async def scalars(self, statement):
            assert "entries.run_id" in str(statement)
            assert "entries.weak_score DESC" in str(statement)
            return SimpleNamespace(all=lambda: [
                SimpleNamespace(
                    id=31, name_en="Co-packaged optics (CPO)", name_ru="Оптика",
                    corpus_query=None,
                ),
                SimpleNamespace(
                    id=30, name_en=None, name_ru="Фотонные вычисления (оптика)",
                    corpus_query=None,
                ),
            ])

    app.dependency_overrides[get_session] = Session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        result = await client.get("/api/corpus/series", params={"q": "HBM", "months": 2})
        assert result.status_code == 200
        body = result.json()
        assert (body["q"], body["matched_total"], body["too_broad"]) == ("HBM", 3, False)
        assert len(body["months"]) == 2
        assert sent[-1] == {"terms": ["HBM"], "months": 2}
        result = await client.get("/api/search/7/corpus?months=2")
        assert result.status_code == 200
        assert [
            (row["entry_id"], row["entry_rank"], row["term"]) for row in result.json()
        ] == [
            (31, 1, "Co-packaged optics OR CPO"), (30, 2, "Фотонные вычисления"),
        ]
        assert sent[-1]["terms"] == ["Co-packaged optics OR CPO", "Фотонные вычисления"]
        assert (await client.get("/api/search/999/corpus")).status_code == 404
        for params in [
            {"q": " "}, {"q": ""}, {"q": "HBM", "months": 0},
            {"q": "HBM", "months": 121}, {},
        ]:
            assert (await client.get("/api/corpus/series", params=params)).status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unset", "unreachable", "server", "timeout"])
async def test_unavailable_corpus_returns_russian_503_and_leaves_history_working(
    api_overrides, failure,
):
    def handler(request):
        if failure == "unreachable":
            raise httpx.ConnectError("private host", request=request)
        if failure == "timeout":
            raise httpx.ReadTimeout("private timeout", request=request)
        return httpx.Response(500, text="private database error")

    corpus = Corpus("") if failure == "unset" else fake_corpus(handler)
    app.dependency_overrides[get_corpus] = lambda: corpus

    class Session:
        async def get(self, *_args):
            return object()

        async def scalars(self, _statement):
            return SimpleNamespace(all=lambda: [])

    app.dependency_overrides[get_session] = Session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        result = await client.get("/api/corpus/series?q=HBM")
        assert result.status_code == 503
        assert result.json()["detail"].startswith(("Корпус", "Запрос к корпусу"))
        assert "private" not in result.text
        assert (await client.get("/api/runs")).json() == []


def test_month_window_crosses_year_and_includes_current_month():
    assert month_window(3, date(2026, 1, 15)) == [
        date(2025, 11, 1), date(2025, 12, 1), date(2026, 1, 1), date(2026, 2, 1),
    ]


@pytest.mark.parametrize(("name", "expected"), [
    ("Photonic/optical processors for AI inference",
     "Photonic processors OR optical processors"),
    ("MCP server security scanners and tool poisoning protection",
     "MCP security OR tool poisoning"),
    ("SSD (solid-state drives) — KV-cache/context-memory tier for AI inference", "SSD"),
    ("Optical circuit switching (OCS) in AI factories", "Optical circuit switching OR OCS"),
    ("Bank branches in the metaverse", "Bank branches metaverse"),
    ("Co-packaged optics (CPO): оптика в одном корпусе", "Co-packaged optics OR CPO"),
    ("Память HBM для ИИ-ускорителей", "HBM"),
    ("Optics for AI ML IT API KV HTTP", "Optics"),
    ("Optics (AI)", "Optics"),
    ("Phase change memory (PCM) for AI", "Phase change memory OR PCM"),
])
def test_legacy_technology_queries(name, expected):
    assert entry_term(name, name) == expected
