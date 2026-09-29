from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import time
from datetime import UTC, date, datetime
from typing import Any

import httpx
from sqlalchemy import func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError, InterfaceError, MissingGreenlet, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from wsignal.config import COMPOSITE_QUOTA, METERED_ADAPTERS, get_settings
from wsignal.corpus import CorpusUnavailable, get_corpus
from wsignal.models import (
    HARNESS_NOTE_KINDS,
    Agent,
    Alias,
    Document,
    Entry,
    Field,
    FieldRename,
    HarnessNote,
    Refutation,
    RunEvent,
    SourceEvent,
    Technology,
    Turn,
)
from wsignal.parsing.base import HarvestResult, Surface
from wsignal.parsing.composite import fuse
from wsignal.parsing.dates import subtract_months
from wsignal.parsing.fts import FTS_COLUMN, FTS_CONFIG
from wsignal.parsing.http import MAX_RESPONSE_BYTES, Fetcher
from wsignal.parsing.page_worker import PageProcessingTimeout, run_page_processing
from wsignal.parsing.registry import (
    ADAPTER_DATE_TYPE,
    COMPOSITES,
    LOCAL_ADAPTERS,
    SOURCE_WALL_CLOCK_S,
    SOURCES,
    adapter_names,
    keyed_reader,
)
from wsignal.parsing.text import page_metadata
from wsignal.parsing.tiering import host_tier

_FETCH_SUMMARY_ADAPTERS = ("arxiv", "epo", "openalex", "rospatent")
_SEARCH_TOOL_NAMES = {"search", "discovery", "web_search", *SOURCES}
_HTML_TAG = re.compile(r"<[^>]*>")
_MARKDOWN_EMPHASIS = re.compile(r"(?:\*{1,2}|_{1,2}|~~|`+)")
_SPACE = re.compile(r"\s+")
_CORPUS_ACTIONS = ("search", "counts", "read")
_HARNESS_NOTES_PER_AGENT = 20
_HARNESS_NOTE_CHARS = 2000
_HARNESS_NOTE_META_CHARS = 2000
_CORPUS_TITLE_CHARS = 160
_CORPUS_RECENT_MONTHS = 12
_DATE_SEMANTICS = {
    "published": "publisher publication date",
    "submitted": "submission date",
    "announced": "announcement date",
    "none": "no source date",
    "mixed": "date meaning varies by source",
    "unknown": "date meaning unknown",
}


_KEYED_SERVICES = {
    "brave": "brave",
    "web_search": "brave",
    "epo": "epo",
    "rospatent": "rospatent",
    "x": "x",
}


_CLOSED: dict[str, tuple[float, str, str]] = {}
_CLOSED_HOLD_S = {"payment": 1800, "challenge": 600}
_DDG_CHALLENGES_TO_CLOSE = 2
_ddg_challenges = 0


def _close(service: str, reason: str) -> None:
    _CLOSED[service] = (time.monotonic(), datetime.now(UTC).strftime("%H:%M"), reason)


def _note_ddg(challenged: bool) -> None:
    global _ddg_challenges
    _ddg_challenges = _ddg_challenges + 1 if challenged else 0
    if _ddg_challenges >= _DDG_CHALLENGES_TO_CLOSE:
        _close("ddg", "challenge")
        _ddg_challenges = 0


def _unavailable(adapter: str) -> dict | None:
    service = _KEYED_SERVICES.get(adapter, adapter)
    closed = _CLOSED.get(service)
    if closed is not None and time.monotonic() - closed[0] < _CLOSED_HOLD_S[closed[2]]:
        why = (
            "answered 402 Payment Required: its key has no credit"
            if closed[2] == "payment"
            else "served its bot challenge several times in a row"
        )
        return {
            "no_request": True,
            "adapter": adapter,
            "error": (
                f"{service} {why} at {closed[1]} UTC, so calls to it are refused for now "
                "without a request and spend nothing. This is not an absence of evidence. "
                "Use another source."
            ),
        }
    if adapter not in _KEYED_SERVICES:
        return None
    try:
        from wsignal.routes import resolve

        if resolve(service) != "off":
            return None
    except Exception:
        return None
    return {
        "no_request": True,
        "adapter": adapter,
        "error": (
            f"{adapter} is not configured in this deployment, so no request was made and "
            "nothing was spent; this is not an absence of evidence. Use another source."
        ),
    }


def _search_limit(arguments: dict) -> int:
    settings = get_settings()
    requested = arguments.get("limit")
    if requested is None:
        requested = getattr(settings, "search_default_limit", 8)
    maximum = getattr(settings, "search_max_limit", 20)
    return max(1, min(int(requested), maximum))


def _clean_search_snippet(value: Any, query: str, cap: int) -> str:
    text = str(value or "")
    cap = max(0, cap)
    if not cap:
        return ""
    text = _HTML_TAG.sub(" ", text)
    text = _MARKDOWN_EMPHASIS.sub("", text)
    text = _SPACE.sub(" ", text).strip()
    terms = [term for term in re.findall(r"[\w-]+", query, flags=re.UNICODE) if term]
    lowered = text.casefold()
    positions = [lowered.find(term.casefold()) for term in terms]
    positions = [position for position in positions if position >= 0]
    if len(text) <= cap:
        return text
    start = max(0, (min(positions) if positions else 0) - cap // 3)
    if start:
        start = text.find(" ", start)
        start = 0 if start < 0 else start + 1
    end = start + cap
    if end < len(text):
        boundary = text.rfind(" ", start, end)
        if boundary > start:
            end = boundary
    return text[start:end].strip()


def _model_search_payload(
    payload: dict, name: str | None, arguments: dict | None
) -> dict:
    records = payload.get("records")
    is_search = name in _SEARCH_TOOL_NAMES or bool(
        re.search(r"(?:^|[_-])(search|fetch|discovery)(?:$|[_-])", name or "")
    )
    adapter_name = str(payload.get("adapter") or "")
    if not is_search and adapter_name not in _SEARCH_TOOL_NAMES:
        return payload
    if records is None and payload.get("count") is not None:
        records = []
    if not isinstance(records, list):
        return payload
    records = [record for record in records if isinstance(record, dict)]
    limit = _search_limit(arguments or {})
    shown_records = records[:limit]
    query = str(payload.get("query") or (arguments or {}).get("query") or "")
    rendered = []
    cap = getattr(get_settings(), "search_snippet_chars", 200)
    for number, record in enumerate(shown_records, start=1):
        row = {"number": number}
        for key, source_key in (
            ("title", "title"),
            ("source_name", "source_name"),
            ("date", "published_at"),
            ("url", "url"),
        ):
            value = record.get(source_key)
            if value is not None:
                row[key] = value
        stored = bool(record.get("retrieved")) and record.get("source_type") == "page"
        source_adapter = record.get("adapter") or record.get("found_by")
        if isinstance(source_adapter, list):
            stored = stored and any(
                source not in {*_FETCH_SUMMARY_ADAPTERS, "hn", "x", "feeds"}
                for source in source_adapter
            )
        else:
            stored = stored and source_adapter not in {
                *_FETCH_SUMMARY_ADAPTERS, "hn", "x", "feeds", "brave", "ddg"
            }
        row["page_stored"] = stored
        snippet = _clean_search_snippet(record.get("snippet"), query, cap)
        if snippet:
            row["snippet"] = snippet
        rendered.append(row)

    matched = payload.get("matched_total", payload.get("total_matched"))
    if matched is None:
        matched = payload.get("count", len(records))
    date_type = payload.get("date_type") or ADAPTER_DATE_TYPE.get(
        str(payload.get("adapter") or name or ""), "unknown"
    )
    output: dict[str, Any] = {
        key: value
        for key, value in {
            "query": payload.get("query") or query,
            "adapter": payload.get("adapter") or name,
            "shown": len(rendered),
            "matched": matched,
            "date_semantics": _DATE_SEMANTICS.get(str(date_type), str(date_type)),
            "records": rendered,
        }.items()
        if value is not None
    }
    if payload.get("match_mode") == "any_term":
        output["match"] = "Loose match: records may match any query term."
    for key in (
        "web_calls_remaining",
        "sources_answered",
        "sources_failed",
        "sources_not_asked",
        "total_matched_by_source",
        "undated_excluded",
        "undated_note",
        "available_to_page",
        "available_note",
        "error",
        "note",
    ):
        if payload.get(key) is not None:
            output[key] = payload[key]
    return output


SNIPPET_CHARS = 600


_WORD = re.compile(r"[^\w\-]+", re.UNICODE)


async def newest_fetchable_document(
    session: AsyncSession, url: str
) -> Document | None:
    return await session.scalar(
        select(Document)
        .where(
            Document.url == url,
            Document.retrieved.is_(True),
            Document.source_type.not_in(("paper", "patent")),
            Document.adapter.is_distinct_from("hn"),
            or_(
                Document.adapter.is_(None),
                Document.adapter.not_in(
                    (*_FETCH_SUMMARY_ADAPTERS, "brave", "ddg")
                ),
            ),
        )
        .order_by(Document.fetched_at.desc())
        .limit(1)
    )


async def newest_document(session: AsyncSession, url: str) -> Document | None:
    return await session.scalar(
        select(Document)
        .where(Document.url == url)
        .order_by(Document.fetched_at.desc())
        .limit(1)
    )



def _fatal_session_error(exc: Exception) -> bool:
    if isinstance(exc, MissingGreenlet):
        return True
    if isinstance(exc, DBAPIError) and exc.connection_invalidated:
        return True
    return isinstance(exc, InterfaceError) and "another operation is in progress" in str(exc)


def tsquery_or(query: str) -> str:
    terms: list[str] = []
    for raw in _WORD.sub(" ", query).split():
        term = raw.strip("-")
        if term and any(character.isalnum() for character in term):
            terms.append(term)
    return " | ".join(terms)


def as_record(document: Any, adapter: str) -> dict:
    return {
        "url": document.url,
        "title": document.title,
        "published_at": (
            document.published_at.isoformat()
            if document.published_at is not None
            and document.published_at <= date.today()
            else None
        ),
        "date_source": (
            getattr(document, "date_source", None)
            if document.published_at is not None
            and document.published_at <= date.today()
            else None
        ),
        "date_type": ADAPTER_DATE_TYPE.get(document.adapter or adapter, "unknown"),
        "source_name": document.source_name,
        "source_type": document.source_type,
        "source_tier": document.source_tier,
        "snippet": document.text[:SNIPPET_CHARS],
        "links_to": document.links_to,
        "retrieved": document.retrieved,
    }


def _store_sql(tsquery: str) -> str:
    return f"""
        WITH hits AS (
            SELECT DISTINCT ON (d.url)
                   d.id, d.url, d.title, d.published_at, d.source_name,
                   d.source_type, d.source_tier, d.links_to, d.adapter, d.retrieved,
                   ts_rank_cd(d.{FTS_COLUMN}, {tsquery}, 33) AS rank
            FROM documents d
            WHERE d.{FTS_COLUMN} @@ {tsquery}
              AND d.source_type IS DISTINCT FROM 'fetch_failure'
            ORDER BY d.url, d.fetched_at DESC
        ), counted AS (
            SELECT *, count(*) OVER () AS matched_total,
                   count(*) FILTER (WHERE published_at IS NULL)
                       OVER () AS undated_total
            FROM hits
        ), top AS (
            SELECT * FROM counted
            WHERE CAST(:since AS date) IS NULL
               OR published_at >= CAST(:since AS date)
            ORDER BY rank DESC, published_at DESC NULLS LAST
            LIMIT :lim
        )
        SELECT t.url, t.title, t.published_at, t.source_name, t.source_type, t.source_tier,
               t.links_to, t.adapter, t.retrieved, t.rank,
               t.matched_total, t.undated_total,
               ts_headline(:cfg, left(d.content, 50000), {tsquery},
                           'MaxFragments=2,MinWords=8,MaxWords=24,'
                           'StartSel=**,StopSel=**') AS snippet
        FROM top t JOIN documents d ON d.id = t.id
        ORDER BY t.rank DESC, t.published_at DESC NULLS LAST
    """


def content_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class RetrievalCache:
    def __init__(self) -> None:
        self._entries: dict[tuple, asyncio.Future] = {}
        self.hits = 0
        self.misses = 0

    async def run_once(self, key: tuple, factory: Any) -> Any:
        existing = self._entries.get(key)
        if existing is not None:
            self.hits += 1
            return await asyncio.shield(existing)

        self.misses += 1
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._entries[key] = future
        try:
            result = await factory()
        except BaseException as exc:
            if self._entries.get(key) is future:
                self._entries.pop(key, None)
            if not future.done():
                future.set_exception(
                    exc
                    if isinstance(exc, Exception)
                    else RuntimeError(f"shared retrieval interrupted: {type(exc).__name__}")
                )
                future.exception()
            raise
        if not future.done():
            future.set_result(result)
        return result


TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "rename_topic",
            "description": (
                "Rename the field you are working on when retrieved evidence shows "
                "a better-named transition is the real moving topic. State why and "
                "the verdict on the previous name as it stands now."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "new_focus": {"type": "string", "maxLength": 2000},
                    "reason": {"type": "string", "maxLength": 500},
                    "verdict_on_previous": {"type": "string", "maxLength": 500},
                },
                "required": ["new_focus", "reason", "verdict_on_previous"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": (
                "Search one source for documents matching a query inside a time "
                "window. Returns urls, titles, dates and snippets. Everything "
                "returned is stored, so fetch() on any of these urls is free. "
                "A search that returns nothing is a result worth reporting, not "
                "a failure to retry with the same words. The 'store' adapter "
                "searches everything already retrieved, costs nothing and hits "
                "no rate limit, so it is usually worth trying first."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "adapter": {
                        "type": "string",
                        "enum": adapter_names(),
                        "description": "Which source to search. Call list_sources first if unsure.",
                    },
                    "query": {
                        "type": "string",
                        "description": (
                            "Keywords. Match the language the source indexes: "
                            "English for arxiv, hn and openalex."
                        ),
                    },
                    "window_months": {
                        "type": "integer",
                        "description": "How far back to look. 24 is the default.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max records to show, default 8, capped at 20.",
                    },
                },
                "required": ["adapter", "query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch",
            "description": (
                "Return the full stored text of a url. If it was returned by an "
                "earlier search it is already stored and this costs nothing. "
                "Quote from what this returns: a quote that is not in the stored "
                "text fails verification later and is visible in the output."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "live": {
                        "type": "boolean",
                        "description": (
                            "Go back to the network even if a recent copy is "
                            "stored. The old copy is kept, not overwritten."
                        ),
                    },
                    "offset": {
                        "type": "integer",
                        "description": (
                            "Start this many characters in. Long pages come "
                            "back in parts; use the next_offset from the reply "
                            "to request the next part."
                        ),
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "corpus",
            "description": (
                "Our research corpus: about 4.6 million dated technology news and "
                "trade-press articles, mostly English and Russian, full-text indexed. "
                "action=search finds articles by title and text, newest first unless "
                "order=oldest, and returns ids, titles, hosts, dates, snippets and the "
                "total number of matches, capped at 10,000 with matched_at_least=true "
                "when there are at least that many. action=counts returns monthly matches "
                "against monthly corpus totals as a share per 10k articles, with the "
                "recent-versus-previous share ratio; corpus volume is several-fold higher "
                "in recent months, so judge by share, never by raw counts. action=read "
                "returns one article by id, in parts like fetch, and stores it like "
                "a fetched page: cite its url with a verbatim quote from this text."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": list(_CORPUS_ACTIONS)},
                    "query": {
                        "type": "string",
                        "description": (
                            "For search and counts. Websearch syntax: words, "
                            "\"exact phrase\", OR, -excluded. At most 200 characters. "
                            "Words match as written, so a Russian query reaches only "
                            "Russian articles and an English one only English ones."
                        ),
                    },
                    "query_alt": {
                        "type": "string",
                        "description": (
                            "For search and counts: the same query in the other language "
                            "(your own translation of the terms). Both run; search merges "
                            "the hits and counts sums the monthly series."
                        ),
                    },
                    "since": {
                        "type": "string",
                        "description": "search: earliest publication date, YYYY-MM-DD.",
                    },
                    "until": {
                        "type": "string",
                        "description": "search: latest publication date, YYYY-MM-DD.",
                    },
                    "lang": {
                        "type": "string",
                        "description": "search: article language, such as en or ru.",
                    },
                    "order": {"type": "string", "enum": ["newest", "oldest"]},
                    "limit": {
                        "type": "integer",
                        "description": (
                            "search: max hits, default 25, capped at 25. Hits are spread "
                            "across hosts and copies of one article are folded into one."
                        ),
                    },
                    "months": {
                        "type": "integer",
                        "description": (
                            "counts: months back including the current one, "
                            "default 36, at most 120."
                        ),
                    },
                    "id": {"type": "integer", "description": "read: an article id from search."},
                    "offset": {
                        "type": "integer",
                        "description": (
                            "read: start this many characters in; use next_offset "
                            "from the previous part."
                        ),
                    },
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "known_technologies",
            "description": (
                "Ask the store whether this technology has been seen before, "
                "under this or any other name. Use it before opening a new one, "
                "so the same thing under two phrasings does not become two."
            ),
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "merge_technologies",
            "description": (
                "Merge two technology rows that name the same thing: every wording, "
                "field and entry of from_id moves to into_id and from_id disappears. "
                "The store is shared across runs, so merge only true duplicates. Logged."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "from_id": {"type": "integer", "description": "The duplicate that goes."},
                    "into_id": {"type": "integer", "description": "The row that stays."},
                    "reason": {"type": "string"},
                },
                "required": ["from_id", "into_id", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "unbind_alias",
            "description": (
                "Detach a wording that was bound to the wrong technology, so it no "
                "longer matches that row. A row's only wording cannot be detached. Logged."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "surface_form": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["surface_form", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "harness_note",
            "description": (
                "Leave a note for the people who build this harness, whenever it "
                "blocks you, contradicts its own descriptions, wastes your effort, or "
                "lacks something you needed. The harness attaches your last tool call "
                "and its result, so say what you expected and what you did instead. "
                "It changes nothing in this run and answers only 'noted': carry on "
                "with the task after it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": list(HARNESS_NOTE_KINDS),
                        "description": (
                            "bug: something is broken. friction: it works but misleads "
                            "or wastes effort. missing: you needed something no tool "
                            "gives. idea: anything else."
                        ),
                    },
                    "tool": {
                        "type": "string",
                        "description": "The tool this is about, if one.",
                    },
                    "text": {"type": "string", "maxLength": _HARNESS_NOTE_CHARS},
                },
                "required": ["kind", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_sources",
            "description": "What sources exist, what each indexes, and what its dates mean.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_technology",
            "description": (
                "Record a technology the store has not seen, with the surface "
                "form you found it under. Open one only for an actual "
                "technology, never for an area of the map: this table is the "
                "index that says which claims concern the same thing, and an "
                "area in it makes that index answer the wrong question. To "
                "record YOUR wording for a technology the store already has, "
                "pass its technology_id: that binds the phrasing to the row "
                "that exists. Without the id you create a second row for one "
                "thing, which is the failure this table exists to prevent."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "technology_id": {
                        "type": "integer",
                        "description": (
                            "An existing technology to bind this phrasing to, "
                            "from known_technologies. Omit to create a new one."
                        ),
                    },
                    "name_ru": {"type": "string"},
                    "name_en": {"type": "string"},
                    "surface_form": {
                        "type": "string",
                        "description": "The exact phrasing you saw it under.",
                    },
                    "lang": {"type": "string", "description": "ru or en"},
                },
                "required": ["surface_form", "lang"],
            },
        },
    },
]

_TEXT_SCHEMA = {"type": "string", "maxLength": 500}
_QUOTE_SCHEMA = {"type": "string", "maxLength": 400}
_DIMENSION_SCHEMA = {
    "type": "object",
    "properties": {"value": {"type": "number", "minimum": 0, "maximum": 1}, "reason": _TEXT_SCHEMA},
    "required": ["value", "reason"],
    "additionalProperties": False,
}
_PATTERN_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": _TEXT_SCHEMA,
        "pattern": _TEXT_SCHEMA,
        "strength": {"type": "number", "minimum": 0, "maximum": 1},
        "url": _TEXT_SCHEMA,
        "quote": _QUOTE_SCHEMA,
    },
    "required": ["kind", "pattern", "strength", "url", "quote"],
    "additionalProperties": False,
}
_CITATION_SCHEMA = {
    "type": "object",
    "properties": {"url": _TEXT_SCHEMA, "quote": _QUOTE_SCHEMA},
    "required": ["url", "quote"],
    "additionalProperties": False,
}
_REBUTTAL_SCHEMA = {
    "type": "object",
    "properties": {
        "attack": _TEXT_SCHEMA,
        "response": _TEXT_SCHEMA,
        "changes": _TEXT_SCHEMA,
    },
    "required": ["attack", "response", "changes"],
    "additionalProperties": False,
}
_FINDINGS_PROPERTIES = {
    "claim": _TEXT_SCHEMA,
    "substance": _DIMENSION_SCHEMA,
    "momentum": _DIMENSION_SCHEMA,
    "faintness": _DIMENSION_SCHEMA,
    "patterns": {"type": "array", "maxItems": 4, "items": _PATTERN_SCHEMA},
    "delivery_gap": _TEXT_SCHEMA,
    "scale_against_field": _TEXT_SCHEMA,
    "what_would_refute": _TEXT_SCHEMA,
    "searched": _TEXT_SCHEMA,
    "citations": {"type": "array", "maxItems": 4, "items": _CITATION_SCHEMA},
    "rebuttal_responses": {
        "type": "array", "maxItems": 6, "items": _REBUTTAL_SCHEMA
    },
}
_FINDINGS_REQUIRED = [
    "claim", "substance", "momentum", "faintness", "patterns", "delivery_gap",
    "scale_against_field", "what_would_refute", "searched", "citations",
]
_REFUTATION_SCHEMA = {
    "type": "function",
    "function": {
        "name": "submit_refutation",
        "description": (
            "Submit concise structured refutation findings and finish this agent's work."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "claims": {
                    "type": "array",
                    "maxItems": 6,
                    "items": {
                        "type": "object",
                        "properties": {
                            "claim": _TEXT_SCHEMA,
                            "opposite": _TEXT_SCHEMA,
                            "verdict": {
                                "type": "string",
                                "enum": ["proven", "partly", "not_proven"],
                            },
                            "effect": _TEXT_SCHEMA,
                            "url": _TEXT_SCHEMA,
                            "quote": _QUOTE_SCHEMA,
                        },
                        "required": ["claim", "opposite", "verdict", "effect", "url", "quote"],
                        "additionalProperties": False,
                    },
                },
                "queries_and_venues": _TEXT_SCHEMA,
            },
            "required": ["claims", "queries_and_venues"],
            "additionalProperties": False,
        },
    },
}
_FINDINGS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "submit_findings",
        "description": "Submit concise structured findings and finish this agent's work.",
        "parameters": {
            "type": "object",
            "properties": _FINDINGS_PROPERTIES,
            "required": _FINDINGS_REQUIRED,
            "additionalProperties": False,
        },
    },
}
_SUBMISSION_SCHEMAS = {
    "submit_findings": _FINDINGS_SCHEMA,
    "submit_refutation": _REFUTATION_SCHEMA,
}


def validate_submission(name: str, arguments: dict) -> dict:
    settings = get_settings()
    schema = copy.deepcopy(_SUBMISSION_SCHEMAS[name]["function"]["parameters"])
    text_cap = max(0, settings.submission_text_max_chars)
    quote_cap = max(0, settings.submission_quote_max_chars)

    def apply_caps(node: dict, key: str = "") -> None:
        if node.get("type") == "string":
            node["maxLength"] = quote_cap if key == "quote" else text_cap
        if node.get("type") == "array":
            configured = {
                "patterns": settings.submission_max_patterns,
                "citations": settings.submission_max_citations,
                "claims": settings.submission_max_refutations,
                "rebuttal_responses": settings.submission_max_rebuttal_responses,
            }.get(key, node.get("maxItems", 200))
            node["maxItems"] = max(0, min(node.get("maxItems", configured), configured))
            apply_caps(node["items"])
        if node.get("type") == "object":
            for child_key, child in node.get("properties", {}).items():
                apply_caps(child, child_key)

    apply_caps(schema)
    if not isinstance(arguments, dict):
        raise ValueError("submission must be an object")
    result = copy.deepcopy(arguments)

    def cap_arrays(value: Any, node: dict, key: str = "") -> None:
        if node.get("type") == "array" and isinstance(value, list):
            if key in {
                "patterns", "citations", "claims", "rebuttal_responses"
            }:
                value[:] = value[:node["maxItems"]]
            for child in value:
                cap_arrays(child, node["items"])
        elif node.get("type") == "object" and isinstance(value, dict):
            for child_key, child in value.items():
                if child_key in node.get("properties", {}):
                    cap_arrays(child, node["properties"][child_key], child_key)

    cap_arrays(result, schema)

    def validate(value: Any, node: dict, path: str) -> None:
        kind = node["type"]
        if kind == "object":
            if not isinstance(value, dict):
                raise ValueError(f"{path} must be an object")
            missing = set(node.get("required", ())) - set(value)
            if missing:
                raise ValueError(f"{path} missing fields: {', '.join(sorted(missing))}")
            if node.get("additionalProperties") is False and set(value) - set(node["properties"]):
                raise ValueError(f"{path} has unexpected fields")
            for key, child in value.items():
                validate(child, node["properties"][key], f"{path}.{key}")
        elif kind == "array":
            if not isinstance(value, list) or len(value) > node.get("maxItems", len(value)):
                raise ValueError(f"{path} must be an array within its item cap")
            for index, child in enumerate(value):
                validate(child, node["items"], f"{path}[{index}]")
        elif kind == "string":
            if not isinstance(value, str) or len(value) > node["maxLength"]:
                raise ValueError(f"{path} must be a string no longer than {node['maxLength']}")
            if "enum" in node and value not in node["enum"]:
                raise ValueError(f"{path} has an invalid value")
        elif kind == "number":
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0 <= value <= 1
            ):
                raise ValueError(f"{path} must be a number between 0 and 1")

    validate(result, schema, "submission")
    if len(json.dumps(result, ensure_ascii=False, separators=(",", ":"))) > max(
        1, settings.submission_max_json_chars
    ):
        raise ValueError("submission exceeds its total character cap")
    return result


ROLE_TOOLS: dict[str, tuple[str, ...]] = {
    "assistant": (
        "search", "corpus", "known_technologies", "open_technology", "list_sources",
        "harness_note",
    ),
    "researcher": (
        "search", "fetch", "corpus", "known_technologies", "list_sources", "rename_topic",
        "harness_note",
    ),
    "refuter": (
        "search", "fetch", "corpus", "list_sources", "rename_topic", "harness_note",
    ),
    "orchestrator": (
        "search",
        "corpus",
        "known_technologies",
        "open_technology",
        "merge_technologies",
        "unbind_alias",
        "list_sources",
        "rename_topic",
        "harness_note",
    ),
}

_BY_NAME = {schema["function"]["name"]: schema for schema in TOOL_SCHEMAS}
BILLED = or_(SourceEvent.ok.is_(True), func.coalesce(SourceEvent.requests, 0) > 0)


def tools_for(role: str) -> list[dict]:
    schemas = [
        copy.deepcopy(_BY_NAME[name])
        for name in ROLE_TOOLS.get(role, ())
        if name in _BY_NAME
    ]
    if role == "orchestrator":
        rename_schema = next(
            schema for schema in schemas
            if schema["function"]["name"] == "rename_topic"
        )
        rename_schema["function"]["parameters"]["properties"]["field_id"] = {
            "type": "integer"
        }
        rename_schema["function"]["parameters"]["required"] = [
            "field_id", "new_focus", "reason", "verdict_on_previous"
        ]
        corpus_schema = next(
            schema for schema in schemas if schema["function"]["name"] == "corpus"
        )
        corpus_schema["function"]["description"] = (
            "Monthly matches of a query in our research corpus of dated technology "
            "news and trade press, against monthly corpus totals as a share per 10k "
            "articles, with the recent-versus-previous share ratio and too_broad. "
            "Use it to check a corpus_query before write_entry."
        )
        properties = corpus_schema["function"]["parameters"]["properties"]
        corpus_schema["function"]["parameters"]["properties"] = {
            "action": {"type": "string", "enum": ["counts"]},
            "query": properties["query"],
            "query_alt": properties["query_alt"],
            "months": properties["months"],
        }
    if role in ("researcher", "refuter"):
        name = "submit_refutation" if role == "refuter" else "submit_findings"
        schema = copy.deepcopy(_SUBMISSION_SCHEMAS[name])
        settings = get_settings()
        def configure(node: dict, key: str = "") -> None:
            if node.get("type") == "string":
                node["maxLength"] = (
                    settings.submission_quote_max_chars
                    if key == "quote"
                    else settings.submission_text_max_chars
                )
            if node.get("type") == "array":
                caps = {
                    "patterns": settings.submission_max_patterns,
                    "citations": settings.submission_max_citations,
                    "claims": settings.submission_max_refutations,
                    "rebuttal_responses": settings.submission_max_rebuttal_responses,
                }
                if key in caps:
                    node["maxItems"] = caps[key]
                configure(node["items"])
            if node.get("type") == "object":
                for child_key, child in node.get("properties", {}).items():
                    configure(child, child_key)

        configure(schema["function"]["parameters"])
        schemas.append(schema)
    return schemas


async def effort_for(session: AsyncSession, agent_ids: list[int]) -> dict[str, int]:

    if not agent_ids:
        return {"searches_run": 0, "sources_checked": 0, "tool_calls": 0}

    rows = (
        await session.execute(
            select(Turn.content).where(Turn.agent_id.in_(agent_ids), Turn.kind == "tool_call")
        )
    ).scalars().all()

    searches = 0
    adapters: set[str] = set()
    for content in rows:
        if not isinstance(content, dict):
            continue
        arguments = content.get("arguments")
        if content.get("name") == "corpus":
            if isinstance(arguments, dict) and arguments.get("action") in ("search", "counts"):
                searches += 1
                adapters.add("corpus")
            continue
        if content.get("name") != "search":
            continue
        searches += 1
        if isinstance(arguments, dict) and arguments.get("adapter"):
            adapters.add(str(arguments["adapter"]))

    return {
        "searches_run": searches,
        "sources_checked": len(adapters),
        "tool_calls": len(rows),
    }


class Toolbox:

    def __init__(
        self,
        session: AsyncSession,
        fetcher: Fetcher,
        cache: RetrievalCache | None = None,
    ) -> None:
        self._session = session
        self._fetcher = fetcher
        self._cache = cache if cache is not None else RetrievalCache()
        self._extra: dict[str, tuple[dict, Any, tuple[str, ...]]] = {}
        self._roles: dict[int, str] = {}
        self._assistant_lookup_agents: set[int] = set()
        self._web_call_limits: dict[int, int] = {}
        self._technology_opener = self._open_technology

    def register(self, schema: dict, handler: Any, roles: tuple[str, ...]) -> None:
        self._extra[schema["function"]["name"]] = (schema, handler, roles)

    def configure_assistant_lookup(self, agent_id: int, web_call_limit: int) -> None:
        self._assistant_lookup_agents.add(agent_id)
        self._web_call_limits[agent_id] = max(0, web_call_limit)

    def schemas_for(self, role: str) -> list[dict]:
        if role == "assistant" and self._assistant_lookup_agents:
            return [
                _BY_NAME[name]
                for name in ("corpus", "search", "fetch", "list_sources", "harness_note")
            ]
        base = tools_for(role)
        base_names = {schema["function"]["name"] for schema in base}
        return base + [
            schema for schema, _, roles in self._extra.values()
            if role in roles and schema["function"]["name"] not in base_names
        ]

    async def call(self, name: str, arguments: dict, agent_id: int | None = None) -> Any:
        web_tool = self._is_web_tool(name)
        billed = web_tool and not await self._served_locally(name, arguments)
        remaining_before = (
            await self._web_calls_remaining(agent_id) if billed else None
        )
        if billed and remaining_before == 0:
            return {
                "error": "web-call budget is spent; no request was made",
                "web_calls_remaining": 0,
                "hint": (
                    "search with adapter=store, the corpus tool, and fetch of a url "
                    "that is already stored cost nothing and still work; everything "
                    "that goes to the network is closed for you now"
                ),
            }
        if name in _SUBMISSION_SCHEMAS:
            try:
                return {"submitted": True, "submission": validate_submission(name, arguments)}
            except ValueError as exc:
                return {"error": str(exc)}
        registered = self._extra.get(name)
        handler = (
            registered[1]
            if registered
            else (
                (lambda args, agent: self._fetch(
                    args,
                    agent,
                    via=("assistant_lookup" if agent in self._assistant_lookup_agents else None),
                ))
                if name == "fetch"
                else {
                    "search": self._search,
                    "corpus": self._corpus,
                    "known_technologies": self._known_technologies,
                    "list_sources": self._list_sources,
                    "open_technology": self._technology_opener,
                    "rename_topic": self._rename_topic,
                    "harness_note": self._harness_note,
                    "merge_technologies": self._merge_technologies,
                    "unbind_alias": self._unbind_alias,
                }.get(name)
            )
        )
        if handler is None:
            payload = {
                "error": f"no such tool: {name}",
                "available": sorted(set(_BY_NAME) | set(self._extra)),
            }
            if web_tool:
                payload["web_calls_remaining"] = await self._web_calls_remaining(agent_id)
            return payload
        try:
            result = await handler(arguments, agent_id)
            if billed and not (isinstance(result, dict) and result.get("no_request")):
                await self._record_web_call(name, agent_id)
            if web_tool:
                remaining = await self._web_calls_remaining(agent_id)
                if isinstance(result, dict):
                    result["web_calls_remaining"] = remaining
                else:
                    result = {"result": result, "web_calls_remaining": remaining}
            return result
        except Exception as exc:
            if isinstance(exc, SQLAlchemyError):
                try:
                    await self._session.rollback()
                except Exception:
                    pass
            if _fatal_session_error(exc):
                raise
            if billed:
                try:
                    await self._record_web_call(name, agent_id)
                except Exception:
                    pass
            payload = {
                "error": f"{type(exc).__name__}: {exc}",
                "hint": (
                    "the tool raised instead of answering. If the message names one of "
                    "your arguments, fix that argument; otherwise the tool itself failed, "
                    "so try another source or move on rather than repeating the call"
                ),
                "tool": name,
                "arguments": {
                    key: value
                    for key, value in arguments.items()
                    if key in ("adapter", "query", "url", "window_months", "limit")
                },
            }
            if web_tool:
                payload["web_calls_remaining"] = await self._web_calls_remaining(agent_id)
            return payload

    async def _record_web_call(self, name: str, agent_id: int | None) -> None:
        if agent_id is None:
            return
        await record_source_event(
            adapter="web_call",
            query=name,
            via=(
                "assistant_lookup"
                if agent_id in self._assistant_lookup_agents
                else "web_call"
            ),
            ok=True,
            agent_id=agent_id,
        )

    async def _harness_note(self, arguments: dict, agent_id: int | None) -> Any:
        kind = str(arguments.get("kind") or "").strip()
        if kind not in HARNESS_NOTE_KINDS:
            return {
                "error": (
                    f"kind {kind!r} is not one of {', '.join(HARNESS_NOTE_KINDS)}: bug is "
                    "something broken, friction something that works but misleads or "
                    "wastes effort, missing something you needed that no tool gives, "
                    "idea anything else"
                )
            }
        text = str(arguments.get("text") or "").strip()
        if not text:
            return {
                "error": (
                    "text is required: what happened, what you expected, and what you "
                    "did instead"
                )
            }
        agent = await self._session.get(Agent, agent_id) if agent_id is not None else None
        if agent_id is not None:
            written = await self._session.scalar(
                select(func.count(HarnessNote.id)).where(HarnessNote.agent_id == agent_id)
            ) or 0
            if written >= _HARNESS_NOTES_PER_AGENT:
                return {"noted": False, "note": "notes are full for this agent; carry on"}
        tool = str(arguments.get("tool") or "").strip()[:64] or None
        self._session.add(
            HarnessNote(
                run_id=getattr(agent, "run_id", None),
                agent_id=agent_id,
                role=getattr(agent, "role", None),
                model=getattr(agent, "model", None),
                kind=kind,
                tool=tool,
                text=text[:_HARNESS_NOTE_CHARS],
                meta={"last_call": await self._last_exchange(agent_id)},
            )
        )
        await self._session.flush()
        return {"noted": True}

    async def _last_exchange(self, agent_id: int | None) -> dict | None:
        if agent_id is None:
            return None
        try:
            turns = (
                await self._session.scalars(
                    select(Turn)
                    .where(
                        Turn.agent_id == agent_id,
                        Turn.kind.in_(("tool_call", "tool_result")),
                    )
                    .order_by(Turn.seq.desc())
                    .limit(12)
                )
            ).all()
        except Exception:
            return None
        results = [
            turn.content for turn in turns
            if turn.kind == "tool_result"
            and isinstance(turn.content, dict)
            and turn.content.get("name") != "harness_note"
        ]
        if not results:
            return None
        result = results[0]
        call = next(
            (
                turn.content for turn in turns
                if turn.kind == "tool_call"
                and isinstance(turn.content, dict)
                and turn.content.get("id") == result.get("id")
            ),
            {},
        )

        def clip(value: Any) -> str:
            return json.dumps(value, ensure_ascii=False, default=str)[:_HARNESS_NOTE_META_CHARS]

        return {
            "name": result.get("name"),
            "arguments": clip(call.get("arguments")),
            "result": clip(result.get("payload")),
        }

    async def _served_locally(self, name: str, arguments: dict) -> bool:
        if not isinstance(arguments, dict):
            return False
        if name == "search":
            return str(arguments.get("adapter") or "") in LOCAL_ADAPTERS
        if name == "fetch":
            url = str(arguments.get("url") or "")
            if not url or arguments.get("live"):
                return False
            try:
                stored = await self._newest_fetchable(url)
                fetched_at = getattr(stored, "fetched_at", None)
                if not isinstance(fetched_at, datetime):
                    return False
                age_h = (datetime.now(UTC) - fetched_at).total_seconds() / 3600
                return age_h <= get_settings().document_max_age_hours
            except Exception:
                return False
        return False

    def _is_web_tool(self, name: str) -> bool:
        return (
            name in {"search", "fetch", "discovery", "web_search"}
            or name in SOURCES
            or bool(re.search(r"(?:^|[_-])(search|fetch|discovery)(?:$|[_-])", name))
        )

    async def _web_calls_remaining(self, agent_id: int | None) -> int | None:
        if agent_id is None:
            return None
        role = await self._role_of(agent_id)
        settings = get_settings()
        allowed = self._web_call_limits.get(
            agent_id,
            settings.assistant_max_searches
            if role == "assistant"
            else getattr(settings, f"{role}_web_calls", None),
        )
        if allowed is None:
            return None
        count_conditions = [
            SourceEvent.agent_id == agent_id,
            SourceEvent.adapter == "web_call",
        ]
        if agent_id in self._assistant_lookup_agents:
            count_conditions.append(SourceEvent.via == "assistant_lookup")
        else:
            count_conditions.extend(
                (
                    SourceEvent.via.is_distinct_from("citation_autofetch"),
                    SourceEvent.via.is_distinct_from("assistant_lookup"),
                )
            )
        used = (
            await self._session.scalar(
                select(func.count(SourceEvent.id)).where(*count_conditions)
            )
        ) or 0
        return max(0, allowed - used)

    async def _search(self, arguments: dict, agent_id: int | None) -> Any:
        adapter = str(arguments.get("adapter") or "")
        if not str(arguments.get("query") or "").strip():
            return {
                "no_request": True,
                "adapter": adapter,
                "error": "query is required: the words to search for; nothing was spent",
            }
        if (
            agent_id is not None
            and adapter not in LOCAL_ADAPTERS
            and agent_id not in self._assistant_lookup_agents
            and await self._role_of(agent_id) == "assistant"
        ):
            rows = (
                await self._session.execute(
                    select(Turn.content).where(
                        Turn.agent_id == agent_id, Turn.kind == "tool_call"
                    )
                )
            ).scalars().all()
            used = max(
                0,
                sum(
                    1
                    for content in rows
                    if isinstance(content, dict)
                    and content.get("name") == "search"
                    and str((content.get("arguments") or {}).get("adapter") or "")
                    not in LOCAL_ADAPTERS
                )
                - 1,
            )
            allowed = get_settings().assistant_max_searches
            if used >= allowed:
                return {
                    "no_request": True,
                    "error": f"your web search budget of {allowed} is spent; no request was made",
                    "hint": (
                        "search with adapter=store and the corpus tool are free and still "
                        "open. Otherwise cut the direction into fields now with what you "
                        "have, and in a field's rationale say when nothing was retrieved "
                        "behind it."
                    ),
                }
        refusal = _unavailable(adapter)
        if refusal is not None:
            return refusal
        refusal = await self._corpus_first(adapter, agent_id)
        if refusal is not None:
            return {"no_request": True, **refusal}
        refusal = await self._over_quota(adapter, agent_id)
        if refusal is not None:
            return {"no_request": True, **refusal}
        if adapter in COMPOSITES:
            return await self._search_composite(adapter, arguments, agent_id)
        if adapter in LOCAL_ADAPTERS:
            return await self._search_store(arguments, agent_id)
        source = SOURCES.get(adapter)
        if source is None:
            return {
                "no_request": True,
                "error": (
                    f"unknown adapter {adapter!r}; pick one from available, and "
                    "list_sources says what each indexes"
                ),
                "available": adapter_names(),
            }

        surface = Surface(
            adapter=adapter,
            query=str(arguments.get("query") or ""),
            window_months=int(arguments.get("window_months") or 24),
            limit=int(arguments.get("limit") or 20),
        )
        result = await self._harvest(adapter, source, surface, agent_id, via=None)
        documents = result.documents
        if result.error:
            return {
                "adapter": adapter,
                "query": surface.query,
                "window_months": surface.window_months,
                "error": result.error,
                "note": (
                    "this source did not answer, so this is NOT a measurement "
                    "and not an absence. Ask another source, or ask this one "
                    "again with different words."
                ),
            }
        if not documents:
            return {
                "adapter": adapter,
                "query": surface.query,
                "window_months": surface.window_months,
                "count": 0,
                **_denominator(result),
                "note": (
                    "no records. This is a measurement; report it rather than "
                    "retrying the same words."
                )
                + (
                    ""
                    if result.total is None
                    else f" The corpus holds {result.total} record(s) matching "
                    "these words, which is the number to quote."
                ),
            }

        records = []
        for document in documents:
            await self._store(document, agent_id)
            records.append(as_record(document, adapter))
        kinds = {record["date_type"] for record in records}
        return {
            "adapter": adapter,
            "query": surface.query,
            "date_type": kinds.pop() if len(kinds) == 1 else "mixed",
            "count": len(records),
            **_denominator(result),
            "records": records,
        }

    async def _corpus_first(self, adapter: str, agent_id: int | None) -> dict | None:
        if (
            agent_id is None
            or adapter in LOCAL_ADAPTERS
            or not get_corpus().url
            or await self._role_of(agent_id) != "assistant"
        ):
            return None
        asked = await self._session.scalar(
            select(SourceEvent.id)
            .where(SourceEvent.agent_id == agent_id, SourceEvent.adapter == "corpus")
            .limit(1)
        )
        if asked is not None:
            return None
        return {
            "adapter": adapter,
            "error": "search our research corpus first",
            "hint": (
                "call corpus with action=search (or counts) for this question; web "
                "sources open once the corpus has been asked"
            ),
        }

    async def _search_composite(
        self, name: str, arguments: dict, agent_id: int | None
    ) -> Any:
        query = str(arguments.get("query") or "").strip()
        if not query:
            return {"error": "query is required"}
        window = int(arguments.get("window_months") or 24)
        limit = max(1, min(int(arguments.get("limit") or 20), 50))
        per_member = min(50, max(10, limit * 2))
        members = COMPOSITES[name]

        per_source: dict[str, list[dict]] = {}
        answered: list[str] = []
        failed: dict[str, str] = {}
        not_asked: dict[str, str] = {}

        if "store" in members:
            local = await self._search_store(
                {"query": query, "window_months": 0, "limit": per_member}, agent_id
            )
            if local.get("error"):
                failed["store"] = str(local["error"])[:160]
            else:
                answered.append("store")
                if local.get("records"):
                    per_source["store"] = local["records"]

        timeout = get_settings().search_provider_timeout_s
        network = []
        for member in members:
            if member not in SOURCES:
                continue
            refusal = await self._over_quota(member, agent_id)
            if refusal is not None:
                not_asked[member] = str(refusal.get("hint") or refusal.get("error"))
                continue
            automatic = await self._automatic_spent(member, agent_id)
            if automatic is not None:
                not_asked[member] = automatic
                continue
            waiting = self._fetcher.queue_estimate(SOURCES[member].host)
            if waiting > timeout:
                not_asked[member] = f"rate limited, {waiting:.0f}s behind others"
                continue
            network.append(member)
        surfaces = {
            member: Surface(
                adapter=member, query=query, window_months=window, limit=per_member
            )
            for member in network
        }
        totals: dict[str, int] = {}
        outcomes = await asyncio.gather(
            *(
                asyncio.wait_for(
                    self._harvest(
                        member,
                        SOURCES[member],
                        surfaces[member],
                        agent_id,
                        via=name,
                    ),
                    timeout,
                )
                for member in network
            ),
            return_exceptions=True,
        )

        for member, outcome in zip(network, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                failed[member] = (
                    f"timeout after {timeout:g}s"
                    if isinstance(outcome, TimeoutError)
                    else f"{type(outcome).__name__}: {outcome}"[:160]
                )
                continue
            if outcome.error:
                failed[member] = outcome.error[:160]
                continue
            answered.append(member)
            if outcome.total is not None:
                totals[member] = outcome.total
            records = []
            for document in outcome.documents:
                await self._store(document, agent_id)
                records.append(as_record(document, member))
            if records:
                per_source[member] = records

        fused = fuse(per_source, limit)
        payload: dict[str, Any] = {
            "adapter": name,
            "query": query,
            "date_type": "mixed",
            "count": len(fused),
            "sources_answered": sorted(answered),
            "sources_failed": failed,
            "sources_not_asked": not_asked,
            "records": fused,
        }
        if totals:
            payload["total_matched_by_source"] = totals
        if not fused:
            payload["note"] = empty_result_note(answered, failed, not_asked)
        return payload

    async def _harvest(
        self,
        adapter: str,
        source: Any,
        surface: Surface,
        agent_id: int | None,
        via: str | None = None,
    ) -> HarvestResult:

        started = time.monotonic()
        hits_before = self._cache.hits
        cap = SOURCE_WALL_CLOCK_S.get(adapter)
        async with self._fetcher.measure() as traffic:
            if cap is None:
                result = await self._harvest_measured(
                    adapter, source, surface, agent_id, started, traffic
                )
            else:
                try:
                    result = await asyncio.wait_for(
                        self._harvest_measured(
                            adapter, source, surface, agent_id, started, traffic
                        ),
                        cap,
                    )
                except TimeoutError:

                    result = HarvestResult(
                        surface=surface,
                        error=(
                            f"stalled: no answer within {cap:g}s including queue "
                            "time. Not an empty result."
                        ),
                    )
        if result.error and "402" in str(result.error) and adapter in _KEYED_SERVICES:
            _close(_KEYED_SERVICES[adapter], "payment")
        await record_source_event(
            adapter=adapter,
            host=source.host,
            query=surface.query,

            ok=result.error is None,
            items=len(result.documents) if result.error is None else None,
            error=result.error,
            duration_ms=int((time.monotonic() - started) * 1000),
            cached=self._cache.hits > hits_before,
            agent_id=agent_id,
            traffic=traffic,
            via=via,
        )
        return result

    async def _harvest_measured(
        self,
        adapter: str,
        source: Any,
        surface: Surface,
        agent_id: int | None,
        started: float,
        traffic: dict,
    ) -> HarvestResult:
        try:
            raw = await self._cache.run_once(
                ("search", adapter, surface.query, surface.window_months, surface.limit),
                lambda: source.harvest(surface, self._fetcher),
            )

            if adapter == "ddg":
                _note_ddg(challenged=False)
            return (
                raw
                if isinstance(raw, HarvestResult)
                else HarvestResult(surface=surface, documents=raw)
            )
        except Exception as exc:
            if adapter == "ddg":
                _note_ddg(challenged="Challenged" in type(exc).__name__)
            if "402" in str(exc) and adapter in _KEYED_SERVICES:
                _close(_KEYED_SERVICES[adapter], "payment")

            await record_source_event(
                adapter=adapter,
                host=source.host,
                query=surface.query,
                ok=False,
                error=f"{type(exc).__name__}: {exc}"[:300],
                duration_ms=int((time.monotonic() - started) * 1000),
                agent_id=agent_id,
                traffic=traffic,
            )
            raise

    async def _search_store(self, arguments: dict, agent_id: int | None = None) -> Any:

        query = str(arguments.get("query") or "").strip()
        if not query:
            return {"error": "query is required"}
        limit = max(1, min(int(arguments.get("limit") or 20), 50))
        months = int(arguments.get("window_months") or 0)
        started = time.monotonic()

        terms = tsquery_or(query)
        if not terms:
            return {"error": "query has no searchable words", "query": query}
        since = subtract_months(date.today(), months) if months > 0 else None
        common = {"cfg": FTS_CONFIG, "lim": limit, "since": since}

        rows = (
            await self._session.execute(
                text(_store_sql("plainto_tsquery(:cfg, :q)")),
                {**common, "q": query},
            )
        ).mappings().all()
        mode = "all_terms"
        if len(rows) < 3 and " | " in terms:
            rows = (
                await self._session.execute(
                    text(_store_sql("to_tsquery(:cfg, :tsq)")),
                    {**common, "tsq": terms},
                )
            ).mappings().all()
            mode = "any_term"

        await record_source_event(
            adapter="store",
            query=query,
            ok=True,
            items=len(rows),
            duration_ms=int((time.monotonic() - started) * 1000),
            agent_id=agent_id,
        )
        if not rows:
            return {
                "adapter": "store",
                "query": query,
                "count": 0,
                "note": (
                    "nothing stored matches. This is a measurement of what we "
                    "hold, not of the world: try a network adapter."
                ),
            }

        records = [
            {
                "url": row["url"],
                "title": row["title"],
                "published_at": row["published_at"].isoformat()
                if row["published_at"]
                else None,
                "date_type": ADAPTER_DATE_TYPE.get(row["adapter"], "unknown"),
                "found_by": row["adapter"],
                "source_name": row["source_name"],
                "source_tier": row["source_tier"],
                "source_type": row["source_type"],
                "retrieved": row["retrieved"],
                "snippet": row["snippet"],
                "links_to": row["links_to"],
                "rank": round(float(row["rank"]), 4),
            }
            for row in rows
        ]
        first = rows[0]
        kinds = {record["date_type"] for record in records}
        payload: dict[str, Any] = {
            "adapter": "store",
            "query": query,
            "date_type": kinds.pop() if len(kinds) == 1 else "mixed",
            "count": len(records),
            "matched_total": int(first["matched_total"]),
            "match_mode": mode,
            "records": records,
        }
        if mode == "any_term":
            payload["match_note"] = (
                "no document held all of those terms, so this matched any of "
                "them and ranked by how well. Treat the weaker hits as leads."
            )
        if months > 0 and first["undated_total"]:
            payload["undated_excluded"] = int(first["undated_total"])
            payload["undated_note"] = (
                "matches whose publisher gave no date, dropped by the window "
                "rather than judged irrelevant. Drop window_months to see them."
            )
        return payload

    async def _fetch(
        self, arguments: dict, agent_id: int | None, via: str | None = None
    ) -> Any:

        url = str(arguments.get("url") or "")
        if not url:
            return {"error": "url is required"}
        live = bool(arguments.get("live") or False)
        offset = max(0, int(arguments.get("offset") or 0))

        stored = await self._newest_fetchable(url)
        from_store = False
        if stored is not None and not live:
            age_h = (datetime.now(UTC) - stored.fetched_at).total_seconds() / 3600
            from_store = age_h <= get_settings().document_max_age_hours

        if not from_store:
            host = httpx.URL(url).host or "unknown"
            started = time.monotonic()
            read = keyed_reader(url)
            try:
                async with self._fetcher.measure() as traffic:
                    async def fetch_page():
                        if read is not None:
                            return await read(url, self._fetcher), False
                        read_with_metadata = getattr(
                            self._fetcher, "get_text_with_metadata", None
                        )
                        if read_with_metadata is not None:
                            text, was_truncated = await read_with_metadata(url)
                            return text, was_truncated
                        return await self._fetcher.get_text(url), False

                    raw, truncated = (
                        await fetch_page()
                        if live
                        else await self._cache.run_once(("fetch", url), fetch_page)
                    )
            except Exception as exc:
                await record_source_event(
                    adapter="fetch",
                    host=host,
                    query=url,
                    ok=False,
                    error=f"{type(exc).__name__}: {exc}"[:300],
                    duration_ms=int((time.monotonic() - started) * 1000),
                    agent_id=agent_id,
                    via=via,
                )
                raise
            metadata: dict | None = None
            if read is not None:
                extracted = raw
                blocked = (
                    "keyed reader returned empty content"
                    if not isinstance(extracted, str) or not extracted.strip()
                    else None
                )
            else:
                try:
                    extracted, blocked, metadata = await run_page_processing(raw, url=url)
                except PageProcessingTimeout as exc:
                    message = f"page processing exceeded {exc.args[0]:g} s"
                    await record_source_event(
                        adapter="fetch",
                        host=host,
                        query=url,
                        ok=False,
                        error=message,
                        duration_ms=int((time.monotonic() - started) * 1000),
                        agent_id=agent_id,
                        traffic=traffic,
                        via=via,
                    )
                    return {
                        "url": url,
                        "error": message,
                        "stored": False,
                        "note": (
                            "this is a retrieval failure, not an empty page. Do not "
                            "report it as absence of evidence. Try another source."
                        ),
                    }
            if truncated and blocked is None:
                extracted = f"{extracted}\n\n[Response truncated at {MAX_RESPONSE_BYTES} bytes.]"
            await record_source_event(
                adapter="fetch",
                host=host,
                query=url,
                ok=blocked is None,
                items=1 if blocked is None else None,
                error=blocked,
                duration_ms=int((time.monotonic() - started) * 1000),
                agent_id=agent_id,
                traffic=traffic,
                via=via,
            )
            if blocked is not None:
                return {
                    "url": url,
                    "error": f"did not get the page: {blocked}",
                    "stored": False,
                    "note": (
                        "this is a retrieval failure, not an empty page. Do not "
                        "report it as absence of evidence. Try search with "
                        "adapter='store', or another source for the same claim."
                    ),
                }
            stored = await self._store_raw(url, host, extracted, agent_id, raw, metadata)

        assert stored is not None
        if via is not None and from_store:
            await record_source_event(
                adapter="fetch",
                host=httpx.URL(url).host or "unknown",
                query=url,
                ok=True,
                items=1,
                cached=True,
                agent_id=agent_id,
                via=via,
            )
        result = self._window(stored, offset, from_store)
        if not from_store and truncated:
            result["truncated"] = True
            result["note"] += " The downloaded response was truncated at the byte ceiling."
        return result

    async def _corpus(self, arguments: dict, agent_id: int | None) -> Any:
        action = str(arguments.get("action") or "")
        if action not in _CORPUS_ACTIONS:
            return {
                "error": (
                    f"action {action!r} is not one of search, counts or read: search "
                    "finds articles, counts gives the monthly series, read opens one "
                    "article by id"
                )
            }
        if action != "counts" and await self._role_of(agent_id) == "orchestrator":
            return {
                "error": (
                    "the orchestrator may use only action=counts, to check a "
                    "corpus_query; searching and reading the corpus is researchers' work, "
                    "so put what you want looked at in a brief"
                )
            }
        try:
            request = _corpus_request(action, arguments)
        except (TypeError, ValueError) as exc:
            return {"action": action, "error": str(exc)}
        corpus = get_corpus()
        if not corpus.url:
            return {
                "action": action,
                "error": "the research corpus is not configured in this deployment",
                "note": "not a measurement and not an absence; use search and fetch",
            }
        started = time.monotonic()
        label = request.get("query") or str(request.get("id") or "")
        try:
            queries = [request.get("query"), request.get("query_alt")]
            queries = [query for query in queries if query]
            if action == "search":
                found: Any = await asyncio.gather(*(
                    corpus.search(
                        query, request["since"], request["until"],
                        request["lang"], request["limit"], request["oldest_first"],
                        agent_timeout=True,
                    )
                    for query in queries
                ))
            elif action == "counts":
                found = await corpus.series(queries, request["months"], agent_timeout=True)
            else:
                found = await corpus.article(request["id"], agent_timeout=True)
        except Exception as exc:
            reason = str(exc) if isinstance(exc, CorpusUnavailable) else type(exc).__name__
            await record_source_event(
                adapter="corpus",
                query=label,
                via=action,
                ok=False,
                error=reason[:300],
                duration_ms=int((time.monotonic() - started) * 1000),
                agent_id=agent_id,
            )
            return {
                "action": action,
                "error": "the research corpus did not answer",
                "reason": reason,
                "note": (
                    "not a measurement and not an absence. A query matching a large "
                    "part of the corpus can time out: narrow it, or use search and fetch."
                ),
            }
        await record_source_event(
            adapter="corpus",
            query=label,
            via=action,
            ok=True,
            items=(
                sum(len(part) for part in found)
                if action == "search"
                else len(found) if isinstance(found, list) else int(found is not None)
            ),
            duration_ms=int((time.monotonic() - started) * 1000),
            agent_id=agent_id,
        )
        if action == "search":
            return _corpus_search(request, found)
        if action == "counts":
            return _corpus_counts_pair(request, found)
        if found is None:
            return {
                "action": "read",
                "error": (
                    f"no corpus article with id {request['id']}; ids come from the "
                    "results of corpus action=search in this conversation"
                ),
            }
        return await self._corpus_read(found, request["offset"], agent_id)

    async def _corpus_read(self, article: dict, offset: int, agent_id: int | None) -> dict:
        url = article["url"]
        host = article.get("host") or httpx.URL(url).host or "unknown"
        title = (article.get("title") or "").strip()
        body = article.get("text") or ""
        content = body if not title or body.startswith(title) else f"{title}\n\n{body}"
        stored = await self._store_raw(
            url,
            host,
            content,
            agent_id,
            None,
            {
                "title": title or None,
                "site_name": host,
                "lang": (article.get("lang") or "")[:8] or None,
                "published_at": article.get("published"),
                "date_source": (article.get("date_source") or "")[:16] or None,
            },
        )
        result = self._window(
            stored, offset, False, f"corpus action=read id={article['id']}"
        )
        result.pop("from_store", None)
        if result.get("links_to") is None:
            result.pop("links_to", None)
        return {
            "action": "read",
            "id": article["id"],
            "host": host,
            "lang": stored.source_lang,
            "stored": True,
            **result,
        }

    def _window(
        self, document: Document, offset: int, from_store: bool, next_call: str = "fetch"
    ) -> dict:
        cap = get_settings().fetch_part_chars
        total = len(document.content)
        requested_offset = offset
        offset = min(offset, total)
        body = document.content[offset : offset + cap]
        end = offset + len(body)
        parts_total = max(1, (total + cap - 1) // cap)
        part = min(parts_total, offset // cap + 1)
        return {
            "url": document.url,
            "title": document.title,
            "published_at": (
                document.published_at.isoformat() if document.published_at else None
            ),
            "source_name": document.source_name,
            "source_tier": document.source_tier,
            "links_to": document.links_to,
            "content": body,
            "chars_total": total,
            "part": part,
            "parts_total": parts_total,
            "next_offset": end if end < total else None,
            "chars_from": offset,
            "chars_to": end,
            "from_store": from_store,
            **({"fetched_at": document.fetched_at.isoformat()} if from_store else {}),
            "note": (
                f"this is part {part} of {parts_total}; the page has {total} total "
                f"characters. call {next_call} with offset={end} for the next part, and "
                "only if this part did not answer the question."
                if end < total
                else (
                    f"this is part {part} of {parts_total}; the page has {total} total "
                    "characters; there is no next part."
                    if requested_offset >= total and requested_offset > 0 and not body
                    and requested_offset == total
                    else (
                        f"offset {requested_offset} is past the end: the page has {total} "
                        "characters, so nothing was returned. Start at offset 0 or at a "
                        "next_offset from an earlier part."
                        if requested_offset > total
                        else f"this is part {part} of {parts_total}; the page has {total} "
                        "total characters; this is the last part."
                    )
                )
            ),
        }

    async def _rename_topic(self, arguments: dict, agent_id: int | None) -> Any:
        if agent_id is None:
            return {"error": "agent is required"}
        agent = await self._session.get(Agent, agent_id)
        if agent is None or agent.role not in ("researcher", "refuter", "orchestrator"):
            return {
                "error": (
                    "rename_topic is available only to field researchers, refuters, "
                    "and orchestrators"
                )
            }
        if (
            agent.role == "orchestrator"
            and getattr(self, "_partition_worker", False)
            and agent_id != getattr(self, "_owner_agent_id", agent_id)
        ):
            return {"error": "only this shard orchestrator may rename its fields"}
        new_focus = arguments.get("new_focus")
        reason = arguments.get("reason")
        verdict = arguments.get("verdict_on_previous")
        values = (
            ("new_focus", new_focus, 2000),
            ("reason", reason, 500),
            ("verdict_on_previous", verdict, 500),
        )
        for key, value, cap in values:
            if not isinstance(value, str) or not value.strip() or len(value.strip()) > cap:
                return {
                    "error": f"{key} must be a non-empty string no longer than {cap} characters"
                }
        field_id = arguments.get("field_id")
        if agent.role == "orchestrator":
            if type(field_id) is not int or field_id <= 0:
                return {
                    "error": (
                        f"field_id must be a positive integer, got {field_id!r}; the "
                        "orchestrator names which field it renames, by an id from read_fields"
                    )
                }
            field = await self._session.get(Field, field_id)
            if field is None or field.run_id != agent.run_id:
                return {
                    "error": (
                        f"field_id {field_id} is not a field in this run; use an id "
                        "from read_fields"
                    )
                }
            if (
                getattr(self, "_partition_worker", False)
                and getattr(field, "orchestrator_agent_id", None)
                != getattr(self, "_owner_agent_id", agent_id)
            ):
                return {"error": "field belongs to another orchestrator shard"}
        else:
            if field_id is not None:
                return {"error": "field_id is only accepted for orchestrator renames"}
            if agent.role == "researcher":
                field = await self._session.scalar(
                    select(Field).where(
                        Field.run_id == agent.run_id,
                        Field.agent_id == agent.id,
                    )
                )
            else:
                review = await self._session.scalar(
                    select(Refutation).where(
                        Refutation.run_id == agent.run_id,
                        Refutation.refuter_agent_id == agent.id,
                    ).order_by(Refutation.id.desc()).limit(1)
                )
                researcher = (
                    await self._session.get(Agent, review.researcher_agent_id)
                    if review
                    else None
                )
                field = (
                    await self._session.scalar(
                        select(Field).where(
                            Field.run_id == agent.run_id,
                            Field.agent_id == researcher.id,
                        )
                    )
                    if researcher
                    else None
                )
            if field is None:
                return {"error": "agent is not assigned to a field"}
        if field.run_id != agent.run_id:
            return {"error": "agent is not assigned to a field in this run"}
        if agent.role == "researcher" and field.agent_id != agent.id:
            return {"error": "researcher may rename only its assigned field"}
        if agent.role == "refuter" and field.agent_id != researcher.id:
            return {"error": "refuter may rename only the field under review"}
        if getattr(field, "state", None) == "dropped":
            return {"error": "a dropped field cannot be renamed"}
        if agent.role == "refuter":
            review = await self._session.scalar(
                select(Refutation).where(
                    Refutation.run_id == agent.run_id,
                    Refutation.refuter_agent_id == agent.id,
                ).order_by(Refutation.id.desc()).limit(1)
            )
            if review is None or review.state != "pending":
                return {"error": "refuter may rename only during its active review"}
        previous_focus = field.focus
        next_focus = new_focus.strip()
        if previous_focus == next_focus:
            return {
                "error": (
                    "new_focus is the same as the current name, so there is nothing to "
                    "rename; the field keeps its name"
                ),
                "current_focus": previous_focus,
            }
        history = FieldRename(
            field_id=field.id,
            run_id=field.run_id,
            agent_id=agent.id,
            previous_focus=previous_focus,
            new_focus=next_focus,
            reason=reason.strip(),
            verdict_on_previous=verdict.strip(),
        )
        self._session.add(history)
        field.focus = next_focus
        affected_ids = {field.agent_id} if field.agent_id is not None else set()
        if field.agent_id is not None:
            refuter_ids = await self._session.scalars(
                select(Refutation.refuter_agent_id).where(
                    Refutation.researcher_agent_id == field.agent_id,
                    Refutation.run_id == field.run_id,
                )
            )
            affected_ids.update(refuter_ids.all())
        if affected_ids:
            affected_agents = await self._session.scalars(
                select(Agent).where(Agent.id.in_(affected_ids))
            )
            for affected in affected_agents.all():
                affected.field = next_focus
        await self._session.flush()
        return {
            "renamed": True,
            "field_id": field.id,
            "previous_focus": previous_focus,
            "new_focus": next_focus,
            "reason": reason.strip(),
            "verdict_on_previous": verdict.strip(),
        }

    async def _known_technologies(self, arguments: dict, agent_id: int | None) -> Any:
        name = str(arguments.get("name") or "").strip()
        if not name:
            return {"error": "name is required"}

        comparable_name = re.sub(r"\s+", " ", name).strip().casefold()
        escaped_name = (
            comparable_name.replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        prefix = f"{escaped_name}%"
        partial = f"%{escaped_name}%"
        rows = (
            await self._session.execute(
                select(
                    Technology,
                    Alias.surface_form,
                    func.similarity(Alias.surface_form, name).label("similarity"),
                )
                .join(Alias, Alias.technology_id == Technology.id)
                .where(
                    func.lower(func.regexp_replace(Alias.surface_form, r"\s+", " ", "g"))
                    .like(prefix)
                    | func.lower(func.regexp_replace(Alias.surface_form, r"\s+", " ", "g"))
                    .like(partial)
                    | Alias.surface_form.op("%")(name)
                )
                .order_by(
                    (func.lower(Alias.surface_form) == comparable_name).desc(),
                    func.lower(Alias.surface_form).like(prefix).desc(),
                    func.similarity(Alias.surface_form, name).desc(),
                )
                .limit(20)
            )
        ).all()
        if not rows:
            return {
                "query": name,
                "matches": [],
                "note": "nothing stored under this or a near name",
            }
        matches = []
        for row in rows:
            technology, surface = row[0], row[1]
            similarity = row[2] if len(row) > 2 else None
            stored = re.sub(r"\s+", " ", surface or "").strip().casefold()
            if stored == comparable_name:
                how = "exact"
            elif comparable_name in stored:
                how = "stored name contains yours"
            elif stored and stored in comparable_name:
                how = "yours contains the stored name; yours may be narrower or different"
            else:
                how = "similar spelling only"
            matches.append({
                "technology_id": technology.id,
                "canonical_name_ru": technology.canonical_name_ru,
                "canonical_name_en": technology.canonical_name_en,
                "matched_alias": surface,
                "match": how,
                "similarity": round(float(similarity or 0), 2),
            })
        return {
            "query": name,
            "matches": matches,
            "note": (
                "matches are by spelling, not meaning. Bind to a row only when it names "
                "the same technology; a 'similar spelling only' or 'yours contains' match "
                "is often a different thing, and then you open a new one."
            ),
        }

    async def _role_of(self, agent_id: int | None) -> str:

        if agent_id is None:
            return ""
        if agent_id not in self._roles:
            self._roles[agent_id] = (
                await self._session.scalar(select(Agent.role).where(Agent.id == agent_id))
            ) or ""
        return self._roles[agent_id]

    async def _over_quota(self, adapter: str, agent_id: int | None) -> dict | None:

        if adapter not in METERED_ADAPTERS or agent_id is None:
            return None
        allowed = get_settings().quota_for(adapter, await self._role_of(agent_id))
        if allowed is None:
            return None
        used = (
            await self._session.scalar(
                select(func.count(SourceEvent.id)).where(
                    SourceEvent.agent_id == agent_id, SourceEvent.adapter == adapter, BILLED
                )
            )
        ) or 0
        if used < allowed:
            return None
        alternatives = [
            name
            for name in ("discovery", "web_search", "store")
            if name == "store" or adapter not in COMPOSITES.get(name, ())
        ]
        return {
            "error": f"quota spent for {adapter!r}",
            "adapter": adapter,
            "allowed_this_run": allowed,
            "used": used,
            "hint": (
                f"{adapter} is metered and you have used your allowance for this run. "
                "This is a budget, not the source refusing you: do not report it as "
                "an absence. Use "
                + ", ".join(alternatives[:-1])
                + (" or " if len(alternatives) > 1 else "")
                + alternatives[-1]
                + " instead."
            ),
        }

    async def _automatic_spent(self, adapter: str, agent_id: int | None) -> str | None:
        allowed = COMPOSITE_QUOTA.get(adapter)
        if allowed is None or agent_id is None:
            return None
        used = (
            await self._session.scalar(
                select(func.count(SourceEvent.id)).where(
                    SourceEvent.agent_id == agent_id,
                    SourceEvent.adapter == adapter,
                    SourceEvent.via.is_not(None),
                    BILLED,
                )
            )
        ) or 0
        if used < allowed:
            return None
        return (
            f"automatic allowance spent ({used}/{allowed} this run). This is a "
            f"budget, not the source refusing you, and it is not an absence. "
            f"`{adapter}` is billed per query, so a fan-out gets one and the "
            f"rest are yours to ask for: call `{adapter}` by name for as many "
            "as the question needs."
        )

    async def _list_sources(self, arguments: dict, agent_id: int | None) -> Any:
        settings = get_settings()
        role = await self._role_of(agent_id)
        sources = []
        for name, source in sorted(SOURCES.items()):
            row = {
                "adapter": name,
                "host": source.host,
                "date_means": ADAPTER_DATE_TYPE.get(name, "unknown"),
            }
            if _unavailable(name) is not None:
                row["available"] = False
                row["note"] = "not configured in this deployment; calls are refused"
                sources.append(row)
                continue
            allowed = settings.quota_for(name, role)
            if allowed is not None:

                used = (
                    await self._session.scalar(
                        select(func.count(SourceEvent.id)).where(
                            SourceEvent.agent_id == agent_id,
                            SourceEvent.adapter == name,
                            BILLED,
                        )
                    )
                    if agent_id is not None
                    else 0
                ) or 0
                row["calls_left_this_run"] = max(0, allowed - used)
                row["note"] = _METERED_NOTES.get(name, "")
                if allowed == 0:
                    row["note"] = (
                        "Not available to your role in this run. "
                        + str(row["note"])
                    ).strip()
            sources.append(row)
        sources += [
            {
                "adapter": name,
                "host": "(already retrieved, no network)",
                "date_means": ADAPTER_DATE_TYPE.get(name, "unknown"),
                "note": (
                    "everything any agent or the feed poller has ever fetched, "
                    "full-text. Free and instant. Search it before the network: "
                    "a hit here is a page we already hold a real copy of."
                ),
            }
            for name in sorted(LOCAL_ADAPTERS)
        ]
        blurbs = {
            "discovery": (
                "START HERE. Asks every source at once and merges them into one "
                "ranked list. Each record says which sources found it, so a url "
                "several independent indexes returned is distinguishable from "
                "one only we hold."
            ),
            "web_search": (
                "The open web as it is, unblended and unreranked. Use it to "
                "judge how widely something is ALREADY discussed, which is the "
                "question discovery cannot answer about its own results."
            ),
        }
        sources += [
            {
                "adapter": name,
                "host": "(several sources: " + ", ".join(members) + ")",
                "date_means": ADAPTER_DATE_TYPE.get(name, "unknown"),
                "note": blurbs.get(name, ""),
            }
            for name, members in sorted(COMPOSITES.items())
        ]
        return {"sources": sources}

    async def _newest(self, url: str) -> Document | None:
        return await newest_document(self._session, url)

    async def _newest_fetchable(self, url: str) -> Document | None:
        return await newest_fetchable_document(self._session, url)

    async def _store(self, document, agent_id: int | None) -> None:
        await store_document(self._session, document, agent_id)

    async def _store_raw(
        self,
        url: str,
        host: str,
        text: str,
        agent_id: int | None,
        raw: str | None = None,
        metadata: dict | None = None,
    ) -> Document:
        meta = metadata if metadata is not None else (page_metadata(raw, url) if raw else {})
        statement = (
            insert(Document.__table__)
            .values(
                url=url,
                title=meta.get("title") or host,
                content=text,
                content_sha=content_sha(text),
                retrieved=True,
                source_name=meta.get("site_name") or host,
                source_type="page",
                source_lang=meta.get("lang") or "und",
                published_at=(
                    meta.get("published_at")
                    if meta.get("published_at") is not None
                    and meta["published_at"] <= date.today()
                    else None
                ),
                date_source=(
                    meta.get("date_source")
                    if meta.get("published_at") is not None
                    and meta["published_at"] <= date.today()
                    else None
                ),
                source_tier=host_tier(host),
                fetched_by_agent_id=agent_id,
            )
            .on_conflict_do_nothing(index_elements=["url", "content_sha"])
        )
        await self._session.execute(statement)
        stored = await self._session.scalar(
            select(Document)
            .where(Document.url == url, Document.content_sha == content_sha(text))
            .limit(1)
        )
        if stored is None:
            raise RuntimeError(f"document vanished after insert: {url}")
        return stored

    async def _same_name(
        self, surface: str, name_ru: str | None, name_en: str | None
    ) -> Technology | None:
        names = {
            re.sub(r"\s+", " ", value).strip().casefold()
            for value in (surface, name_ru, name_en)
            if value and value.strip()
        }
        if not names:
            return None

        def normal(column: Any) -> Any:
            return func.lower(func.regexp_replace(column, r"\s+", " ", "g"))

        return await self._session.scalar(
            select(Technology)
            .outerjoin(Alias, Alias.technology_id == Technology.id)
            .where(
                normal(Technology.canonical_name_en).in_(names)
                | normal(Technology.canonical_name_ru).in_(names)
                | normal(Alias.surface_form).in_(names)
            )
            .order_by(Technology.id)
            .limit(1)
        )

    async def _merge_technologies(self, arguments: dict, agent_id: int | None) -> Any:
        reason = str(arguments.get("reason") or "").strip()
        try:
            source_id = int(arguments.get("from_id"))
            target_id = int(arguments.get("into_id"))
        except (TypeError, ValueError):
            return {
                "error": (
                    "from_id and into_id must be technology ids from known_technologies: "
                    "from_id is the duplicate that disappears, into_id the row that stays"
                )
            }
        if not reason:
            return {"error": "reason is required: why these two rows are one technology"}
        if source_id == target_id:
            return {"error": f"from_id and into_id are both {source_id}; nothing to merge"}
        source = await self._session.get(Technology, source_id)
        target = await self._session.get(Technology, target_id)
        missing = [
            str(key) for key, row in ((source_id, source), (target_id, target)) if row is None
        ]
        if missing:
            return {
                "error": (
                    f"no technology {', '.join(missing)} in the store; take ids from "
                    "known_technologies"
                )
            }
        moved = {}
        for model, column in (
            (Alias, Alias.technology_id),
            (Field, Field.technology_id),
            (Entry, Entry.technology_id),
        ):
            result = await self._session.execute(
                update(model).where(column == source_id).values(technology_id=target_id)
            )
            moved[model.__tablename__] = result.rowcount or 0
        summary = {
            "from_id": source_id,
            "from_name": source.canonical_name_en or source.canonical_name_ru,
            "into_id": target_id,
            "into_name": target.canonical_name_en or target.canonical_name_ru,
            "moved": moved,
            "reason": reason,
            "agent_id": agent_id,
        }
        await self._session.delete(source)
        run_id = await self._run_of(agent_id)
        if run_id is not None:
            self._session.add(
                RunEvent(run_id=run_id, kind="technologies_merged", payload=summary)
            )
        await self._session.flush()
        return {"merged": True, **summary}

    async def _unbind_alias(self, arguments: dict, agent_id: int | None) -> Any:
        surface = re.sub(r"\s+", " ", str(arguments.get("surface_form") or "")).strip()
        reason = str(arguments.get("reason") or "").strip()
        if not surface or not reason:
            return {
                "error": (
                    "surface_form and reason are required: the wording to detach and why "
                    "it names a different technology"
                )
            }
        aliases = (
            await self._session.scalars(
                select(Alias).where(func.lower(Alias.surface_form) == surface.casefold())
            )
        ).all()
        if not aliases:
            return {
                "error": (
                    f"no stored wording {surface!r}; known_technologies shows the "
                    "matched_alias values that exist"
                )
            }
        removed = []
        for alias in aliases:
            siblings = await self._session.scalar(
                select(func.count(Alias.id)).where(
                    Alias.technology_id == alias.technology_id, Alias.id != alias.id
                )
            )
            if not siblings:
                return {
                    "error": (
                        f"{surface!r} is the only wording of technology "
                        f"{alias.technology_id}; unbinding it would orphan the row. Merge "
                        "the row into the right one with merge_technologies instead."
                    )
                }
            removed.append({"technology_id": alias.technology_id, "lang": alias.lang})
            await self._session.delete(alias)
        run_id = await self._run_of(agent_id)
        if run_id is not None:
            self._session.add(RunEvent(
                run_id=run_id,
                kind="alias_unbound",
                payload={"surface_form": surface, "removed": removed, "reason": reason},
            ))
        await self._session.flush()
        return {"unbound": True, "surface_form": surface, "removed": removed}

    async def _run_of(self, agent_id: int | None) -> int | None:
        if agent_id is None:
            return None
        return await self._session.scalar(select(Agent.run_id).where(Agent.id == agent_id))

    async def _open_technology(self, arguments: dict, agent_id: int | None) -> Any:
        surface = str(arguments.get("surface_form") or "").strip()
        lang = str(arguments.get("lang") or "").strip() or "ru"
        name_ru = (arguments.get("name_ru") or "").strip() or None
        name_en = (arguments.get("name_en") or "").strip() or None
        if not surface:
            return {"error": "surface_form is required"}

        requested = arguments.get("technology_id")
        binding = requested not in (None, "", 0, "0")
        target: Technology | None = None
        if not binding and not name_ru and not name_en:
            bound = await self._session.scalar(
                select(Alias).where(func.lower(Alias.surface_form) == surface.lower())
            )
            if bound is not None:
                return {
                    "technology_id": bound.technology_id,
                    "surface_form": bound.surface_form,
                    "opened": False,
                    "note": "this surface form was already bound to a technology",
                }
        if binding:
            try:
                candidate = int(requested)
            except (TypeError, ValueError):
                return {
                    "error": (
                        f"technology_id {requested!r} is not an id; take it from "
                        "known_technologies, or omit it to open a new technology"
                    )
                }
            target = await self._session.get(Technology, candidate)
            if target is None:
                return {
                    "error": f"no technology {candidate} in the store",
                    "note": (
                        "an id you did not get back from a tool is not an id. "
                        "look it up with known_technologies first."
                    ),
                }
        elif not name_ru and not name_en:
            return {
                "error": (
                    "a new technology needs name_ru or name_en (ideally both); to bind "
                    "this phrasing to an existing one, pass its technology_id instead"
                )
            }
        else:
            target = await self._same_name(surface, name_ru, name_en)
            if target is not None:
                binding = True

        existing = await self._session.scalar(
            select(Alias).where(
                func.lower(Alias.surface_form) == surface.lower(),
                Alias.lang == lang,
            )
        )
        if existing is not None:
            return {
                "technology_id": existing.technology_id,
                "surface_form": existing.surface_form,
                "opened": False,
                "note": "this surface form was already bound to a technology",
            }

        if target is None:
            target = Technology(canonical_name_ru=name_ru, canonical_name_en=name_en)
            self._session.add(target)
            await self._session.flush()
        self._session.add(
            Alias(
                technology_id=target.id,
                surface_form=surface,
                lang=lang,
                first_seen_by_agent_id=agent_id,
            )
        )
        await self._session.flush()
        return {
            "technology_id": target.id,
            "canonical_name_ru": target.canonical_name_ru,
            "canonical_name_en": target.canonical_name_en,
            "surface_form": surface,
            "opened": not binding,
            "note": (
                (
                    "this wording is now bound to the technology that was already "
                    "there; no second row was created"
                    if requested not in (None, "", 0, "0")
                    else "a technology with this exact name was already stored, so your "
                    "wording is bound to it and no second row was created; if it is a "
                    "different thing, open it under a more specific name"
                )
                if binding
                else "a technology the store had not seen"
            ),
        }


async def record_source_event(
    *,
    adapter: str,
    ok: bool,
    host: str | None = None,
    query: str | None = None,
    items: int | None = None,
    error: str | None = None,
    duration_ms: int | None = None,
    cached: bool = False,
    agent_id: int | None = None,
    traffic: dict | None = None,
    via: str | None = None,
) -> None:
    try:
        from wsignal.db import get_sessionmaker

        async with get_sessionmaker()() as session:
            session.add(
                SourceEvent(
                    adapter=adapter,
                    host=host,
                    query=query,
                    via=via,
                    ok=ok,
                    items=items,
                    error=error,
                    duration_ms=duration_ms,
                    cached=cached,
                    agent_id=agent_id,
                    bytes=None if traffic is None else traffic.get("bytes"),
                    requests=None if traffic is None else traffic.get("requests"),
                )
            )
            await session.commit()
    except Exception:
        pass


async def store_document(
    session: AsyncSession, document: Any, agent_id: int | None = None
) -> None:
    text = document.text or document.title
    statement = (
        insert(Document.__table__)
        .values(
            url=document.url,
            title=document.title,
            content=text,
            content_sha=content_sha(text),
            retrieved=document.retrieved,
            links_to=document.links_to,
            source_name=document.source_name,
            source_type=document.source_type,
            source_lang=document.source_lang,
            source_tier=document.source_tier,
            published_at=(
                document.published_at
                if document.published_at is not None
                and document.published_at <= date.today()
                else None
            ),
            date_source=(
                getattr(document, "date_source", None)
                if document.published_at is not None
                and document.published_at <= date.today()
                else None
            ),
            adapter=document.adapter or None,
            fetched_by_agent_id=agent_id,
        )
        .on_conflict_do_nothing(index_elements=["url", "content_sha"])
    )
    await session.execute(statement)


def _corpus_day(value: Any, key: str) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise ValueError(f"{key} must be a date written YYYY-MM-DD") from exc


def _corpus_request(action: str, arguments: dict) -> dict:
    if action == "read":
        article_id = arguments.get("id")
        if isinstance(article_id, bool) or int(article_id or 0) <= 0:
            raise ValueError("id must be a positive article id from action=search")
        return {"id": int(article_id), "offset": max(0, int(arguments.get("offset") or 0))}
    query = " ".join(str(arguments.get("query") or "").split())
    if not 1 <= len(query) <= 200:
        raise ValueError("query must be nonblank and at most 200 characters")
    query_alt = " ".join(str(arguments.get("query_alt") or "").split()) or None
    if query_alt is not None and len(query_alt) > 200:
        raise ValueError("query_alt must be at most 200 characters")
    if query_alt is not None and query_alt.casefold() == query.casefold():
        query_alt = None
    if action == "counts":
        raw_months = arguments.get("months")
        months = int(raw_months) if raw_months not in (None, "") else 36
        if not 1 <= months <= 120:
            raise ValueError(f"months must be between 1 and 120, got {months}")
        return {"query": query, "query_alt": query_alt, "months": months}
    since = _corpus_day(arguments.get("since"), "since")
    until = _corpus_day(arguments.get("until"), "until")
    if since and until and since > until:
        raise ValueError("since must not be later than until")
    lang = str(arguments.get("lang") or "").strip().casefold()[:8] or None
    order = str(arguments.get("order") or "newest")
    if order not in ("newest", "oldest"):
        raise ValueError("order must be newest or oldest")
    return {
        "query": query,
        "query_alt": query_alt,
        "since": since,
        "until": until,
        "lang": lang,
        "limit": _corpus_limit(arguments),
        "order": order,
        "oldest_first": order == "oldest",
    }


def _corpus_limit(arguments: dict) -> int:
    settings = get_settings()
    requested = arguments.get("limit")
    if requested is None:
        requested = settings.corpus_search_default_limit
    return max(1, min(int(requested), settings.corpus_search_max_limit))


def _corpus_snippet(row: dict, query: str, cap: int) -> str:
    raw = str(row.get("snippet") or "")
    snippet = _clean_search_snippet(raw, query, cap)
    title = " ".join(str(row.get("title") or "").split())
    if "**" not in raw and title and snippet.startswith(title):
        snippet = snippet[len(title):].strip(" .:-–—|")
    return snippet


def _corpus_hits(request: dict, rows: list[dict]) -> dict:
    cap = getattr(get_settings(), "search_snippet_chars", 200)
    payload: dict[str, Any] = {
        "action": "search",
        "query": request["query"],
        "order": request["order"],
        **{
            key: request[key].isoformat() if key != "lang" else request[key]
            for key in ("since", "until", "lang")
            if request[key]
        },
        "matched": int(rows[0]["matched"]) if rows else 0,
        "matched_at_least": bool(rows[0]["matched_at_least"]) if rows else False,
        "shown": len(rows),
        "hits": [
            {
                "id": row["id"],
                "title": (row.get("title") or "")[:_CORPUS_TITLE_CHARS],
                "host": row.get("host"),
                "date": row["published"].isoformat() if row.get("published") else None,
                "lang": row.get("lang"),
                "snippet": _corpus_snippet(row, request["query"], cap),
                **({"copies": row["copies"]} if (row.get("copies") or 1) > 1 else {}),
            }
            for row in rows
        ],
    }
    first = rows[0] if rows else {}
    if first.get("candidates") is not None:
        payload["candidates_considered"] = first["candidates"]
        payload["duplicates_folded"] = first.get("folded", 0)
        hosts = first.get("hosts") or {}
        payload["hosts_in_candidates"] = hosts
        if hosts and first["candidates"]:
            host, count = max(hosts.items(), key=lambda item: item[1])
            if count * 2 > first["candidates"]:
                payload["host_note"] = (
                    f"{host} holds {count} of {first['candidates']} candidates; the hits "
                    "are spread across hosts, so read this as one outlet's coverage, "
                    "not independent sources"
                )
    if not rows:
        payload["note"] = (
            "no article in the corpus matches. This measures our sample of the "
            "trade and tech press, not the web."
        )
    return payload


_CYRILLIC = re.compile(r"[Ѐ-ӿ]")
_LATIN = re.compile(r"[A-Za-z]")


def _language_note(query: str, langs: set[str]) -> str | None:
    cyrillic = bool(_CYRILLIC.search(query))
    latin_words = bool(_LATIN.search(_CYRILLIC.sub("", query)))
    if cyrillic and not latin_words:
        return (
            "a Russian-only query reaches only Russian-language articles, and most of the "
            "corpus is English: pass the English terms as query_alt to cover both"
        )
    if len(langs) == 1 and next(iter(langs)) not in ("en", None, ""):
        lang = next(iter(langs))
        return (
            f"every hit is in {lang}: words match as written, so pass the terms in the "
            "other language as query_alt to cover both"
        )
    return None


def _corpus_search(request: dict, parts: list[list[dict]]) -> dict:
    if len(parts) == 1:
        payload = _corpus_hits(request, parts[0])
        note = _language_note(request["query"], {row.get("lang") for row in parts[0]})
        if note:
            payload["language_note"] = note
        return payload
    merged: list[dict] = []
    seen: set = set()
    longest = max(len(part) for part in parts)
    for index in range(longest):
        for part in parts:
            if index < len(part) and part[index]["id"] not in seen:
                seen.add(part[index]["id"])
                merged.append(part[index])
    merged = merged[: request["limit"]]
    matched = sum(int(part[0]["matched"]) for part in parts if part)
    at_least = any(bool(part[0]["matched_at_least"]) for part in parts if part)
    rows = [
        {**row, "matched": matched, "matched_at_least": at_least}
        for row in merged
    ]
    payload = _corpus_hits(request, rows)
    payload["query_alt"] = request["query_alt"]
    payload["by_query"] = [
        {
            "query": query,
            "matched": int(part[0]["matched"]) if part else 0,
            "matched_at_least": bool(part[0]["matched_at_least"]) if part else False,
            "shown": sum(1 for row in merged if row in part),
        }
        for query, part in zip((request["query"], request["query_alt"]), parts, strict=True)
    ]
    return payload


def _corpus_counts_pair(request: dict, series: list[dict]) -> dict:
    if len(series) == 1:
        payload = _corpus_counts(request, series[0])
        note = _language_note(request["query"], set())
        if note:
            payload["language_note"] = note
        return payload
    first, second = series
    by_month = {month["m"]: month["n"] for month in second["months"]}
    combined = {
        "months": [
            {**month, "n": month["n"] + by_month.get(month["m"], 0)}
            for month in first["months"]
        ],
        "matched_total": first["matched_total"] + second["matched_total"],
        "too_broad": bool(first["too_broad"] or second["too_broad"]),
    }
    payload = _corpus_counts(request, combined)
    payload["query_alt"] = request["query_alt"]
    sides = [
        _corpus_counts({**request, "query": query}, part)
        for query, part in ((request["query"], first), (request["query_alt"], second))
    ]
    payload["by_query"] = [
        {
            "query": side["query"],
            "matched_total": side["matched_total"],
            "share_ratio": side.get("share_ratio"),
        }
        for side in sides
    ]
    small, large = sorted(max(1, side["matched_total"]) for side in sides)
    ratios = [side.get("share_ratio") for side in sides]
    if large >= 3 * small or (
        None not in ratios and (ratios[0] - 1) * (ratios[1] - 1) < 0
    ):
        payload["balance_note"] = (
            "the two queries differ a lot in size or move in opposite directions, so "
            "the summed series mostly follows one of them; read by_query before "
            "drawing a trend, and make both sides name the same technology"
        )
    return payload


def _corpus_share(matches: int, total: int) -> float | None:
    return round(matches * 10000 / total, 2) if total else None


def _corpus_counts(request: dict, series: dict) -> dict:
    months = series["months"]
    matches = [month["n"] for month in months]
    totals = [month["total"] for month in months]
    span = min(_CORPUS_RECENT_MONTHS, len(months) // 2)
    payload: dict[str, Any] = {
        "action": "counts",
        "query": request["query"],
        "from": months[0]["m"],
        "to": months[-1]["m"],
        "matches": matches,
        "totals": totals,
        "share_per_10k": [
            _corpus_share(count, total) for count, total in zip(matches, totals, strict=True)
        ],
        "matched_total": series["matched_total"],
        "too_broad": series["too_broad"],
    }
    if span:
        recent = _corpus_share(sum(matches[-span:]), sum(totals[-span:]))
        previous = _corpus_share(
            sum(matches[-2 * span:-span]), sum(totals[-2 * span:-span])
        )
        payload.update({
            "recent_months": span,
            "recent_share_per_10k": recent,
            "previous_share_per_10k": previous,
            "share_ratio": (
                round(recent / previous, 2) if recent is not None and previous else None
            ),
        })
    payload["note"] = (
        "compare shares, not raw matches: the corpus grows over the window. "
        f"{payload['to']} is still in progress."
        + (
            " too_broad: the query matches over 2% of the corpus, so it names "
            "something wider than one technology."
            if series["too_broad"]
            else ""
        )
    )
    return payload


def empty_result_note(
    answered: list[str], failed: dict[str, str], not_asked: dict[str, str]
) -> str:
    if answered:
        return (
            f"nothing found. This is a measurement across {len(answered)} "
            "source(s) that answered"
            + (f", {len(failed)} that failed" if failed else "")
            + (
                f" and {len(not_asked)} never asked (see sources_not_asked)"
                if not_asked
                else ""
            )
            + "; report it rather than retrying the same words."
        )
    return (
        "NOT A MEASUREMENT. No source answered this query, so nothing here is "
        "evidence of absence. "
        + (f"{len(failed)} failed. " if failed else "")
        + (
            f"{len(not_asked)} were not asked at all - our own budget or rate "
            "limit, not the source. `sources_not_asked` says which and why, and "
            "naming an adapter directly is not capped the way a fan-out is. "
            if not_asked
            else ""
        )
        + "Ask a different source before concluding anything."
    )


def render_tool_result(
    payload: Any,
    name: str | None = None,
    arguments: dict | None = None,
) -> str:
    if isinstance(payload, dict):
        payload = _model_search_payload(payload, name, arguments)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


_METERED_NOTES: dict[str, str] = {
    "brave": (
        "The open web, billed per query. This is what `web_search` runs on, so "
        "the allowance is shared between calling it directly and calling "
        "web_search. Results are LEADS: the snippet is Brave's, not the page, "
        "so fetch before quoting."
    ),
    "epo": (
        "European and PCT patent publications, title and abstract, by publication "
        "date. A filing is the earliest DATED public artifact most technologies "
        "ever get, so this is the instrument for 'how long has this existed' "
        "rather than for 'who is talking about it'. Metered: spend it on a "
        "technology you already have a claim about, not on opening a field."
    ),
    "rospatent": (
        "Russian patents, live to 2026. The Western aggregators lose Russian data "
        "after 2021 and this does not, so it is the only source here that can "
        "answer whether something is happening in Russia. Frozen for PCT, CN, JP "
        "and DE - for those use epo. Its `total` is an exact denominator even "
        "when it returns few rows. Metered."
    ),
    "x": (
        "Posts from the last seven days. A primary indicator that may raise a "
        "lead and may never be the sole basis for a claim - something else has to "
        "carry it. Retweets are excluded, because one text reposted forty times "
        "is one source. Costs money per post read; metered hard."
    ),
}


def _denominator(result: HarvestResult) -> dict:
    out: dict[str, Any] = {}
    if result.total is not None:
        out["total_matched"] = result.total
    if result.available is not None and result.available != result.total:
        out["available_to_page"] = result.available
        out["available_note"] = (
            "available is how many can be paged, not how many exist. "
            "Quote total_matched."
        )
    return out
