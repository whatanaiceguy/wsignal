import json
from types import SimpleNamespace

from wsignal.inference.tools import _search_limit, render_tool_result


def test_search_limit_uses_settings_default_and_honors_explicit_limit(monkeypatch):
    monkeypatch.setattr(
        "wsignal.inference.tools.get_settings",
        lambda: SimpleNamespace(
            search_default_limit=8, search_max_limit=20, search_snippet_chars=200
        ),
    )

    assert _search_limit({}) == 8
    assert _search_limit({"limit": 13}) == 13
    assert _search_limit({"limit": 99}) == 20


def test_search_rendering_compacts_records_and_header(monkeypatch):
    monkeypatch.setattr(
        "wsignal.inference.tools.get_settings",
        lambda: SimpleNamespace(
            search_default_limit=8, search_max_limit=20, search_snippet_chars=200
        ),
    )
    records = [
        {
            "url": f"https://example.test/research/{index}",
            "rank": index + 1,
            "title": f"Signal research {index}",
            "snippet": (
                "**Quantum batteries** improve stored energy for practical systems."
            ),
            "found_by": None,
            "links_to": None,
            "date_type": "published",
            "retrieved": index % 2 == 0,
            "source_name": f"Source {index}",
            "source_tier": 1,
            "published_at": f"2026-0{index % 8 + 1}-15",
        }
        for index in range(20)
    ]
    payload = {
        "query": "quantum batteries",
        "adapter": "store",
        "date_type": "published",
        "count": 20,
        "matched_total": 34,
        "match_mode": "any_term",
        "match_note": "A long paragraph with internal matching detail.",
        "records": records,
    }

    result = json.loads(
        render_tool_result(payload, "search", {"query": "quantum batteries"})
    )

    assert result["shown"] == 8
    assert result["matched"] == 34
    assert result["date_semantics"] == "publisher publication date"
    assert result["match"] == "Loose match: records may match any query term."
    assert "match_note" not in result
    for record in result["records"]:
        assert set(record) == {
            "number", "title", "source_name", "date", "page_stored", "url", "snippet"
        }
        assert len(record["snippet"]) <= 200
        assert "**" not in record["snippet"]
        assert "quantum batteries" in record["snippet"].casefold()
        assert len(record["snippet"]) < len(records[0]["snippet"])
    assert len(render_tool_result(payload, "search", {"query": "quantum batteries"})) < 2000


def test_explicit_limit_and_web_search_result_adapter_detection(monkeypatch):
    monkeypatch.setattr(
        "wsignal.inference.tools.get_settings",
        lambda: SimpleNamespace(
            search_default_limit=8, search_max_limit=20, search_snippet_chars=200
        ),
    )
    payload = {
        "adapter": "brave",
        "query": "test",
        "count": 12,
        "records": [
            {
                "title": f"Result {index}",
                "url": f"https://example.test/{index}",
                "source_name": "Example",
                "retrieved": False,
                "snippet": "A short result.",
                "rank": index,
                "found_by": None,
                "links_to": None,
                "date_type": "none",
                "source_tier": 0,
            }
            for index in range(12)
        ],
    }

    result = json.loads(
        render_tool_result(payload, "web_search", {"query": "test", "limit": 3})
    )

    assert result["shown"] == 3
    assert result["matched"] == 12
    assert [row["number"] for row in result["records"]] == [1, 2, 3]
    assert all(row["page_stored"] is False for row in result["records"])
    assert len(json.dumps(result, ensure_ascii=False)) < 1000


def test_missing_record_fields_and_nulls_are_not_emitted(monkeypatch):
    monkeypatch.setattr(
        "wsignal.inference.tools.get_settings",
        lambda: SimpleNamespace(
            search_default_limit=8, search_max_limit=20, search_snippet_chars=200
        ),
    )
    payload = {
        "adapter": "brave",
        "query": "test",
        "records": [{"url": "https://example.test", "title": None, "snippet": None}],
    }

    result = json.loads(render_tool_result(payload, "brave_search", {"query": "test"}))

    assert result["records"] == [
        {"number": 1, "url": "https://example.test", "page_stored": False}
    ]
    assert result["shown"] == 1
    assert result["matched"] == 1
    assert result["date_semantics"] == "no source date"


def test_search_snippet_cap_is_configurable(monkeypatch):
    monkeypatch.setattr(
        "wsignal.inference.tools.get_settings",
        lambda: SimpleNamespace(
            search_default_limit=8, search_max_limit=20, search_snippet_chars=24
        ),
    )
    payload = {
        "adapter": "ddg",
        "query": "needle",
        "records": [
            {
                "title": "T",
                "url": "https://example.test",
                "snippet": "prefix " * 12 + "needle " + "suffix " * 12,
            }
        ],
    }

    result = json.loads(render_tool_result(payload, "ddg", {"query": "needle"}))

    assert len(result["records"][0]["snippet"]) <= 24
    assert "needle" in result["records"][0]["snippet"]
    assert result["records"][0]["snippet"] != ("prefix " * 12 + "needle " + "suffix " * 12)
