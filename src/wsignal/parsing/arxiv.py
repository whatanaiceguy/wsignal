from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import date

from wsignal.parsing.base import TIER_AUTHORITATIVE, Document, Surface
from wsignal.parsing.dates import subtract_months, valid_publication_date
from wsignal.parsing.http import Fetcher


class ArxivSource:
    name = "arxiv"
    host = "export.arxiv.org"

    async def harvest(self, surface: Surface, fetcher: Fetcher) -> list[Document]:
        today = date.today()
        start = subtract_months(today, surface.window_months)
        params = {
            "search_query": (
                f"({build_terms(surface.query)}) "
                f"AND submittedDate:[{start:%Y%m%d}0000 TO {today:%Y%m%d}2359]"
            ),
            "start": 0,
            "max_results": max(0, surface.limit),
        }
        xml = await fetcher.get_text("https://export.arxiv.org/api/query", params)
        root = ET.fromstring(xml)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        documents: list[Document] = []
        for entry in root.findall("atom:entry", ns):
            try:
                title = _text(entry.find("atom:title", ns))
                summary = _text(entry.find("atom:summary", ns))
                published = _date(_text(entry.find("atom:published", ns)))
                if not title or published is None or published < start or published > today:
                    continue
                links = entry.findall("atom:link", ns)
                url = next(
                    (
                        link.attrib.get("href")
                        for link in links
                        if link.attrib.get("rel") == "alternate"
                    ),
                    None,
                )
                url = url or _text(entry.find("atom:id", ns))
                if not url:
                    continue
                lang = entry.find("{http://www.w3.org/2005/Atom}language")
                documents.append(Document(
                    url=url,
                    title=title,
                    text=summary or title,
                    source_name="arXiv",
                    source_type="paper",
                    source_lang=_text(lang) or "en",
                    source_tier=TIER_AUTHORITATIVE,
                    published_at=published,
                    date_source="api",
                    adapter=self.name,
                ))
            except (AttributeError, KeyError, TypeError, ValueError):
                continue
        return documents[: max(0, surface.limit)]


def build_terms(query: str) -> str:
    terms = re.findall(r"\w[\w-]*", query, re.UNICODE)
    if not terms:
        return f'all:"{query}"'
    return " OR ".join(f"all:{term}" for term in terms)


def _text(element: ET.Element | None) -> str:
    return element.text or "" if element is not None else ""


def _date(value: str) -> date | None:
    try:
        return valid_publication_date(date.fromisoformat(value[:10]))
    except (TypeError, ValueError):
        return None

