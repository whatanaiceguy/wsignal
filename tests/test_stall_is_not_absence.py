import pytest

from wsignal.inference.tools import Toolbox
from wsignal.parsing.base import HarvestResult, Surface

STALL = "stalled: no answer within 5s including queue time. Not an empty result."


class FakeFetcher:
    def queue_estimate(self, host):
        return 0.0


def stalled(*args, **kwargs):
    async def run():
        return HarvestResult(surface=Surface(adapter="arxiv", query="q"), error=STALL)

    return run()


@pytest.mark.asyncio
async def test_a_stalled_adapter_is_not_reported_as_a_measurement(monkeypatch):
    monkeypatch.setattr(Toolbox, "_harvest", stalled)
    payload = await Toolbox(None, None)._search(
        {"adapter": "arxiv", "query": "neuromorphic"}, None
    )
    assert payload["error"] == STALL
    assert "NOT a measurement" in payload["note"]
    assert "count" not in payload


@pytest.mark.asyncio
async def test_a_stalled_member_does_not_count_as_a_source_that_answered(monkeypatch):
    monkeypatch.setattr(Toolbox, "_harvest", stalled)
    toolbox = Toolbox(None, FakeFetcher())
    payload = await toolbox._search_composite(
        "web_search", {"query": "neuromorphic"}, None
    )
    assert payload["sources_answered"] == []
    assert set(payload["sources_failed"]) == {"brave"}
    assert payload["note"].startswith("NOT A MEASUREMENT")
