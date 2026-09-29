import pytest

from wsignal.parsing.base import (
    TIER_AUTHORITATIVE,
    TIER_SOCIAL,
    TIER_TRADE,
    TIER_UNKNOWN,
)
from wsignal.parsing.hosts import known_hosts
from wsignal.parsing.tiering import host_tier, normalise_host


@pytest.mark.parametrize(
    "value,expected",
    [
        ("https://www.ncsc.gov.uk/guidance/pqc-migration-timelines", "ncsc.gov.uk"),
        ("WWW.Example.COM", "example.com"),
        ("example.com:8443", "example.com"),
        ("", ""),
    ],
)
def test_a_host_is_read_from_either_a_host_or_a_url(value, expected):
    assert normalise_host(value) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://www.ncsc.gov.uk/guidance/pqc-migration-timelines",
        "https://nist.gov/anything",
        "https://www.cisa.gov/news",
        "https://arxiv.org/abs/2603.01234",
        "https://digital-strategy.ec.europa.eu/en/policies",
        "https://cs.stanford.edu/~someone/paper.pdf",
    ],
)
def test_mandated_publishers_are_authoritative(url):
    assert host_tier(url) == TIER_AUTHORITATIVE


@pytest.mark.parametrize(
    "url",
    [
        "https://x.com/someone/status/1",
        "https://www.tiktok.com/@someone/video/1",
        "https://news.ycombinator.com/item?id=1",
        "https://old.reddit.com/r/x/comments/1",
        "https://someone.medium.com/a-post",
        "https://www.businesswire.com/news/home/1/en/thing",
    ],
)
def test_the_six_categories_the_clause_names_are_social(url):
    assert host_tier(url) == TIER_SOCIAL


def test_a_subdomain_of_a_platform_is_the_platform_and_a_lookalike_is_not():
    assert host_tier("https://blog.medium.com/x") == TIER_SOCIAL
    assert host_tier("https://notmedium.com/x") != TIER_SOCIAL


def test_a_register_host_is_trade():
    host = next(h for h in sorted(known_hosts()) if not h.endswith((".gov", ".edu")))
    assert host_tier(host) == TIER_TRADE


@pytest.mark.parametrize(
    "url",
    [
        "https://some-startup-we-never-heard-of.io/blog/launch",
        "https://неизвестный-сайт.рф/статья",
        "",
        "not a url at all",
    ],
)
def test_an_unrecognised_host_is_unknown_rather_than_social(url):
    assert host_tier(url) == TIER_UNKNOWN


def test_the_tiers_are_ordered_from_most_to_least_authoritative():
    """0 sits outside the order on purpose: it is not a low score."""
    assert TIER_AUTHORITATIVE < TIER_TRADE < TIER_SOCIAL
    assert TIER_UNKNOWN < TIER_AUTHORITATIVE
