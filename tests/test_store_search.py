from wsignal.inference.tools import _store_sql, tsquery_or
from wsignal.parsing.fts import FTS_COLUMN
from wsignal.parsing.registry import LOCAL_ADAPTERS, adapter_names


def test_terms_are_ored_not_anded():
    assert tsquery_or("agent identity access management stealth") == (
        "agent | identity | access | management | stealth"
    )


def test_operators_cannot_reach_tsquery():
    hostile = "cloud & (compute | ') <-> !x : *"
    built = tsquery_or(hostile)
    for character in "&()'<>!:*":
        assert character not in built
    assert built == "cloud | compute | x"


def test_hyphens_and_cyrillic_survive():
    assert tsquery_or("multi-agent") == "multi-agent"
    assert tsquery_or("квантовое сжатие") == "квантовое | сжатие"


def test_a_query_with_no_words_is_reported_rather_than_run():
    assert tsquery_or("   ") == ""
    assert tsquery_or("!!! ???") == ""


def test_a_single_term_has_no_broad_pass_to_fall_back_to():
    assert " | " not in tsquery_or("neuromorphic")


def test_the_filter_and_the_rank_both_read_the_stored_column():
    sql = _store_sql("to_tsquery(:cfg, :tsq)")
    assert sql.count(f"d.{FTS_COLUMN}") == 2, "rank and filter must both read it"
    assert f"ts_rank_cd(d.{FTS_COLUMN}" in sql
    assert f"WHERE d.{FTS_COLUMN} @@" in sql
    assert "to_tsvector(" not in sql


def test_store_is_offered_to_agents_but_is_not_a_network_source():
    assert "store" in adapter_names()
    assert "store" in LOCAL_ADAPTERS
    from wsignal.parsing.registry import SOURCES

    assert "store" not in SOURCES, (
        "a Source takes a Surface and a Fetcher; this one needs a session"
    )
