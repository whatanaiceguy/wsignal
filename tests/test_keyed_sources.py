from datetime import date

import pytest

import wsignal.inference.tools as tools
from wsignal.config import COMPOSITE_QUOTA, METERED_ADAPTERS, Settings
from wsignal.inference.tools import Toolbox
from wsignal.parsing import epo, rospatent, x
from wsignal.parsing.base import TIER_AUTHORITATIVE, TIER_SOCIAL, Surface
from wsignal.parsing.hn import HackerNewsSource
from wsignal.parsing.openalex import OpenAlexSource
from wsignal.parsing.registry import ADAPTER_DATE_TYPE, COMPOSITES, SOURCES

EPO_DOC = {
    "exchange-document": {
        "@country": "WO",
        "@doc-number": "2026185975",
        "@kind": "A1",
        "bibliographic-data": {
            "publication-reference": {
                "document-id": [
                    {
                        "@document-id-type": "docdb",
                        "country": {"$": "WO"},
                        "doc-number": {"$": "2026185975"},
                        "kind": {"$": "A1"},
                        "date": {"$": "20260910"},
                    },
                    {
                        "@document-id-type": "epodoc",
                        "doc-number": {"$": "WO2026185975"},
                        "date": {"$": "20260910"},
                    },
                ]
            },
            "invention-title": {"@lang": "en", "$": "NEUROMORPHIC ARITHMETIC DEVICE"},
        },
        "abstract": {"p": {"$": "A device that does the thing."}},
    }
}


def _epo_payload(documents):
    return {
        "ops:world-patent-data": {
            "ops:biblio-search": {
                "@total-result-count": "886",
                "ops:search-result": {"exchange-documents": documents},
            }
        }
    }


def test_epo_reads_a_single_result_that_arrived_as_an_object():
    assert len(epo._documents(_epo_payload(EPO_DOC))) == 1
    assert len(epo._documents(_epo_payload([EPO_DOC, EPO_DOC]))) == 2


def test_epo_survives_a_response_that_is_not_the_shape_at_all():
    assert epo._documents({}) == []
    assert epo._documents({"ops:world-patent-data": None}) == []
    assert epo._documents("<html>a block page</html>") == []


def test_epo_builds_a_document_a_juror_can_open():
    document = epo._one(EPO_DOC, "epo")
    assert document is not None
    assert document.url == (
        "https://worldwide.espacenet.com/patent/search?q=pn%3DWO2026185975A1"
    )
    assert document.title == "NEUROMORPHIC ARITHMETIC DEVICE"
    assert "A device that does the thing." in document.text
    assert document.source_type == "patent"
    assert document.source_tier == TIER_AUTHORITATIVE
    assert document.published_at == date(2026, 9, 10)


def test_epo_takes_the_publication_date_and_not_whichever_date_comes_first():
    biblio = EPO_DOC["exchange-document"]["bibliographic-data"]
    assert epo._published(biblio) == date(2026, 9, 10)
    assert epo._published({}) is None


def test_epo_without_a_country_or_number_is_not_a_document():
    assert epo._one({"exchange-document": {"@country": "WO"}}, "epo") is None


ROSPATENT_HIT = {
    "id": "RU2863963C1_20260615",
    "common": {
        "publishing_office": "RU",
        "document_number": "2863963",
        "kind": "C1",
        "publication_date": "2026.06.15",
    },
    "snippet": {
        "title": "Способ <em>мультипараметрической</em> диагностики",
        "description": "Так для приборной реализации <em>нейроморфных</em> процессоров",
        "patentee": "АО «Ромашка»",
    },
}


@pytest.mark.parametrize(
    "value,expected",
    [
        ("2026.06.15", date(2026, 6, 15)),
        ("20260615", date(2026, 6, 15)),
        ("2026-06-15", date(2026, 6, 15)),
        ("1899.12.31", None),
        (f"{date.today().year + 2}.01.01", None),
        ("", None),
        (None, None),
        ("2026.13.99", None),
    ],
)
def test_rospatent_reads_both_date_forms(value, expected):
    assert rospatent._date(value) == expected


def test_rospatent_strips_the_highlight_tags_out_of_a_quotable_span():
    assert "<em>" not in rospatent._plain(ROSPATENT_HIT["snippet"]["description"])
    assert "нейроморфных" in rospatent._plain(ROSPATENT_HIT["snippet"]["description"])


def test_rospatent_builds_a_document():
    document = rospatent._one(ROSPATENT_HIT, "rospatent")
    assert document is not None
    assert document.url.endswith("/docs/RU2863963C1_20260615")
    assert document.published_at == date(2026, 6, 15)
    assert document.source_lang == "ru"
    assert document.source_type == "patent"
    assert "<em>" not in document.text


@pytest.mark.asyncio
async def test_rospatent_search_rejects_missing_hits_key(monkeypatch):
    monkeypatch.setattr(
        rospatent,
        "get_settings",
        lambda: type("Settings", (), {"rospatent_api_key": "key"})(),
    )

    class Fetcher:
        async def request_json(self, *_args, **_kwargs):
            return {}

    with pytest.raises(ValueError, match="hits list"):
        await rospatent.RospatentSource().harvest(
            Surface(adapter="rospatent", query="q", limit=1), Fetcher()
        )


@pytest.mark.parametrize(
    "source,payload,expected_error",
    [
        (OpenAlexSource(), {}, "results list"),
        (HackerNewsSource(), {}, "hits list"),
    ],
)
@pytest.mark.asyncio
async def test_json_adapters_reject_missing_result_keys(source, payload, expected_error):
    class Fetcher:
        async def get_json(self, *_args, **_kwargs):
            return payload

    with pytest.raises(ValueError, match=expected_error):
        await source.harvest(Surface(adapter=source.name, query="q"), Fetcher())


def test_rospatent_without_an_id_is_not_a_document():
    assert rospatent._one({"common": {}}, "rospatent") is None


def test_rospatent_window_is_the_expected_month_start(monkeypatch):
    class FrozenDate:
        @classmethod
        def today(cls):
            return date(2026, 9, 18)

    monkeypatch.setattr(rospatent, "date", FrozenDate)
    assert rospatent._window_start(24) == "20240901"


X_PAYLOAD = {
    "data": [
        {
            "id": "1000000000000000002",
            "author_id": "1000000000000000001",
            "created_at": "2026-09-17T22:06:21.000Z",
            "lang": "en",
            "text": "Neuromorphic chips are showing up in EDA libraries.",
            "public_metrics": {
                "like_count": 4,
                "retweet_count": 1,
                "reply_count": 0,
                "impression_count": 294,
            },
        }
    ],
    "includes": {
        "users": [{"id": "1000000000000000001", "username": "example_user", "name": "Example"}]
    },
}


def test_x_resolves_the_author_through_the_expansion():
    document = x._one(X_PAYLOAD["data"][0], x._authors(X_PAYLOAD), "x")
    assert document is not None
    assert document.url == "https://x.com/example_user/status/1000000000000000002"
    assert document.source_name == "X / @example_user"
    assert document.published_at == date(2026, 9, 17)
    assert document.source_tier == TIER_SOCIAL


def test_a_missing_expansion_costs_the_name_and_not_the_link():
    document = x._one(X_PAYLOAD["data"][0], {}, "x")
    assert document is not None
    assert document.url == "https://x.com/i/status/1000000000000000002"


def test_x_carries_reach_because_smallness_needs_a_denominator():
    document = x._one(X_PAYLOAD["data"][0], x._authors(X_PAYLOAD), "x")
    assert "impression_count 294" in document.text


@pytest.mark.asyncio
async def test_x_rejects_missing_data_key(monkeypatch):
    monkeypatch.setattr(x, "get_settings", lambda: type("S", (), {"x_bearer_token": "key"})())

    class Fetcher:
        async def request_json(self, *_args, **_kwargs):
            return {}

    with pytest.raises(ValueError, match="data list"):
        await x.XSource().harvest(Surface(adapter="x", query="q", limit=2), Fetcher())


def test_x_without_text_is_not_a_document():
    assert x._one({"id": "1"}, {}, "x") is None


@pytest.mark.asyncio
async def test_x_harvest_never_exceeds_requested_limit(monkeypatch):
    monkeypatch.setattr(x, "get_settings", lambda: type("S", (), {"x_bearer_token": "key"})())

    class Fetcher:
        async def request_json(self, *_args, **_kwargs):
            return {"data": [dict(X_PAYLOAD["data"][0], id=str(i)) for i in range(25)]}

    documents = await x.XSource().harvest(
        Surface(adapter="x", query="q", limit=3),
        Fetcher(),
    )
    assert len(documents) == 3


def test_every_metered_adapter_exists_and_says_what_its_dates_mean():
    for adapter in METERED_ADAPTERS:
        assert adapter in SOURCES, adapter
        assert ADAPTER_DATE_TYPE.get(adapter) not in (None, "unknown"), adapter


def test_the_discovery_fan_out_never_reaches_a_metered_source():
    assert not set(COMPOSITES["discovery"]) & set(METERED_ADAPTERS)


@pytest.mark.parametrize(
    "adapter,role",
    [
        ("epo", "researcher"),
        ("epo", "refuter"),
        ("epo", "orchestrator"),
        ("rospatent", "researcher"),
        ("rospatent", "orchestrator"),
        ("x", "researcher"),
        ("x", "refuter"),
        ("x", "assistant"),
        ("x", "orchestrator"),
    ],
)
def test_the_quota_is_per_role(adapter, role):
    settings = Settings()
    expected = getattr(
        settings,
        f"quota_{adapter}_orchestrator" if role == "orchestrator" else f"quota_{adapter}",
    )
    assert settings.quota_for(adapter, role) == expected


def test_an_unmetered_adapter_has_no_ceiling_at_all():
    for adapter in ("ddg", "openalex", "arxiv", "hn", "store", "discovery"):
        assert Settings().quota_for(adapter, "researcher") is None


class _QuotaSession:
    def __init__(self, role, direct_used, automatic_used=0):
        self.role = role
        self.direct_used = direct_used
        self.automatic_used = automatic_used

    async def scalar(self, statement):
        clause = str(statement)
        if "SELECT agents.role" in clause:
            return self.role
        assert "source_events.agent_id" in clause
        assert "source_events.adapter" in clause
        if "source_events.via IS NOT NULL" in clause:
            return self.automatic_used
        return self.direct_used


class _QuotaFetcher:
    def queue_estimate(self, _host):
        return 0.0


@pytest.mark.asyncio
@pytest.mark.parametrize("at_limit", [False, True])
async def test_direct_metered_search_refuses_at_role_allowance(monkeypatch, at_limit):
    settings = Settings()
    allowed = settings.quota_for("brave", "researcher")
    used = allowed if at_limit else allowed - 1
    monkeypatch.setattr(tools, "get_settings", lambda: settings)
    toolbox = Toolbox(_QuotaSession("researcher", used), _QuotaFetcher())
    calls = 0

    async def harvest(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        from wsignal.parsing.base import HarvestResult

        return HarvestResult(surface=None)

    monkeypatch.setattr(toolbox, "_harvest", harvest)
    result = await toolbox._search({"adapter": "brave", "query": "q"}, 7)
    if at_limit:
        assert result["error"] == "quota spent for 'brave'"
        assert result["used"] == allowed
        assert calls == 0
    else:
        assert "error" not in result
        assert calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("at_limit", [False, True])
async def test_composite_member_refuses_at_direct_role_allowance(monkeypatch, at_limit):
    settings = Settings()
    allowed = settings.quota_for("brave", "researcher")
    used = allowed if at_limit else allowed - 1
    monkeypatch.setattr(tools, "get_settings", lambda: settings)
    toolbox = Toolbox(_QuotaSession("researcher", used, 0), _QuotaFetcher())
    harvested = []

    async def harvest(adapter, *_args, **_kwargs):
        harvested.append(adapter)
        from wsignal.parsing.base import HarvestResult

        return HarvestResult(surface=None)

    monkeypatch.setattr(toolbox, "_harvest", harvest)
    result = await toolbox._search_composite("web_search", {"query": "q"}, 7)
    if at_limit:
        assert "brave" in result["sources_not_asked"]
        assert "used your allowance" in result["sources_not_asked"]["brave"]
        assert harvested == []
    else:
        assert result["sources_answered"] == ["brave"]
        assert harvested == ["brave"]


@pytest.mark.asyncio
@pytest.mark.parametrize("at_limit", [False, True])
async def test_composite_member_automatic_allowance_is_enforced(monkeypatch, at_limit):
    settings = Settings()
    automatic_allowed = COMPOSITE_QUOTA["brave"]
    automatic_used = automatic_allowed if at_limit else automatic_allowed - 1
    direct_allowed = settings.quota_for("brave", "researcher")
    session = _QuotaSession("researcher", direct_allowed - 1, automatic_used)
    monkeypatch.setattr(tools, "get_settings", lambda: settings)
    toolbox = Toolbox(session, _QuotaFetcher())
    harvested = []

    async def harvest(adapter, *_args, **_kwargs):
        harvested.append(adapter)
        from wsignal.parsing.base import HarvestResult

        return HarvestResult(surface=None)

    monkeypatch.setattr(toolbox, "_harvest", harvest)
    result = await toolbox._search_composite("web_search", {"query": "q"}, 7)
    if at_limit:
        assert result["sources_not_asked"]["brave"].startswith("automatic allowance spent")
        assert harvested == []
    else:
        assert result["sources_answered"] == ["brave"]
        assert harvested == ["brave"]
