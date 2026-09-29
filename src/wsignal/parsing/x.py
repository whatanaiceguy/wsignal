from __future__ import annotations

from datetime import date, datetime
from typing import Any

from wsignal.config import get_settings
from wsignal.parsing.base import TIER_SOCIAL, Document, Surface
from wsignal.parsing.dates import valid_publication_date

SEARCH_URL = "https://api.x.com/2/tweets/search/recent"

MIN_RESULTS = 10
MAX_RESULTS = 100


class MissingCredentials(RuntimeError):
    pass


class XSource:
    name = "x"
    host = "api.x.com"

    async def harvest(self, surface: Surface, fetcher: Any) -> list[Document]:
        settings = get_settings()
        if not settings.x_bearer_token:
            raise MissingCredentials("X_BEARER_TOKEN is not set")

        payload = await fetcher.request_json(
            "GET",
            SEARCH_URL,
            params={
                "query": f"{surface.query} -is:retweet",
                "max_results": max(MIN_RESULTS, min(MAX_RESULTS, surface.limit)),
                "tweet.fields": "created_at,public_metrics,lang,author_id",
                "expansions": "author_id",
                "user.fields": "username,name,verified",
            },
            headers={"Authorization": f"Bearer {settings.x_bearer_token}"},
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ValueError("X response is missing a data list")
        return [
            document
            for post in (payload.get("data") or [])
            if isinstance(post, dict)
            and (document := _one(post, _authors(payload), self.name)) is not None
        ][: max(0, surface.limit)]


def _authors(payload: dict) -> dict[str, dict]:
    includes = payload.get("includes")
    users = includes.get("users") if isinstance(includes, dict) else None
    return {
        str(user.get("id")): user
        for user in (users or [])
        if isinstance(user, dict) and user.get("id")
    }


def _date(value: Any) -> date | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return valid_publication_date(
            datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
        )
    except ValueError:
        return None


def _one(post: dict, authors: dict[str, dict], adapter: str) -> Document | None:
    identifier = str(post.get("id") or "").strip()
    text = post.get("text")
    if not identifier or not isinstance(text, str) or not text.strip():
        return None

    author = authors.get(str(post.get("author_id") or "")) or {}
    username = str(author.get("username") or "").strip()
    url = f"https://x.com/{username}/status/{identifier}" if username else (
        f"https://x.com/i/status/{identifier}"
    )

    metrics = post.get("public_metrics")
    metrics = metrics if isinstance(metrics, dict) else {}
    reach = ", ".join(
        f"{name} {metrics[name]}"
        for name in ("like_count", "retweet_count", "reply_count", "impression_count")
        if isinstance(metrics.get(name), int)
    )

    lang = str(post.get("lang") or "").strip().lower()[:2] or "und"

    return Document(
        url=url,
        title=f"@{username}" if username else f"post {identifier}",
        text=f"{text.strip()}\n\n[{reach}]" if reach else text.strip(),
        source_name=f"X / @{username}" if username else "X",
        source_type="social",
        source_lang=lang,
        source_tier=TIER_SOCIAL,
        published_at=(published := _date(post.get("created_at"))),
        date_source="api" if published is not None else None,
        adapter=adapter,
        retrieved=True,
    )
