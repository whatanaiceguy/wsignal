"""Poll the host register and store what the feeds carry.

Not an agent and not part of a run. This is ingest: it fills `documents` with
what the curated tail published recently, so that a later search has a corpus of
our own to look in rather than only whatever an engine will return today.

**Failures are per host.** One publisher serving a 403, a timeout or malformed
XML must not cost the eighty-five that would have answered. Every host's outcome
is reported, and a feed that parsed to zero items is a result rather than an
error - it is how a feed that changed shape underneath us becomes visible.

**Nothing here judges.** Feeds are unfiltered by construction: geekwire's feed
carries a story about a radio station alongside a funding round. Selecting is
the search layer's job, and dropping items here on a keyword would quietly
narrow the corpus to what we already expected to find, which is the one thing a
weak-signal instrument must not do.
"""

from __future__ import annotations

import argparse
import asyncio
import time
from dataclasses import dataclass

from sqlalchemy import func, select

from wsignal.db import get_sessionmaker
from wsignal.inference.tools import record_source_event, store_document
from wsignal.models import Document
from wsignal.parsing.feeds import looks_like_feed, parse_feed
from wsignal.parsing.hosts import HostEntry, pollable_hosts
from wsignal.parsing.http import Fetcher


@dataclass
class PollResult:
    host: str
    items: int = 0
    error: str | None = None
    ms: int = 0
    traffic: dict | None = None


async def poll_one(
    entry: HostEntry, fetcher: Fetcher, semaphore
) -> tuple[PollResult, list[Document]]:
    """Always the same shape, whatever happened.

    An earlier draft returned a bare result on the error path and a tuple on the
    success path, which typechecks nowhere and would have made the caller guess.
    """
    started = time.monotonic()

    def elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    async with semaphore:
        async with fetcher.measure() as traffic:
            try:
                body = await fetcher.get_text(entry.feed or "")
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"[:160]
                return (
                    PollResult(
                        entry.host, error=error, ms=elapsed(), traffic=traffic
                    ),
                    [],
                )
    if not looks_like_feed(body):
        return (
            PollResult(
                entry.host,
                error=f"not a feed: {len(body)} bytes of non-xml on a 200",
                ms=elapsed(),
                traffic=traffic,
            ),
            [],
        )
    documents = parse_feed(body, entry.host)
    return (
        PollResult(
            entry.host, items=len(documents), ms=elapsed(), traffic=traffic
        ),
        documents,
    )


async def run(hosts: list[HostEntry], concurrency: int, dry_run: bool) -> int:
    fetcher = Fetcher()
    semaphore = asyncio.Semaphore(concurrency)
    before = after = 0
    sessionmaker = get_sessionmaker()

    async with sessionmaker() as session:
        if not dry_run:
            before = await session.scalar(select(func.count(Document.id))) or 0

        results = await asyncio.gather(
            *(poll_one(entry, fetcher, semaphore) for entry in hosts)
        )

        stored = 0
        for result, documents in results:
            await record_source_event(
                adapter="feeds",
                host=result.host,
                ok=result.error is None,
                items=None if result.error else result.items,
                error=result.error,
                duration_ms=result.ms,
                traffic=result.traffic,
            )
            if result.error:
                print(f"  ERR  {result.host[:38]:<38} {result.error[:60]}")
                continue
            mark = "    " if result.items else "ZERO"
            print(f"  {mark} {result.host[:38]:<38} {result.items:>3} items  {result.ms:>5}ms")
            if dry_run:
                continue
            for document in documents:
                await store_document(session, document)
                stored += 1
        if not dry_run:
            await session.commit()
            after = await session.scalar(select(func.count(Document.id))) or 0

    await fetcher.aclose()

    ok = sum(1 for result, _ in results if not result.error)
    errors = len(results) - ok
    items = sum(result.items for result, _ in results)
    print(
        f"\n{len(results)} hosts: {ok} answered, {errors} failed. "
        f"{items} items seen, {stored if not dry_run else 0} offered to the store, "
        f"{after - before} new rows."
    )
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Poll the feed register.")
    parser.add_argument("--host", action="append", help="only this host; repeatable")
    parser.add_argument("--limit", type=int, help="only the first N pollable hosts")
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument(
        "--dry-run", action="store_true", help="fetch and parse, write nothing"
    )
    args = parser.parse_args(argv)

    hosts = pollable_hosts()
    if args.host:
        wanted = {h.lower().removeprefix("www.") for h in args.host}
        hosts = [entry for entry in hosts if entry.host in wanted]
        missing = wanted - {entry.host for entry in hosts}
        if missing:
            print(f"not pollable or not in the register: {', '.join(sorted(missing))}")
    if args.limit:
        hosts = hosts[: args.limit]
    if not hosts:
        print("nothing to poll")
        return 1

    print(f"polling {len(hosts)} feeds at concurrency {args.concurrency}")
    return asyncio.run(run(hosts, args.concurrency, args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
