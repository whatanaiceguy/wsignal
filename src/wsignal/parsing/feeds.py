from __future__ import annotations

import html as html_module
import re
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit
from xml.etree import ElementTree

from wsignal.parsing.base import Document
from wsignal.parsing.dates import valid_publication_date
from wsignal.parsing.tiering import host_tier

MAX_FEED_BYTES = 5_000_000

_TAGS = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")
_XML_OPENER = re.compile(r"\s*(<\?xml|<rss|<feed|<rdf:RDF)", re.I)


def looks_like_feed(body: str) -> bool:
    return bool(_XML_OPENER.match(body or ""))


def _text(fragment: str | None) -> str:
    if not fragment:
        return ""
    return _SPACE.sub(" ", html_module.unescape(_TAGS.sub(" ", fragment))).strip()


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child(node, *names: str):
    wanted = {name.lower() for name in names}
    for element in node:
        if _local(element.tag) in wanted:
            return element
    return None


def _link(node) -> str | None:
    fallback = None
    for element in node:
        if _local(element.tag) != "link":
            continue
        href = element.attrib.get("href")
        if href:
            rel = (element.attrib.get("rel") or "alternate").lower()
            if rel == "alternate":
                return href.strip()
            fallback = fallback or href.strip()
        elif (element.text or "").strip():
            return element.text.strip()
    if fallback:
        return fallback
    about = node.attrib.get("{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about")
    return about.strip() if about else None


def _date(node) -> date | None:
    element = _child(node, "pubdate", "published", "updated", "date")
    raw = (element.text or "").strip() if element is not None else ""
    if not raw:
        return None
    try:
        return valid_publication_date(parsedate_to_datetime(raw).date())
    except (TypeError, ValueError):
        pass
    try:
        return valid_publication_date(
            datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
        )
    except ValueError:
        return None


def parse_feed(body: str, host: str) -> list[Document]:
    if not body or len(body.encode("utf-8", "ignore")) > MAX_FEED_BYTES:
        return []
    try:
        root = ElementTree.fromstring(body.strip())
    except ElementTree.ParseError:
        return []

    documents: list[Document] = []
    seen: set[str] = set()
    for node in root.iter():
        if _local(node.tag) not in ("item", "entry"):
            continue
        url = _link(node)
        if not url or not url.startswith(("http://", "https://")):
            continue
        if url in seen:
            continue
        seen.add(url)

        title_element = _child(node, "title")
        title = _text(title_element.text if title_element is not None else "")
        if not title:
            continue

        body_element = _child(node, "description", "summary", "encoded", "content")
        summary = _text(body_element.text if body_element is not None else "")

        documents.append(
            Document(
                url=url,
                title=title,
                text=summary or title,
                source_name=urlsplit(url).netloc.lower().removeprefix("www.") or host,
                source_type="news",
                source_lang="und",
                source_tier=host_tier(url),
                published_at=(published := _date(node)),
                date_source="feed" if published is not None else None,
                adapter="feeds",
                retrieved=False,
                links_to=None,
            )
        )
    return documents
