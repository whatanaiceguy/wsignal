from __future__ import annotations

import re
from datetime import date
from html.parser import HTMLParser
from urllib.parse import urlsplit

from wsignal.parsing.dates import valid_publication_date

_DROP = frozenset(
    {"script", "style", "noscript", "head", "template", "svg", "canvas", "iframe"}
)

_BREAK = frozenset(
    {
        "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt",
        "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5",
        "h6", "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section",
        "table", "td", "th", "tr", "ul",
    }
)

_HTML_HINT = re.compile(r"<!doctype\s+html|<html[\s>]|<body[\s>]|<div[\s>]", re.I)
_BLANK_RUN = re.compile(r"\n{3,}")
_SPACES = re.compile(r"[ \t ]+")


def looks_like_html(text: str) -> bool:
    return bool(_HTML_HINT.search(text[:4096]))

_CHALLENGE = (
    "just a moment",
    "attention required",
    "checking your browser",
    "checking your connection",
    "enable javascript and cookies",
    "verify you are human",
    "are you a robot",
    "unusual traffic from your computer",
    "ddos protection by",
    "request unsuccessful. incapsula",
    "__cf_chl",
    "cf-browser-verification",
)

_MIN_TEXT = 200
_MIN_MARKUP = 2000


def looks_blocked(raw: str, extracted: str) -> str | None:
    haystack = raw[:8192].lower()
    for phrase in _CHALLENGE:
        if phrase in haystack:
            return f"challenge page, matched {phrase!r}"
    if (
        looks_like_html(raw)
        and len(raw) >= _MIN_MARKUP
        and len(extracted.strip()) < _MIN_TEXT
    ):
        return (
            f"{len(raw)} bytes of markup rendered to "
            f"{len(extracted.strip())} characters of text; "
            "client-rendered or blocked without saying so"
        )
    return None


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._dropping = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in _DROP:
            self._dropping += 1
        elif tag in _BREAK:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROP:
            self._dropping = max(0, self._dropping - 1)
        elif tag in _BREAK:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._dropping:
            self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def html_to_text(raw: str) -> str:
    if not looks_like_html(raw):
        return raw
    parser = _Extractor()
    try:
        parser.feed(raw)
        parser.close()
    except Exception:
        pass
    lines = [_SPACES.sub(" ", line).strip() for line in parser.text().splitlines()]
    return _BLANK_RUN.sub("\n\n", "\n".join(lines)).strip()

_META_TAG = re.compile(r"<meta\b[^>]*>", re.I)
_META_KEYS = ("name", "property", "itemprop")
_META_VALUE_ATTRIBUTES = frozenset(("property", "name", "itemprop", "content"))
_META_KEY_VALUE = re.compile(
    r"([\w:-]+)\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s\"'=<>`]+))",
    re.I,
)
_HTML_LANG = re.compile(r"<html[^>]*\slang\s*=\s*[\"']([A-Za-z]{2})", re.I)
_TITLE = re.compile(r"<title[^>]*>([^<]{0,20000})</title>", re.I)
_JSONLD_DATE = re.compile(r'"datePublished"\s*:\s*"([^"]{4,40})"', re.I)
_DATE_MARKER = re.compile(
    r"(?:publish(?:ed)?|entry|post)[-_ ]?(?:date|time)|"
    r"(?:date|time)[-_ ]?(?:publish(?:ed)?|entry|post)|pubdate",
    re.I,
)
_URL_DATE = re.compile(
    r"(?:^|/)(?:(\d{4})/(\d{1,2})/(\d{1,2})|"
    r"(\d{4})-(\d{2})-(\d{2})|"
    r"(\d{4})(\d{2})(\d{2}))(?:/|$)"
)

_TITLE_KEYS = ("og:title", "twitter:title", "dc.title")
_SITE_KEYS = ("og:site_name", "application-name", "dc.publisher", "publisher")
_LANG_KEYS = ("og:locale", "dc.language", "language", "content-language")
_DATE_KEYS = (
    "article:published_time",
    "datepublished",
    "date",
    "dc.date",
    "dc.date.issued",
    "pubdate",
    "publish-date",
    "og:published_time",
    "parsely-pub-date",
    "sailthru.date",
)


def _metas(raw: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for tag in _META_TAG.finditer(raw[:200_000]):
        attributes: dict[str, str] = {}
        for match in _META_KEY_VALUE.finditer(tag.group()):
            name, double_quoted, single_quoted, unquoted = match.groups()
            if name.lower() in _META_VALUE_ATTRIBUTES:
                attributes.setdefault(
                    name.lower(),
                    next(
                        value
                        for value in (double_quoted, single_quoted, unquoted)
                        if value is not None
                    ),
                )
        key = next((attributes[name] for name in _META_KEYS if name in attributes), None)
        value = attributes.get("content")
        if key is not None and value is not None:
            found.setdefault(key.strip().lower(), value.strip())
    return found


def _first(metas: dict[str, str], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = metas.get(key)
        if value:
            return value
    return ""


def _as_date(value: str, today: date | None = None) -> date | None:
    text_value = (value or "").strip()
    if len(text_value) < 10:
        return None
    head = text_value[:10]
    parsed = None
    for separator in ("-", "/", "."):
        parts = head.split(separator)
        if len(parts) == 3 and len(parts[0]) == 4:
            try:
                year, month, day = (int(part) for part in parts)
                if 1990 <= year <= 2100:
                    parsed = date(year, month, day)
            except ValueError:
                return None
            break
    if parsed is not None and parsed <= (today or date.today()):
        return valid_publication_date(parsed)
    return None


def _url_date(url: str, today: date | None = None) -> date | None:
    path = urlsplit(url).path
    for match in _URL_DATE.finditer(path):
        groups = match.groups()
        numbers = next(
            groups[index : index + 3]
            for index in (0, 3, 6)
            if groups[index] is not None
        )
        try:
            candidate = date(*(int(part) for part in numbers))
        except ValueError:
            continue
        if 1990 <= candidate.year <= 2100 and candidate <= (today or date.today()):
            return valid_publication_date(candidate)
    return None


class _TimeDates(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.article_depth = 0
        self.values: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.lower(): value or "" for key, value in attrs}
        if tag.lower() == "article":
            self.article_depth += 1
        if tag.lower() != "time":
            return
        datetime_value = attributes.get("datetime", "")
        if not datetime_value:
            return
        itemprops = attributes.get("itemprop", "").lower().split()
        marked = self.article_depth > 0 or "datepublished" in itemprops
        if not marked:
            marked = any(
                _DATE_MARKER.search(f"{key} {value}")
                for key, value in attrs
                if key.lower() in {"class", "id", "itemprop", "property", "name", "data-testid"}
            )
        if marked:
            self.values.append(datetime_value)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "article":
            self.article_depth = max(0, self.article_depth - 1)


def page_metadata(raw: str, url: str = "") -> dict:
    if not looks_like_html(raw):
        return {}
    metas = _metas(raw)

    title = _first(metas, _TITLE_KEYS)
    if not title:
        match = _TITLE.search(raw[:200_000])
        if match:
            title = _SPACES.sub(" ", html_to_text(match.group(1))).strip()

    lang = _first(metas, _LANG_KEYS)[:5].replace("_", "-").split("-")[0].lower()
    if not lang:
        match = _HTML_LANG.search(raw[:200_000])
        lang = match.group(1).lower() if match else ""

    now = date.today()
    meta_date = next(
        (
            candidate
            for key in _DATE_KEYS
            if (
                candidate := _as_date(metas.get(key, ""), now)
            ) is not None
        ),
        None,
    )
    jsonld_match = _JSONLD_DATE.search(raw[:200_000])
    jsonld_date = (
        _as_date(jsonld_match.group(1), now)
        if jsonld_match
        else None
    )
    structured_date = meta_date or jsonld_date
    structured_source = "meta" if meta_date is not None else "jsonld"
    url_date = _url_date(url, now) if url else None
    published = structured_date
    date_source = structured_source if published is not None else None
    if url_date is not None and (
        published is None or abs((published - url_date).days) > 3
    ):
        published = url_date
        date_source = "url"
    if published is None:
        time_dates = _TimeDates()
        try:
            time_dates.feed(raw[:200_000])
            time_dates.close()
        except Exception:
            pass
        published = next(
            (
                candidate
                for value in time_dates.values
                if (candidate := _as_date(value, now)) is not None
            ),
            None,
        )
        if published is not None:
            date_source = "time"

    return {
        "title": title[:300],
        "site_name": _first(metas, _SITE_KEYS)[:120],
        "lang": lang if len(lang) == 2 else "",
        "published_at": published,
        "date_source": date_source,
    }
