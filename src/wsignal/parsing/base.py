from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

TIER_UNKNOWN = 0
TIER_AUTHORITATIVE = 1
TIER_TRADE = 2
TIER_SOCIAL = 3

TIER_NAMES: dict[int, str] = {
    TIER_UNKNOWN: "unknown",
    TIER_AUTHORITATIVE: "authoritative",
    TIER_TRADE: "trade",
    TIER_SOCIAL: "social",
}


def tier_name(tier: int) -> str:
    return TIER_NAMES.get(int(tier), "unknown")

DECLARATIVE_TYPES = frozenset({"paper", "patent", "standard", "news"})


@dataclass(slots=True)
class Document:
    url: str
    title: str
    text: str
    source_name: str
    source_type: str
    source_lang: str
    source_tier: int
    published_at: date | None = None
    date_source: str | None = None
    adapter: str = ""
    retrieved: bool = True
    links_to: str | None = None

    @property
    def may_create_technology(self) -> bool:
        return self.source_type in DECLARATIVE_TYPES


@dataclass(slots=True)
class Surface:

    adapter: str
    query: str
    window_months: int = 24
    why: str = ""
    limit: int = 50


@dataclass(slots=True)
class HarvestResult:

    surface: Surface
    documents: list[Document] = field(default_factory=list)
    error: str | None = None
    total: int | None = None
    available: int | None = None


class Source(Protocol):

    name: str
    host: str

    async def harvest(
        self, surface: Surface, fetcher: object
    ) -> list[Document] | HarvestResult: ...
