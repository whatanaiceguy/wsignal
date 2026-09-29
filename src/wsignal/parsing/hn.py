from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from wsignal.parsing.base import TIER_SOCIAL, Document, Surface
from wsignal.parsing.dates import subtract_months, valid_publication_date
from wsignal.parsing.http import Fetcher


class HackerNewsSource:

    name = "hn"
    host = "hn.algolia.com"

    async def harvest(self, surface: Surface, fetcher: Fetcher) -> list[Document]:
        today = date.today()
        start = subtract_months(today, surface.window_months)
        params = {
            "query": surface.query,
            "optionalWords": surface.query,
            "tags": "story",
            "hitsPerPage": max(0, surface.limit),
            "numericFilters": f"created_at_i>={_epoch(start)}",
        }
        payload = await fetcher.get_json("https://hn.algolia.com/api/v1/search", params)
        if not isinstance(payload, dict) or not isinstance(payload.get("hits"), list):
            raise ValueError("Hacker News response is missing a hits list")
        hits = payload["hits"]
        documents: list[Document] = []
        for hit in hits:
            try:
                if not isinstance(hit, dict):
                    continue
                title = hit.get("title") or hit.get("story_title")
                if not isinstance(title, str) or not title.strip():
                    continue
                text = hit.get("story_text") or hit.get("comment_text") or title
                if not isinstance(text, str):
                    text = title
                object_id = hit.get("objectID")
                if not object_id:
                    continue
                url = f"https://news.ycombinator.com/item?id={object_id}"
                linked = hit.get("url")
                if not isinstance(linked, str) or linked == url:
                    linked = None
                published = _date(hit.get("created_at"))
                if published is not None and not (start <= published <= today):
                    continue
                documents.append(Document(
                    url=url,
                    title=title,
                    text=text,
                    source_name="Hacker News",
                    source_type="social",
                    source_lang="en",
                    source_tier=TIER_SOCIAL,
                    published_at=published,
                    date_source="api" if published is not None else None,
                    adapter=self.name,
                    retrieved=True,
                    links_to=linked,
                ))
            except (AttributeError, KeyError, TypeError, ValueError):
                continue
        return documents[: max(0, surface.limit)]


def _date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return valid_publication_date(
            datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        )
    except ValueError:
        return None



def _epoch(day: date) -> int:
    return int(datetime.combine(day, datetime.min.time(), tzinfo=UTC).timestamp())
