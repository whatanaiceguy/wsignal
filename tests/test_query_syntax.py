import pytest

from wsignal.parsing.arxiv import build_terms

IMPOSSIBLE_AS_PHRASE = "software supply chain security dependency integrity provenance"


@pytest.mark.parametrize(
    "query",
    ["neuromorphic computing", IMPOSSIBLE_AS_PHRASE],
)
def test_queries_use_field_qualified_terms_without_exact_phrase(query):
    built = build_terms(query)
    terms = query.split()
    assert '"' not in built
    assert built.count(" OR ") == len(terms) - 1
    for term in terms:
        assert f"all:{term}" in built


def test_punctuation_does_not_reach_the_query():
    assert build_terms("AI-агенты, IAM (identity)") == (
        "all:AI-агенты OR all:IAM OR all:identity"
    )


def test_empty_query_falls_back_rather_than_producing_empty_parens():
    assert build_terms("!!!") == 'all:"!!!"'
