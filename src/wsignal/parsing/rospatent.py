from __future__ import annotations

import html
import re
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

BASE = "https://searchplatform.rospatent.gov.ru/patsearch/v0.2"
SEARCH_URL = f"{BASE}/search"
DOCS_PREFIX = f"{BASE}/docs/"

_TAGS = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"[ \t]*\n[ \t]*")


class MissingCredentials(RuntimeError):
    pass


class RospatentSource:
    name = "rospatent"
    host = "searchplatform.rospatent.gov.ru"

    async def harvest(self, surface: Surface, fetcher: Any) -> list[Document]:
        settings = get_settings()
        if not settings.rospatent_api_key:
            raise MissingCredentials("SEARCHPLATFORM_ROSPATENT_API is not set")

        body: dict[str, Any] = {
            "q": surface.query,
            "limit": max(1, min(100, surface.limit)),
            "offset": 0,
            "sort": "publication_date:desc",
        }
        window = _window_start(surface.window_months)
        if window:
            body["filter"] = {"date_published": {"range": {"gte": window}}}

        payload = await fetcher.request_json(
            "POST",
            SEARCH_URL,
            headers={
                "Authorization": f"Bearer {settings.rospatent_api_key}",
                "Content-Type": "application/json",
            },
            json=body,
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("hits"), list):
            raise ValueError("Rospatent response is missing a hits list")
        hits = payload["hits"]
        return HarvestResult(
            surface=surface,
            documents=[
                document
                for hit in (hits or [])
                if isinstance(hit, dict)
                and (document := _one(hit, self.name)) is not None
            ],
            total=_int(payload.get("total")),
            available=_int(payload.get("available")),
        )


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _window_start(window_months: int) -> str:
    today = date.today()
    months = max(0, window_months)
    year = today.year - months // 12
    month = today.month - months % 12
    if month < 1:
        month += 12
        year -= 1
    return f"{year:04d}{month:02d}01"


def _plain(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    out = value
    for tag in ("<em>", "</em>", "<b>", "</b>"):
        out = out.replace(tag, "")
    return out.strip()


def _date(value: Any) -> date | None:
    raw = str(value or "").strip()
    digits = raw.replace(".", "").replace("-", "")
    if len(digits) != 8 or not digits.isdigit():
        return None
    year = int(digits[:4])
    if not 1900 <= year <= date.today().year + 1:
        return None
    try:
        return valid_publication_date(
            date(year, int(digits[4:6]), int(digits[6:]))
        )
    except ValueError:
        return None


def _biblio_title(hit: dict) -> str:
    biblio = hit.get("biblio")
    if not isinstance(biblio, dict):
        return ""
    for lang in ("ru", "en"):
        entry = biblio.get(lang)
        if isinstance(entry, dict):
            title = entry.get("title")
            if isinstance(title, str) and title.strip():
                return _plain(title)
    return ""


def _one(hit: dict, adapter: str) -> Document | None:
    identifier = str(hit.get("id") or "").strip()
    if not identifier:
        return None

    common = hit.get("common")
    common = common if isinstance(common, dict) else {}
    snippet = hit.get("snippet")
    snippet = snippet if isinstance(snippet, dict) else {}

    title = _plain(snippet.get("title")) or _biblio_title(hit) or identifier
    description = _plain(snippet.get("description"))
    holder = _plain(snippet.get("patentee")) or _plain(snippet.get("applicant"))

    text = "\n\n".join(part for part in (title, description) if part)
    if holder:
        text = f"{text}\n\n{holder}"

    return Document(
        url=f"{BASE}/docs/{identifier}",
        title=title,
        text=text,
        source_name="Роспатент",
        source_type="patent",
        source_lang="ru",
        source_tier=TIER_AUTHORITATIVE,
        published_at=(published := _date(common.get("publication_date"))),
        date_source="api" if published is not None else None,
        adapter=adapter,
        retrieved=False,
    )


def is_document_url(url: str) -> bool:
    return url.startswith(DOCS_PREFIX)


async def document_text(url: str, fetcher: Any) -> str:
    settings = get_settings()
    if not settings.rospatent_api_key:
        raise MissingCredentials("SEARCHPLATFORM_ROSPATENT_API is not set")
    payload = await fetcher.request_json(
        "GET",
        url,
        headers={"Authorization": f"Bearer {settings.rospatent_api_key}"},
    )
    if not isinstance(payload, dict):
        raise ValueError("Rospatent document response is not an object")
    parts: list[str] = []
    for section in ("abstract", "claims", "description"):
        node = payload.get(section)
        if not isinstance(node, dict):
            continue
        for lang in ("ru", "en"):
            markup = node.get(lang)
            if isinstance(markup, str) and markup.strip():
                text = _strip_markup(markup)
                if text:
                    parts.append(text)
                break
    return "\n\n".join(parts)


def _strip_markup(markup: str) -> str:
    text = html.unescape(_TAGS.sub(" ", markup))
    text = _SPACE.sub("\n", text)
    return "\n".join(
        " ".join(line.split()) for line in text.splitlines() if line.strip()
    ).strip()
