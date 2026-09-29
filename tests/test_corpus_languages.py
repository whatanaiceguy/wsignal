from wsignal.inference.tools import (
    _corpus_counts_pair,
    _corpus_request,
    _corpus_search,
    _corpus_snippet,
)


def _row(article_id, lang, matched):
    return {
        "id": article_id,
        "title": f"t{article_id}",
        "host": "h",
        "published": None,
        "lang": lang,
        "snippet": "**x**",
        "matched": matched,
        "matched_at_least": False,
    }


def test_a_russian_only_query_is_told_to_add_the_english_terms():
    request = _corpus_request("search", {"query": "безопасность MCP-серверов"})
    payload = _corpus_search(request, [[_row(1, "ru", 1)]])
    assert "query_alt" in payload["language_note"]
    english = _corpus_request("search", {"query": "MCP server security"})
    assert "language_note" not in _corpus_search(english, [[_row(2, "en", 1)]])


def test_query_alt_merges_both_languages_and_reports_each():
    request = _corpus_request(
        "search",
        {"query": "MCP server security", "query_alt": "безопасность MCP", "limit": 3},
    )
    english = [_row(1, "en", 40), _row(2, "en", 40), _row(3, "en", 40)]
    russian = [_row(9, "ru", 5), _row(1, "en", 5)]
    payload = _corpus_search(request, [english, russian])
    assert [hit["id"] for hit in payload["hits"]] == [1, 9, 2]
    assert payload["matched"] == 45
    assert [part["matched"] for part in payload["by_query"]] == [40, 5]
    assert "language_note" not in payload


def test_query_alt_sums_the_monthly_series():
    request = _corpus_request(
        "counts", {"query": "CPO", "query_alt": "оптика в корпусе", "months": 2}
    )
    months = [{"m": "2026-08", "n": 3, "total": 100}, {"m": "2026-09", "n": 4, "total": 100}]
    other = [{"m": "2026-08", "n": 1, "total": 100}, {"m": "2026-09", "n": 0, "total": 100}]
    payload = _corpus_counts_pair(request, [
        {"months": months, "matched_total": 7, "too_broad": False},
        {"months": other, "matched_total": 1, "too_broad": False},
    ])
    assert payload["matches"] == [4, 4]
    assert payload["matched_total"] == 8
    assert payload["by_query"][1]["query"] == "оптика в корпусе"
    assert payload["by_query"][1]["matched_total"] == 1


def test_months_zero_is_refused_rather_than_defaulted():
    try:
        _corpus_request("counts", {"query": "CPO", "months": 0})
    except ValueError as exc:
        assert "got 0" in str(exc)
    else:
        raise AssertionError("months=0 was accepted")


def test_a_lead_snippet_does_not_repeat_the_title():
    row = {"title": "AI agent as a worker", "snippet": "AI agent as a worker. Risks begin here"}
    assert _corpus_snippet(row, "agent", 200) == "Risks begin here"
    matched = {"title": "AI agent", "snippet": "AI **agent** in the title"}
    assert _corpus_snippet(matched, "agent", 200).startswith("AI agent")


def test_unbalanced_query_alt_gets_a_note_and_per_query_ratios():
    request = _corpus_request(
        "counts", {"query": "agent identity", "query_alt": "ИИ-агент", "months": 4}
    )
    def months(counts):
        return [
            {"m": f"2026-0{i}", "n": n, "total": 100}
            for i, n in zip(range(6, 10), counts, strict=True)
        ]

    small = months((4, 4, 2, 2))
    large = months((10, 10, 40, 40))
    payload = _corpus_counts_pair(request, [
        {"months": small, "matched_total": 12, "too_broad": False},
        {"months": large, "matched_total": 100, "too_broad": False},
    ])
    assert "balance_note" in payload
    assert payload["by_query"][0]["share_ratio"] < 1 < payload["by_query"][1]["share_ratio"]
