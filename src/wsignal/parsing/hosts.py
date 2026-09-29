from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

REGISTER = Path(__file__).resolve().parent.parent / "data" / "hosts.json"


@dataclass(frozen=True, slots=True)
class HostEntry:
    host: str
    feed: str | None
    poll: bool
    source: str
    dataset_urls: int
    note: str | None = None
    items_seen: int = 0
    discovered: str | None = None

    @property
    def pollable(self) -> bool:
        return self.poll and bool(self.feed)


def _entry(raw: dict) -> HostEntry:
    host = str(raw.get("host") or "").strip().lower().removeprefix("www.")
    if not host:
        raise ValueError(f"register entry without a host: {raw!r}")
    feed = raw.get("feed") or None
    return HostEntry(
        host=host,
        feed=str(feed) if feed else None,
        poll=bool(raw.get("poll", bool(feed))),
        source=str(raw.get("source") or "manual"),
        dataset_urls=int(raw.get("dataset_urls") or 0),
        note=raw.get("note") or None,
        items_seen=int(raw.get("items_seen") or 0),
        discovered=raw.get("discovered") or None,
    )


def load_register(path: Path | None = None) -> list[HostEntry]:
    target = path or REGISTER
    raw = json.loads(target.read_text(encoding="utf-8"))
    entries = [_entry(item) for item in raw.get("hosts", [])]
    seen: set[str] = set()
    for entry in entries:
        if entry.host in seen:
            raise ValueError(f"host listed twice in the register: {entry.host}")
        seen.add(entry.host)
    return entries


@lru_cache(maxsize=1)
def _cached() -> tuple[HostEntry, ...]:
    return tuple(load_register())


def pollable_hosts() -> list[HostEntry]:
    return [entry for entry in _cached() if entry.pollable]


def known_hosts() -> set[str]:
    return {entry.host for entry in _cached()}
