from datetime import date

import pytest

from wsignal.config import COMPOSITE_QUOTA, METERED_ADAPTERS, Settings
from wsignal.parsing.brave import BraveSource, freshness, parse_results
from wsignal.parsing.dates import subtract_months
from wsignal.parsing.registry import ADAPTER_DATE_TYPE, COMPOSITES, SOURCES


def test_web_search_runs_on_brave_and_not_on_the_challenged_provider():
    assert COMPOSITES["web_search"] == ("brave",)
    assert "ddg" not in COMPOSITES["web_search"]
    assert "ddg" not in COMPOSITES["discovery"]


def test_brave_is_registered():
    assert "brave" in SOURCES
    assert SOURCES["brave"].host == "api.search.brave.com"


def test_automatic_allowance_is_preserved_alongside_the_metered_quota():
    assert COMPOSITE_QUOTA["brave"] == 3
    assert "brave" in METERED_ADAPTERS
    settings = Settings()
    for role in ("researcher", "refuter", "orchestrator", "assistant"):
        assert settings.quota_for("brave", role) == getattr(
            settings, "quota_brave_orchestrator" if role == "orchestrator" else "quota_brave"
        ), role


def test_brave_search_dates_are_recorded_as_published_candidates():
    assert ADAPTER_DATE_TYPE["brave"] == "none"
    parsed = parse_results(
        {
            "web": {
                "results": [
                    {
                        "url": "https://example.com/a",
                        "title": "A",
                        "page_age": "2025-06-19T12:00:00Z",
                    }
                ]
            }
        }
    )
    assert parsed[0][3] == date(2025, 6, 19)


def test_brave_age_is_used_only_when_it_is_an_absolute_date():
    result = parse_results(
        {
            "web": {
                "results": [
                    {"url": "https://example.com/a", "title": "A", "age": "2 days ago"},
                    {"url": "https://example.com/b", "title": "B", "age": "2025-06-18"},
                ]
            }
        }
    )
    assert result[0][3] is None
    assert result[1][3] == date(2025, 6, 18)


@pytest.mark.asyncio
async def test_brave_document_carries_page_age_through_harvest(monkeypatch):
    import wsignal.parsing.brave as brave
    from wsignal.config import Settings

    settings = Settings(_env_file=None, brave_api_key="key")
    monkeypatch.setattr(brave, "get_settings", lambda: settings)
    calls = []

    class Fetcher:
        async def request_json(self, *args, **kwargs):
            calls.append((args, kwargs))
            return {
                "web": {
                    "results": [
                        {
                            "url": "https://example.com/a",
                            "title": "A",
                            "page_age": "2025-06-19",
                        }
                    ]
                }
            }

    from wsignal.parsing.base import Surface

    result = await BraveSource().harvest(
        Surface(adapter="brave", query="q", limit=1), Fetcher()
    )
    assert result.documents[0].published_at == date(2025, 6, 19)
    assert result.documents[0].date_source == "search"
    assert calls[0][0][1] == brave.ENDPOINT
    assert calls[0][1]["headers"]["X-Subscription-Token"] == "key"


@pytest.mark.asyncio
async def test_brave_without_a_key_goes_through_the_relay_without_one(monkeypatch):
    import wsignal.parsing.brave as brave
    from wsignal.config import Settings
    from wsignal.parsing.base import Surface

    settings = Settings(_env_file=None, brave_api_key="", relay_url="https://relay.test")
    monkeypatch.setattr(brave, "get_settings", lambda: settings)
    calls = []

    class Fetcher:
        async def request_json(self, *args, **kwargs):
            calls.append((args, kwargs))
            return {"web": {"results": []}}

    await BraveSource().harvest(Surface(adapter="brave", query="q", limit=1), Fetcher())
    assert calls[0][0][1] == "https://relay.test/v1/src/brave/web/search"
    assert "X-Subscription-Token" not in calls[0][1]["headers"]


@pytest.mark.asyncio
async def test_brave_in_local_mode_without_a_key_is_off(monkeypatch):
    import wsignal.parsing.brave as brave
    from wsignal.config import Settings
    from wsignal.parsing.base import Surface

    settings = Settings(_env_file=None, brave_api_key="", mode="local")
    monkeypatch.setattr(brave, "get_settings", lambda: settings)
    with pytest.raises(brave.BraveUnauthorised):
        await BraveSource().harvest(Surface(adapter="brave", query="q", limit=1), object())


@pytest.mark.parametrize(
    "today,months,expected_start,expected_window",
    [
        (date(2026, 9, 18), 24, date(2024, 9, 18), "2024-09-18to2026-09-18"),
        (date(2026, 8, 18), 1, date(2026, 7, 18), "2026-07-18to2026-08-18"),
        (date(2026, 3, 31), 1, date(2026, 2, 28), "2026-02-28to2026-03-31"),
        (date(2024, 3, 31), 1, date(2024, 2, 29), "2024-02-29to2024-03-31"),
        (date(2026, 1, 31), 13, date(2024, 12, 31), "2024-12-31to2026-01-31"),
        (date(2026, 9, 18), 0, date(2026, 9, 18), None),
        (date(2026, 9, 18), -3, date(2026, 9, 18), None),
    ],
)
def test_brave_window_clamps_calendar_months_and_handles_nonpositive_windows(
    today, months, expected_start, expected_window
):
    assert subtract_months(today, months) == expected_start
    assert freshness(months, today) == expected_window


def test_results_parse_with_their_extra_snippets():
    payload = {
        "web": {
            "results": [
                {
                    "title": "Agent IAM is not a category yet",
                    "url": "https://example.com/a",
                    "description": "One sentence fragment",
                    "extra_snippets": ["second excerpt", "third excerpt"],
                }
            ]
        }
    }
    parsed = parse_results(payload)
    assert parsed == [
        (
            "https://example.com/a",
            "Agent IAM is not a category yet",
            "One sentence fragment second excerpt third excerpt",
            None,
        )
    ]


def test_a_malformed_or_empty_payload_is_not_a_crash():
    assert parse_results({}) == []
    assert parse_results({"web": None}) == []
    assert parse_results({"web": {"results": "nonsense"}}) == []
    assert parse_results(None) == []
    assert parse_results({"web": {"results": [{"url": "ftp://x", "title": "t"}]}}) == []
    assert parse_results({"web": {"results": [{"url": "https://x", "title": ""}]}}) == []
