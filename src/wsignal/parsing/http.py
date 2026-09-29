from __future__ import annotations

import asyncio
import contextlib
import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

import httpx

from wsignal.config import get_settings
from wsignal.retry import RetryNote, with_retries

HOST_RATES: dict[str, float] = {
    "export.arxiv.org": 1 / 3,
    "api.search.brave.com": 1.0,
    "api.gdeltproject.org": 1 / 5,
    "api.github.com": 0.5,
    "api.openalex.org": 8.0,
    "hn.algolia.com": 5.0,
    "api.crossref.org": 10.0,
    "eutils.ncbi.nlm.nih.gov": 3.0,
    "www.ebi.ac.uk": 5.0,
    "lite.duckduckgo.com": 1 / 6,
    "ops.epo.org": 0.25,
    "searchplatform.rospatent.gov.ru": 1.0,
    "api.x.com": 0.5,
}
DEFAULT_RATE = 2.0
MAX_RESPONSE_BYTES = 5_000_000

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

IDENTIFY_TO: frozenset[str] = frozenset(
    {
        "api.openalex.org",
        "export.arxiv.org",
        "hn.algolia.com",
        "api.crossref.org",
        "eutils.ncbi.nlm.nih.gov",
        "www.ebi.ac.uk",
    }
)


class TokenBucket:
    def __init__(self, per_second: float, max_interval: float = 30.0) -> None:
        self.interval = 1.0 / per_second
        self._max_interval = max_interval
        self._lock = asyncio.Lock()
        self._next_at = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self._next_at - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._next_at = max(now, self._next_at) + self.interval

    def queue_estimate(self) -> float:
        return max(0.0, self._next_at - time.monotonic())

    def widen(self, factor: float = 2.0) -> float:
        self.interval = min(self._max_interval, self.interval * factor)
        return self.interval

_TRAFFIC: ContextVar[dict | None] = ContextVar("fetch_traffic", default=None)


@dataclass
class FetchStats:
    requests: int = 0


class Fetcher:

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._settings = get_settings()
        settings = self._settings
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=settings.fetch_connect_timeout_s,
                read=settings.fetch_read_timeout_s,
                write=settings.fetch_connect_timeout_s,
                pool=settings.fetch_connect_timeout_s,
            ),
            follow_redirects=True,
            headers={"User-Agent": settings.user_agent},
        )
        self._buckets: dict[str, TokenBucket] = {}
        self.stats = FetchStats()
        self.last_response_truncated = False

    def user_agent_for(self, host: str) -> str:
        return self._settings.user_agent if host in IDENTIFY_TO else BROWSER_UA

    def queue_estimate(self, host: str) -> float:
        return self._bucket(host).queue_estimate()

    @contextlib.asynccontextmanager
    async def measure(self):
        counter = {"requests": 0, "bytes": 0}
        token = _TRAFFIC.set(counter)
        try:
            yield counter
        finally:
            _TRAFFIC.reset(token)

    def _bucket(self, host: str) -> TokenBucket:
        if host not in self._buckets:
            self._buckets[host] = TokenBucket(HOST_RATES.get(host, DEFAULT_RATE))
        return self._buckets[host]

    async def _request(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        json: Any | None = None,
        data: Any | None = None,
    ) -> httpx.Response:
        host = httpx.URL(url).host
        bucket = self._bucket(host)

        async def queue() -> None:
            await bucket.acquire()

        async def attempt(remaining_s: float) -> httpx.Response:
            self.stats.requests += 1
            self.last_response_truncated = False
            timeout = httpx.Timeout(
                connect=min(self._settings.fetch_connect_timeout_s, remaining_s),
                read=min(self._settings.fetch_read_timeout_s, remaining_s),
                write=self._settings.fetch_connect_timeout_s,
                pool=min(self._settings.fetch_connect_timeout_s, remaining_s),
            )
            request_headers = {"User-Agent": self.user_agent_for(host), **(headers or {})}
            async with self._client.stream(
                method,
                url,
                params=params,
                json=json,
                data=data,
                headers=request_headers,
                timeout=timeout,
            ) as streamed:
                chunks: list[bytes] = []
                size = 0
                truncated = False
                async for chunk in streamed.aiter_bytes():
                    remaining = MAX_RESPONSE_BYTES - size
                    if len(chunk) > remaining:
                        chunks.append(chunk[:remaining])
                        size += remaining
                        truncated = True
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                response_headers = streamed.headers.copy()
                response_headers.pop("content-encoding", None)
                response_headers.pop("content-length", None)
                response = httpx.Response(
                    status_code=streamed.status_code,
                    headers=response_headers,
                    content=b"".join(chunks),
                    request=streamed.request,
                    extensions={**streamed.extensions, "wsignal_truncated": truncated},
                )
            response.raise_for_status()
            self.last_response_truncated = bool(
                response.extensions.get("wsignal_truncated")
            )
            return response

        def noted(note: RetryNote) -> None:
            if note.status in (429, 503):
                bucket.widen()

        try:
            response, _ = await with_retries(
                attempt,
                deadline_s=self._settings.fetch_deadline_s,
                max_attempts=self._settings.fetch_max_attempts,
                base_delay=self._settings.fetch_retry_base_delay_s,
                label=f"{method} {host}",
                status_also=frozenset({406}) if host == "export.arxiv.org" else frozenset(),
                before_attempt=queue,
                queue_timeout_s=self._settings.fetch_queue_timeout_s,
                on_retry=noted,
            )
        except Exception:
            raise
        counter = _TRAFFIC.get()
        if counter is not None:
            counter["requests"] += 1
            counter["bytes"] += len(response.content)
        return response

    async def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        return (await self._request("GET", url, params)).json()

    async def get_text(self, url: str, params: dict[str, Any] | None = None) -> str:
        return (await self._request("GET", url, params)).text

    async def get_text_with_metadata(
        self, url: str, params: dict[str, Any] | None = None
    ) -> tuple[str, bool]:
        response = await self._request("GET", url, params)
        return response.text, bool(response.extensions.get("wsignal_truncated"))

    async def request_json(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        json: Any | None = None,
        data: Any | None = None,
    ) -> Any:
        response = await self._request(
            method, url, params=params, headers=headers, json=json, data=data
        )
        return response.json()

    async def aclose(self) -> None:
        await self._client.aclose()
