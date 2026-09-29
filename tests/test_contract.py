from wsignal.interface.schemas import Citation, Entry, SearchResponse, SearchStats


def test_jury_response_contract_exposes_required_fields():
    assert {
        "name", "url", "published_at", "type", "lang", "tier", "verified",
        "title_original", "summary_ru", "summary_is_generated",
    } <= set(Citation.model_fields)
    assert {
        "name_ru", "transition_ru", "score", "why_ru", "what_would_refute_ru",
        "problem_ru", "advantage_ru", "case_example_ru", "searches_run",
        "sources_checked", "citations", "patterns",
    } <= set(Entry.model_fields)
    assert {
        "candidates_found", "sources_processed", "high_confidence_count",
        "citations_verified", "citations_total",
    } <= set(SearchStats.model_fields)
    assert {"entries", "models_used"} <= set(SearchResponse.model_fields)
