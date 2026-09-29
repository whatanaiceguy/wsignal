from __future__ import annotations

import asyncio
import base64
import time
from datetime import date
from typing import Any

from wsignal.config import get_settings
from wsignal.parsing.base import (
    TIER_AUTHORITATIVE,
    Document,
    HarvestResult,
    Surface,
)
from wsignal.parsing.dates import valid_publication_date

TOKEN_URL = "https://ops.epo.org/3.2/auth/accesstoken"
SEARCH_URL = "https://ops.epo.org/3.2/rest-services/published-data/search/biblio"

MAX_RANGE = 100


class MissingCredentials(RuntimeError):
    pass


class _Token:

    def __init__(self) -> None:
        self.value: str | None = None
        self.expires_at = 0.0
        self.lock = asyncio.Lock()

    async def get(self, fetcher: Any) -> str:
        settings = get_settings()
        key, secret = settings.epo_consumer_key, settings.epo_consumer_secret_key
        if not (key and secret):
            raise MissingCredentials(
                "EPO_CONSUMER_KEY and EPO_CONSUMER_SECRET_KEY are not set"
            )
        async with self.lock:
            if self.value and time.monotonic() < self.expires_at:
                return self.value
            basic = base64.b64encode(f"{key}:{secret}".encode()).decode()
            payload = await fetcher.request_json(
                "POST",
                TOKEN_URL,
                headers={
                    "Authorization": f"Basic {basic}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={"grant_type": "client_credentials"},
            )
            self.value = str(payload["access_token"])
            lifetime = float(payload.get("expires_in", 1200))
            self.expires_at = time.monotonic() + max(60.0, lifetime - 60)
            return self.value


_TOKEN = _Token()


class EpoSource:
    name = "epo"
    host = "ops.epo.org"

    async def harvest(self, surface: Surface, fetcher: Any) -> list[Document]:
        token = await _TOKEN.get(fetcher)
        today = date.today()
        first = today.year - max(1, surface.window_months) // 12
        term = surface.query.replace('"', " ").strip()
        query = f'(ti="{term}" or ab="{term}") and pd within "{first} {today.year}"'
        payload = await fetcher.request_json(
            "GET",
            SEARCH_URL,
            params={"q": query, "Range": f"1-{min(MAX_RANGE, max(1, surface.limit))}"},
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        return HarvestResult(
            surface=surface,
            documents=[
                document
                for record in _documents(payload)
                if (document := _one(record, self.name)) is not None
            ],
            total=_total(payload),
        )


def _documents(payload: Any) -> list[dict]:
    node = payload
    for key in (
        "ops:world-patent-data",
        "ops:biblio-search",
        "ops:search-result",
        "exchange-documents",
    ):
        if not isinstance(node, dict):
            return []
        node = node.get(key)
    return [item for item in _as_list(node) if isinstance(item, dict)]


def _total(payload: Any) -> int | None:
    node = payload
    for key in ("ops:world-patent-data", "ops:biblio-search"):
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    if not isinstance(node, dict):
        return None
    try:
        return int(node.get("@total-result-count"))
    except (TypeError, ValueError):
        return None


def _as_list(value: Any) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("$", "")).strip()
    if isinstance(value, str):
        return value.strip()
    return ""


def _one(record: dict, adapter: str) -> Document | None:
    document = record.get("exchange-document")
    document = document[0] if isinstance(document, list) and document else document
    if not isinstance(document, dict):
        return None

    country = str(document.get("@country") or "").strip()
    number = str(document.get("@doc-number") or "").strip()
    kind = str(document.get("@kind") or "").strip()
    if not (country and number):
        return None
    publication = f"{country}{number}{kind}"

    biblio = document.get("bibliographic-data")
    biblio = biblio if isinstance(biblio, dict) else {}

    title, lang = _title(biblio)
    if not title:
        title = publication
    abstract = _abstract(document)

    return Document(
        url=f"https://worldwide.espacenet.com/patent/search?q=pn%3D{publication}",
        title=title,
        text=f"{title}\n\n{abstract}".strip(),
        source_name="European Patent Office",
        source_type="patent",
        source_lang=lang,
        source_tier=TIER_AUTHORITATIVE,
        published_at=(published := _published(biblio)),
        date_source="api" if published is not None else None,
        adapter=adapter,
        retrieved=True,
    )


def _title(biblio: dict) -> tuple[str, str]:
    for entry in _as_list(biblio.get("invention-title")):
        text = _text(entry)
        if text:
            lang = "en"
            if isinstance(entry, dict):
                lang = str(entry.get("@lang") or "en").lower()[:2]
            return text, lang
    return "", "en"


def _abstract(document: dict) -> str:
    parts: list[str] = []
    for entry in _as_list(document.get("abstract")):
        if not isinstance(entry, dict):
            continue
        for paragraph in _as_list(entry.get("p")):
            text = _text(paragraph)
            if text:
                parts.append(text)
    return "\n".join(parts)


def _published(biblio: dict) -> date | None:
    reference = biblio.get("publication-reference")
    reference = reference if isinstance(reference, dict) else {}
    for entry in _as_list(reference.get("document-id")):
        if not isinstance(entry, dict):
            continue
        raw = _text(entry.get("date"))
        if len(raw) == 8 and raw.isdigit():
            try:
                return valid_publication_date(
                    date(int(raw[:4]), int(raw[4:6]), int(raw[6:]))
                )
            except ValueError:
                continue
    return None
