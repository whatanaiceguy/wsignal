from __future__ import annotations

from datetime import date
from typing import Any

from wsignal.config import get_settings
from wsignal.parsing.base import TIER_AUTHORITATIVE, Document, Surface
from wsignal.parsing.dates import subtract_months, valid_publication_date
from wsignal.parsing.http import Fetcher


class OpenAlexSource:
    name = "openalex"
    host = "api.openalex.org"

    async def harvest(self, surface: Surface, fetcher: Fetcher) -> list[Document]:
        today = date.today()
        start = subtract_months(today, surface.window_months)
        settings = get_settings()
        params: dict[str, Any] = {
            "search": surface.query,
            "filter": (
                f"from_publication_date:{start.isoformat()},"
                f"to_publication_date:{today.isoformat()}"
            ),
            "per-page": max(0, surface.limit),
        }
        if settings.openalex_api_key:
            params["api_key"] = settings.openalex_api_key
        if settings.contact_email:
            params["mailto"] = settings.contact_email

        payload = await fetcher.get_json("https://api.openalex.org/works", params)
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise ValueError("OpenAlex response is missing a results list")
        records = payload["results"]
        documents: list[Document] = []
        for record in records:
            try:
                if not isinstance(record, dict):
                    continue
                title = record.get("title")
                if not isinstance(title, str) or not title.strip():
                    continue
                abstract = _abstract(record.get("abstract_inverted_index"))
                location = record.get("primary_location") or {}
                source = location.get("source") if isinstance(location, dict) else {}
                source = source if isinstance(source, dict) else {}
                url = location.get("landing_page_url") if isinstance(location, dict) else None
                url = url or record.get("doi") or record.get("id")
                if not isinstance(url, str) or not url:
                    continue
                language = record.get("language")
                language = language if isinstance(language, str) and language else "en"
                documents.append(Document(
                    url=url,
                    title=title,
                    text=abstract or title,
                    source_name=source.get("display_name") or "OpenAlex",
                    source_type="paper",
                    source_lang=language,
                    source_tier=TIER_AUTHORITATIVE,
                    published_at=(published := _date(record.get("publication_date"))),
                    date_source="api" if published is not None else None,
                    adapter=self.name,
                ))
            except (AttributeError, KeyError, TypeError, ValueError):
                continue
        return documents



def _date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return valid_publication_date(date.fromisoformat(value[:10]))
    except ValueError:
        return None


def _abstract(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    words: dict[int, str] = {}
    try:
        for word, positions in value.items():
            if not isinstance(word, str):
                continue
            for position in positions:
                words[int(position)] = word
    except (TypeError, ValueError):
        return ""
    return " ".join(words[position] for position in sorted(words))
