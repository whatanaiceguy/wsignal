import logging
import re
from datetime import UTC, date, datetime
from functools import lru_cache
from itertools import product
from typing import Any

import httpx

from wsignal import __version__
from wsignal.config import get_settings
from wsignal.routes import corpus_endpoint

log = logging.getLogger(__name__)

GENERIC_ABBREVIATIONS = {
    "AI", "ML", "IT", "API", "KV", "LLM", "ИИ", "ПО", "SDK", "SLA", "URL", "HTTP", "HTTPS",
}

def month_window(months: int, today: date | None = None) -> list[date]:
    current = today or datetime.now(UTC).date()
    last = current.year * 12 + current.month - 1
    return [
        date(index // 12, index % 12 + 1, 1)
        for index in range(last - months + 1, last + 2)
    ]


def normalize_term(term: str) -> str:
    return " ".join(term.split()).casefold()


def entry_term(name_en: str | None, name_ru: str) -> str:
    name = (name_en or "").strip() or name_ru.strip()
    name = re.sub(r"\bin\s+(?:the\s+)?metaverse\b", "metaverse", name, flags=re.I)
    head = re.split(r"\s+(?:for|in|для)\s+|:|\s+[—–]\s+", name, maxsplit=1, flags=re.I)[0]
    explicit = re.findall(r"\(\s*([A-ZА-ЯЁ][A-ZА-ЯЁ0-9]{1,11})\s*\)", head)
    abbreviations = [
        token for token in re.findall(r"(?<![\w-])[A-ZА-ЯЁ]{3,12}(?![\w-])", head)
        if token not in GENERIC_ABBREVIATIONS
    ]
    abbreviations = list(dict.fromkeys(
        token for token in explicit + abbreviations if token not in GENERIC_ABBREVIATIONS
    ))
    base = head
    while re.search(r"\([^()]*\)", base):
        base = re.sub(r"\([^()]*\)", " ", base)
    alternatives = []
    for clause in re.split(r"\s+(?:and|и)\s+", base, flags=re.I):
        clause = re.sub(r"\b(?:servers?|scanners?|protection)\b", " ", clause, flags=re.I)
        words = clause.split()
        variants = product(*(word.split("/") for word in words))
        for variant in variants:
            term = " ".join(variant).strip(" ,;")
            if term and normalize_term(term) not in {normalize_term(t) for t in alternatives}:
                alternatives.append(term)
            if len(alternatives) >= 4:
                break
    if len(alternatives) == 1 and abbreviations and re.search(r"[а-яё]", alternatives[0], re.I):
        alternatives = abbreviations
    elif len(alternatives) == 1:
        alternatives.extend(token for token in abbreviations if token not in alternatives)
    return " OR ".join(alternatives[:4]) or head.strip()


def validate_corpus_query(value: object) -> str:
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 200:
        raise ValueError("corpus_query must be a nonblank English query of at most 200 characters")
    query = " ".join(value.split())
    if query.count('"') % 2:
        raise ValueError("corpus_query has an unclosed phrase")
    alternatives = [[]]
    for token in re.findall(r'"[^"]*"|\S+', query):
        if token.upper() == "OR":
            alternatives.append([])
        else:
            alternatives[-1].append(token)
    if not 1 <= len(alternatives) <= 4:
        raise ValueError("corpus_query must contain 1-4 OR alternatives")
    generic = GENERIC_ABBREVIATIONS | {"FOR", "IN", "THE", "AND", "TO", "OF", "A"}
    for alternative in alternatives:
        positive = " ".join(token for token in alternative if not token.startswith("-"))
        words = set(re.findall(r"[A-Za-z]+", positive.upper()))
        if not words - generic or re.search(r"[а-яё]", positive, flags=re.I):
            raise ValueError("each corpus_query alternative must name a technology in English")
    return query


class CorpusUnavailable(Exception):
    pass


def _day(value: Any) -> Any:
    if isinstance(value, str) and len(value) >= 10:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return value
    return value


def _with_dates(row: dict) -> dict:
    return {**row, "published": _day(row.get("published"))}


class Corpus:
    """The research corpus, read over HTTP from a corpus server.

    `url` is the corpus API base; empty means this deployment has no corpus.
    """

    def __init__(self, url: str):
        self.url = url.rstrip("/")
        self._client: httpx.AsyncClient | None = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={"User-Agent": f"wsignal/{__version__}", "Accept": "application/json"},
                follow_redirects=True,
            )
        return self._client

    async def _call(
        self, method: str, path: str, agent_timeout: bool, payload: dict | None = None,
    ) -> Any:
        if not self.url:
            raise CorpusUnavailable("Корпус не настроен")
        settings = get_settings()
        timeout = (
            settings.agent_corpus_command_timeout_s
            if agent_timeout else settings.corpus_command_timeout_s
        )
        try:
            response = await self._http().request(
                method, f"{self.url}{path}", json=payload, timeout=timeout,
            )
        except httpx.TimeoutException as exc:
            raise CorpusUnavailable("Запрос к корпусу превысил лимит времени") from exc
        except httpx.HTTPError as exc:
            raise CorpusUnavailable("Корпус временно недоступен") from exc
        if response.status_code == 404:
            return None
        if response.status_code == 429:
            raise CorpusUnavailable("Лимит запросов к корпусу исчерпан, повторите позже")
        if response.status_code >= 400:
            detail = None
            try:
                detail = response.json().get("detail")
            except ValueError:
                pass
            raise CorpusUnavailable(
                detail if isinstance(detail, str) and detail else "Корпус временно недоступен"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise CorpusUnavailable("Корпус временно недоступен") from exc

    async def search(
        self, query: str, since: date | None, until: date | None, lang: str | None,
        limit: int, oldest_first: bool = False, agent_timeout: bool = False,
    ) -> list[dict]:
        payload = {
            "query": query,
            "since": since.isoformat() if since else None,
            "until": until.isoformat() if until else None,
            "lang": lang,
            "limit": limit,
            "oldest_first": oldest_first,
        }
        body = await self._call("POST", "/search", agent_timeout, payload)
        return [_with_dates(row) for row in (body or {}).get("results", [])]

    async def article(self, article_id: int, agent_timeout: bool = False) -> dict | None:
        body = await self._call("GET", f"/article/{int(article_id)}", agent_timeout)
        article = (body or {}).get("article")
        return None if article is None else _with_dates(article)

    async def series(
        self, terms: list[str], months: int = 36, agent_timeout: bool = False,
    ) -> list[dict]:
        body = await self._call(
            "POST", "/series", agent_timeout, {"terms": list(terms), "months": months},
        )
        series = (body or {}).get("series")
        if not isinstance(series, list) or len(series) != len(terms):
            raise CorpusUnavailable("Корпус вернул неполный ответ")
        return series


@lru_cache(maxsize=1)
def _corpus(url: str) -> Corpus:
    return Corpus(url)


def get_corpus() -> Corpus:
    return _corpus(corpus_endpoint().base_url)
