from __future__ import annotations

from datetime import date, datetime
from typing import Any
from urllib.parse import urlsplit

from wsignal.config import get_settings
from wsignal.parsing.base import Document, HarvestResult, Surface
from wsignal.parsing.dates import subtract_months, valid_publication_date
from wsignal.parsing.http import Fetcher
from wsignal.parsing.tiering import host_tier
from wsignal.routes import brave_endpoint

ENDPOINT = "https://api.search.brave.com/res/v1/web/search"

MAX_COUNT = 20


class BraveUnauthorised(RuntimeError):
    pass


def freshness(window_months: int, today: date | None = None) -> str | None:
    if window_months <= 0:
        return None
    now = today or date.today()
    return f"{subtract_months(now, window_months).isoformat()}to{now.isoformat()}"


def _absolute_date(value: Any) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        result = datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            result = date.fromisoformat(raw[:10])
        except ValueError:
            return None
    return valid_publication_date(result)


def parse_results(payload: Any) -> list[tuple[str, str, str, date | None]]:
    if not isinstance(payload, dict):
        return []
    results = ((payload.get("web") or {}).get("results")) or []
    if not isinstance(results, list):
        return []

    out: list[tuple[str, str, str, date | None]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        title = str(item.get("title") or "").strip()
        if not url.startswith(("http://", "https://")) or not title:
            continue
        parts = [str(item.get("description") or "").strip()]
        extra = item.get("extra_snippets")
        if isinstance(extra, list):
            parts += [str(snippet).strip() for snippet in extra if snippet]
        text = " ".join(part for part in parts if part)
        published = _absolute_date(item.get("page_age"))
        if published is None:
            published = _absolute_date(item.get("age"))
        out.append((url, title, text, published))
    return out


class BraveSource:
    name = "brave"
    host = "api.search.brave.com"

    async def harvest(self, surface: Surface, fetcher: Fetcher) -> HarvestResult:
        query = surface.query.strip()
        if not query:
            return HarvestResult(surface=surface, documents=[])

        endpoint = brave_endpoint(ENDPOINT, get_settings())
        if endpoint.route == "off":
            raise BraveUnauthorised(
                "no BRAVE_API_KEY configured and no relay. Not an empty result."
            )
        headers = {"Accept": "application/json"}
        if endpoint.key:
            headers["X-Subscription-Token"] = endpoint.key

        params: dict[str, Any] = {
            "q": query,
            "count": max(1, min(surface.limit, MAX_COUNT)),
            "extra_snippets": "true",
        }
        window = freshness(surface.window_months)
        if window:
            params["freshness"] = window
        payload = await fetcher.request_json(
            "GET", endpoint.base_url, params=params, headers=headers,
        )

        documents: list[Document] = []
        seen: set[str] = set()
        for url, title, text, published in parse_results(payload):
            if url in seen:
                continue
            seen.add(url)
            host = urlsplit(url).netloc.lower().removeprefix("www.")
            documents.append(
                Document(
                    url=url,
                    title=title,
                    text=text or title,
                    source_name=host or "unknown",
                    source_type="page",
                    source_lang="und",
                    source_tier=host_tier(host),
                    published_at=published,
                    date_source="search" if published is not None else None,
                    adapter=self.name,
                    retrieved=False,
                    links_to=None,
                )
            )
        
        return HarvestResult(
            surface=surface, documents=documents[: max(0, surface.limit)]
        )
