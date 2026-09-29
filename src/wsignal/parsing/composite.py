from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

RRF_K = 60

_JUNK_PARAMS = (
    "utm_",
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref_src",
    "igshid",
    "_hsenc",
    "_hsmi",
)


def normalise_url(url: str) -> str:
    parts = urlsplit(url.strip())
    host = parts.netloc.lower().removeprefix("www.")
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith(_JUNK_PARAMS)
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(
        (parts.scheme.lower() or "https", host, path, urlencode(query), "")
    )


def fuse(per_source: dict[str, list[dict]], limit: int) -> list[dict]:
    merged: dict[str, dict] = {}
    for source in sorted(per_source):
        for position, record in enumerate(per_source[source]):
            url = record.get("url")
            if not url:
                continue
            key = normalise_url(url)
            entry = merged.get(key)
            if entry is None:
                entry = dict(record)
                entry["found_by"] = []
                entry["rank_in"] = {}
                entry["_score"] = 0.0
                merged[key] = entry
            if source not in entry["found_by"]:
                entry["found_by"].append(source)
            entry["rank_in"][source] = position + 1
            entry["_score"] += 1.0 / (RRF_K + position + 1)
            if not entry.get("published_at") and record.get("published_at"):
                entry["published_at"] = record["published_at"]
                entry["date_type"] = record.get("date_type", "unknown")
                entry["date_source"] = record.get("date_source", "search")

    ordered = sorted(merged.values(), key=lambda item: -item["_score"])
    out = []
    for entry in ordered[:limit]:
        entry["agreement"] = len(entry["found_by"])
        entry["score"] = round(entry.pop("_score"), 5)
        out.append(entry)
    return out
