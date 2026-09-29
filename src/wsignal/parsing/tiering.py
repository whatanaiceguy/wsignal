
from __future__ import annotations

from collections.abc import Collection
from urllib.parse import urlsplit

from wsignal.parsing.base import (
    TIER_AUTHORITATIVE,
    TIER_SOCIAL,
    TIER_TRADE,
    TIER_UNKNOWN,
)
from wsignal.parsing.hosts import known_hosts

SOCIAL_HOSTS: frozenset[str] = frozenset(
    {
        "x.com",
        "twitter.com",
        "facebook.com",
        "instagram.com",
        "tiktok.com",
        "linkedin.com",
        "vk.com",
        "ok.ru",
        "weibo.com",
        "threads.net",
        "bsky.app",
        "mastodon.social",
        "t.me",
        "telegram.me",
        "telegra.ph",
        "youtube.com",
        "youtu.be",
        "news.ycombinator.com",
        "reddit.com",
        "lobste.rs",
        "slashdot.org",
        "techmeme.com",
        "digg.com",
        "quora.com",
        "medium.com",
        "substack.com",
        "dev.to",
        "hashnode.dev",
        "blogspot.com",
        "wordpress.com",
        "tumblr.com",
        "livejournal.com",
        "habr.com",
        "prnewswire.com",
        "businesswire.com",
        "globenewswire.com",
        "einpresswire.com",
        "accesswire.com",
        "prweb.com",
        "newswire.com",
        "prlog.org",
    }
)

AUTHORITATIVE_HOSTS: frozenset[str] = frozenset(
    {
        "arxiv.org",
        "doi.org",
        "ietf.org",
        "rfc-editor.org",
        "iso.org",
        "iec.ch",
        "itu.int",
        "w3.org",
        "ieee.org",
        "acm.org",
        "etsi.org",
        "nist.gov",
        "cisa.gov",
        "ncsc.gov.uk",
        "enisa.europa.eu",
        "epo.org",
        "wipo.int",
        "uspto.gov",
        "patentscope.wipo.int",
        "rospatent.gov.ru",
        "fips.ru",
        "who.int",
        "oecd.org",
    }
)

AUTHORITATIVE_SUFFIXES: tuple[str, ...] = (
    ".gov",
    ".gov.uk",
    ".gov.ru",
    ".mil",
    ".edu",
    ".ac.uk",
    ".europa.eu",
    ".int",
)


def normalise_host(value: str) -> str:
    raw = (value or "").strip()
    if "//" in raw or raw.startswith("http"):
        raw = urlsplit(raw).netloc
    return raw.lower().split("@")[-1].split(":")[0].removeprefix("www.")


def _matches(host: str, known: Collection[str]) -> bool:
    return any(host == entry or host.endswith("." + entry) for entry in known)


def host_tier(value: str) -> int:
    host = normalise_host(value)
    if not host:
        return TIER_UNKNOWN
    if _matches(host, SOCIAL_HOSTS):
        return TIER_SOCIAL
    if _matches(host, AUTHORITATIVE_HOSTS) or host.endswith(AUTHORITATIVE_SUFFIXES):
        return TIER_AUTHORITATIVE
    if _matches(host, known_hosts()):
        return TIER_TRADE
    return TIER_UNKNOWN
