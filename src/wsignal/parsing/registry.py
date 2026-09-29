from __future__ import annotations

from typing import Any

from wsignal.parsing.arxiv import ArxivSource
from wsignal.parsing.base import Source
from wsignal.parsing.brave import BraveSource
from wsignal.parsing.ddg import DuckDuckGoLiteSource
from wsignal.parsing.epo import EpoSource
from wsignal.parsing.hn import HackerNewsSource
from wsignal.parsing.openalex import OpenAlexSource
from wsignal.parsing.rospatent import RospatentSource
from wsignal.parsing.rospatent import document_text as rospatent_document_text
from wsignal.parsing.rospatent import is_document_url as is_rospatent_document
from wsignal.parsing.x import XSource

SOURCES: dict[str, Source] = {
    source.name: source
    for source in (
        OpenAlexSource(),
        ArxivSource(),
        HackerNewsSource(),
        DuckDuckGoLiteSource(),
        BraveSource(),
        EpoSource(),
        RospatentSource(),
        XSource(),
    )
}
ADAPTER_DATE_TYPE: dict[str, str] = {
    "openalex": "published",
    "arxiv": "submitted",
    "hn": "announced",
    "ddg": "none",
    "brave": "none",
    "feeds": "announced",
    "epo": "published",
    "rospatent": "published",
    "x": "announced",
    "store": "mixed",
    "discovery": "mixed",
    "web_search": "none",
}

LOCAL_ADAPTERS: tuple[str, ...] = ("store",)

COMPOSITES: dict[str, tuple[str, ...]] = {
    "discovery": ("store", "openalex", "hn"),
    "web_search": ("brave",),
}

SOURCE_WALL_CLOCK_S: dict[str, float] = {"arxiv": 5.0}


def adapter_names() -> list[str]:
    return sorted(set(SOURCES) | set(LOCAL_ADAPTERS) | set(COMPOSITES))

KEYED_READERS: tuple[tuple[Any, Any], ...] = (
    (is_rospatent_document, rospatent_document_text),
)


def keyed_reader(url: str):
    for matches, read in KEYED_READERS:
        if matches(url):
            return read
    return None
