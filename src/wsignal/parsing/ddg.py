from __future__ import annotations

import html as html_module
import re
from urllib.parse import parse_qs, urlsplit

from wsignal.parsing.base import Document, Surface
from wsignal.parsing.http import Fetcher
from wsignal.parsing.tiering import host_tier

_ANCHOR = re.compile(r"<a\b([^>]*result-link[^>]*)>(.{0,10000}?)</a>", re.S | re.I)
_HREF = re.compile(r"""href=(['"])(.*?)\1(?=\s|>)""", re.I)
_SNIPPET = re.compile(
    r"""<td[^>]*class=["']result-snippet["'][^>]*>(.{0,20000}?)</td>""", re.S | re.I
)
_TAGS = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")


class DuckDuckGoChallenged(RuntimeError):
    pass


def looks_challenged(body: str) -> bool:
    lowered = body.lower()
    if "bots use duckduckgo" in lowered or "confirm this search was made by a human" in lowered:
        return True
    return 'name="q"' not in body and "result-link" not in body


def _clean(fragment: str) -> str:
    return _SPACE.sub(" ", html_module.unescape(_TAGS.sub(" ", fragment))).strip()


def _target(href: str) -> str | None:
    href = html_module.unescape(href)
    if href.startswith("//"):
        href = "https:" + href
    parts = urlsplit(href)
    if "duckduckgo.com" in parts.netloc and parts.path.startswith("/l/"):
        wrapped = parse_qs(parts.query).get("uddg")
        if not wrapped:
            return None
        href = wrapped[0]
    if not href.startswith(("http://", "https://")):
        return None
    return href


def parse_results(body: str) -> list[tuple[str, str, str]]:
    anchors = list(_ANCHOR.finditer(body))
    snippets = list(_SNIPPET.finditer(body))
    out: list[tuple[str, str, str]] = []
    for index, anchor in enumerate(anchors):
        href_match = _HREF.search(anchor.group(1))
        if href_match is None:
            continue
        url = _target(href_match.group(2))
        title = _clean(anchor.group(2))
        if not url or not title:
            continue
        stop = anchors[index + 1].start() if index + 1 < len(anchors) else len(body)
        snippet = ""
        for candidate in snippets:
            if anchor.end() <= candidate.start() < stop:
                snippet = _clean(candidate.group(1))
                break
        out.append((url, title, snippet))
    return out


def _window(months: int) -> str | None:
    if months <= 0:
        return None
    if months <= 1:
        return "m"
    if months <= 12:
        return "y"
    return None


class DuckDuckGoLiteSource:
    name = "ddg"
    host = "lite.duckduckgo.com"

    async def harvest(self, surface: Surface, fetcher: Fetcher) -> list[Document]:
        query = surface.query.strip()
        if not query:
            return []
        params: dict[str, str] = {"q": query}
        window = _window(surface.window_months)
        if window:
            params["df"] = window

        body = await fetcher.get_text("https://lite.duckduckgo.com/lite/", params)
        if looks_challenged(body):
            raise DuckDuckGoChallenged(
                f"duckduckgo served its bot challenge instead of results "
                f"({len(body)} bytes, no search form). Not an empty result."
            )

        documents: list[Document] = []
        seen: set[str] = set()
        for url, title, snippet in parse_results(body):
            if url in seen:
                continue
            seen.add(url)
            host = urlsplit(url).netloc.lower().removeprefix("www.")
            documents.append(
                Document(
                    url=url,
                    title=title,
                    text=snippet or title,
                    source_name=host or "unknown",
                    source_type="page",
                    source_lang="und",
                    source_tier=host_tier(host),
                    published_at=None,
                    adapter=self.name,
                    retrieved=False,
                    links_to=None,
                )
            )
        return documents[: max(0, surface.limit)]
