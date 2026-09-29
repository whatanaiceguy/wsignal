import asyncio

from wsignal.parsing.base import Surface
from wsignal.parsing.ddg import DuckDuckGoLiteSource, parse_results

DCD = (
    "https%3A%2F%2Fwww.datacenterdynamics.com%2Fen%2Fnews"
    "%2Fneuromorphic%2Dcompute%2Dstartup%2Draises%2D475m%2F"
)
EUS = "https%3A%2F%2Feu-startups.com%2F2026%2F03%2Fspiking-nets%2F"


def _anchor(encoded: str, rut: str, title: str) -> str:
    return (
        '<a rel="nofollow" '
        f'href="//duckduckgo.com/l/?uddg={encoded}&amp;rut={rut}" '
        f"class='result-link'>{title}</a>"
    )


_SNIPPET_ONE = (
    "Brain-inspired AI startup has raised $475 million in its "
    "<b>seed</b> <b>funding</b> round."
)

FIXTURE = f"""
<table border="0">
  <tr>
    <td valign="top">1.&nbsp;</td>
    <td>
      {_anchor(DCD, "23b46322", "Neuromorphic compute startup raises $475m")}
    </td>
  </tr>
  <tr>
    <td>&nbsp;&nbsp;&nbsp;</td>
    <td class='result-snippet'>{_SNIPPET_ONE}</td>
  </tr>
  <tr>
    <td valign="top">2.&nbsp;</td>
    <td>
      {_anchor(EUS, "ffff1111", "Spiking networks &amp; the edge")}
    </td>
  </tr>
</table>
"""


def test_extracts_the_real_url_not_the_redirect():
    results = parse_results(FIXTURE)
    assert len(results) == 2
    assert results[0][0] == (
        "https://www.datacenterdynamics.com/en/news/"
        "neuromorphic-compute-startup-raises-475m/"
    )
    assert results[1][0] == "https://eu-startups.com/2026/03/spiking-nets/"
    assert not any("duckduckgo.com/l/" in url for url, _, _ in results)


def test_href_quotes_must_match():
    body = '<a class="result-link" href="https://good.test/\'bad">ok</a>'
    assert parse_results(body) == []


def test_titles_and_snippets_are_plain_text():
    results = parse_results(FIXTURE)
    assert results[0][1] == "Neuromorphic compute startup raises $475m"
    assert "<b>" not in results[0][2]
    assert "seed funding round" in results[0][2]
    assert results[1][1] == "Spiking networks & the edge"


def test_a_result_without_a_snippet_does_not_steal_the_next_one():
    results = parse_results(FIXTURE)
    assert results[1][2] == ""


def test_harvest_distinguishes_anomaly_from_a_real_empty_results_page():
    import pytest

    from wsignal.parsing.ddg import DuckDuckGoChallenged

    async def run(body):
        fetcher = _StubFetcher(body)
        surface = Surface(adapter="ddg", query="neuromorphic", window_months=24)
        return await DuckDuckGoLiteSource().harvest(surface, fetcher)

    with pytest.raises(DuckDuckGoChallenged):
        asyncio.run(run("<html><body>If this error persists...</body></html>"))
    assert asyncio.run(run('<html><form><input name="q"></form><p>no results</p></html>')) == []


class _StubFetcher:
    def __init__(self, body: str) -> None:
        self.body = body
        self.calls: list[tuple[str, dict]] = []

    async def get_text(self, url: str, params=None) -> str:
        self.calls.append((url, dict(params or {})))
        return self.body


def _harvest(query: str, months: int, limit: int = 20):
    fetcher = _StubFetcher(FIXTURE)
    surface = Surface(adapter="ddg", query=query, window_months=months, limit=limit)
    documents = asyncio.run(DuckDuckGoLiteSource().harvest(surface, fetcher))
    return documents, fetcher


def test_documents_are_leads_and_never_claim_to_be_the_page():
    documents, _ = _harvest("neuromorphic", 24)
    assert len(documents) == 2
    for document in documents:
        assert document.retrieved is False
        assert document.adapter == "ddg"
        assert document.published_at is None
        assert document.may_create_technology is False
    assert documents[0].source_name == "datacenterdynamics.com"
    assert documents[1].source_name == "eu-startups.com"


def test_window_maps_onto_the_coarse_df_filter():
    _, wide = _harvest("x", 24)
    assert "df" not in wide.calls[0][1]
    _, year = _harvest("x", 12)
    assert year.calls[0][1]["df"] == "y"
    _, month = _harvest("x", 1)
    assert month.calls[0][1]["df"] == "m"


def test_empty_query_costs_no_request():
    documents, fetcher = _harvest("   ", 24)
    assert documents == []
    assert fetcher.calls == []
