import pytest

from wsignal.inference.tools import _denominator
from wsignal.parsing import epo, rospatent
from wsignal.parsing.base import HarvestResult, Surface


class FakeFetcher:
    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.calls = []

    async def request_json(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.payloads.pop(0)


EPO_PAYLOAD = {
    "ops:world-patent-data": {
        "ops:biblio-search": {
            "@total-result-count": "2580",
            "ops:search-result": {
                "exchange-documents": {
                    "exchange-document": {
                        "@country": "EP",
                        "@doc-number": "1000000",
                        "@kind": "B1",
                        "bibliographic-data": {
                            "invention-title": {"@lang": "en", "$": "A THING"}
                        },
                    }
                }
            },
        }
    }
}


def test_epo_reads_the_total_off_the_response():
    assert epo._total(EPO_PAYLOAD) == 2580
    assert epo._total({}) is None
    assert epo._total({"ops:world-patent-data": {"ops:biblio-search": {}}}) is None


@pytest.mark.asyncio
async def test_epo_harvest_carries_the_denominator(monkeypatch):
    monkeypatch.setattr(
        epo._TOKEN, "get", lambda fetcher: _immediately("token"), raising=False
    )
    result = await epo.EpoSource().harvest(
        Surface(adapter="epo", query="tensor network", limit=1),
        FakeFetcher(EPO_PAYLOAD),
    )
    assert isinstance(result, HarvestResult)
    assert result.total == 2580
    assert len(result.documents) == 1


def _immediately(value):
    async def done():
        return value

    return done()


ROSPATENT_PAYLOAD = {
    "total": 3241,
    "available": 1000,
    "hits": [
        {
            "id": "RU2863963C1_20260615",
            "common": {"publication_date": "2026.06.15"},
            "snippet": {"title": "Способ", "description": "описание"},
        }
    ],
}


class FakeSettings:
    rospatent_api_key = "key"


@pytest.mark.asyncio
async def test_rospatent_harvest_carries_total_and_the_paging_cap(monkeypatch):
    monkeypatch.setattr(rospatent, "get_settings", lambda: FakeSettings())
    fetcher = FakeFetcher(ROSPATENT_PAYLOAD)
    result = await rospatent.RospatentSource().harvest(
        Surface(adapter="rospatent", query="сжатие", limit=101), fetcher
    )
    assert isinstance(result, HarvestResult)
    assert result.total == 3241
    assert result.available == 1000
    method, url, kwargs = fetcher.calls[0]
    assert method == "POST"
    assert url == rospatent.SEARCH_URL
    assert kwargs["json"]["limit"] == 100
    assert kwargs["json"]["offset"] == 0


def test_the_paging_cap_is_reported_only_when_it_is_not_the_total():
    capped = _denominator(
        HarvestResult(surface=None, total=3241, available=1000)
    )
    assert capped["total_matched"] == 3241
    assert capped["available_to_page"] == 1000
    assert "Quote total_matched" in capped["available_note"]

    uncapped = _denominator(HarvestResult(surface=None, total=7, available=7))
    assert uncapped == {"total_matched": 7}


def test_a_source_that_cannot_count_is_not_made_to_invent_a_number():
    assert _denominator(HarvestResult(surface=None)) == {}


def test_zero_hits_with_a_known_total_is_a_different_finding_from_zero_hits():
    assert _denominator(HarvestResult(surface=None, total=0)) == {"total_matched": 0}
