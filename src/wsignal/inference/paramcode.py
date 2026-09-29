from __future__ import annotations

import re
from urllib.parse import urlparse

ACADEMIC_OR_ANALYST = (
    "arxiv.org",
    "nature.com",
    "science.org",
    "ieee.org",
    "acm.org",
    "doi.org",
    "springer.com",
    "sciencedirect.com",
    "biorxiv.org",
    "ssrn.com",
    "nih.gov",
    "gartner.com",
    "forrester.com",
    "idc.com",
    "mckinsey.com",
    "iso.org",
    "nist.gov",
    "etsi.org",
    "ietf.org",
    "europa.eu",
)

TRADE = (
    "techcrunch.com",
    "theregister.com",
    "eetimes.com",
    "datacenterdynamics.com",
    "venturebeat.com",
    "geekwire.com",
    "theverge.com",
    "wired.com",
    "reuters.com",
    "bloomberg.com",
    "ft.com",
    "wsj.com",
    "cnbc.com",
    "forbes.com",
    "protocol.com",
    "axios.com",
    "semianalysis.com",
    "tomshardware.com",
    "anandtech.com",
    "lightreading.com",
    "siliconangle.com",
    "fortune.com",
    "yahoo.com",
    "economist.com",
    "nytimes.com",
    "theinformation.com",
    "businessinsider.com",
    "zdnet.com",
    "arstechnica.com",
    "engadget.com",
    "crunchbase.com",
    "pitchbook.com",
    "sifted.eu",
    "techcrunch.cn",
)

PR_OR_VENDOR = (
    "prnewswire.com",
    "businesswire.com",
    "globenewswire.com",
    "einpresswire.com",
    "medium.com",
    "substack.com",
    "linkedin.com",
    "reddit.com",
    "news.ycombinator.com",
    "youtube.com",
    "x.com",
    "twitter.com",
    "blog.google",
    "cloud.google.com",
    "aws.amazon.com",
    "azure.microsoft.com",
    "developer.nvidia.com",
    "blogs.nvidia.com",
    "github.com",
    "huggingface.co",
)

_URL = re.compile(r"https?://[^\s\)\]]+")
_SPLIT_ORGS = re.compile(r"[,;/]| и | and ", re.IGNORECASE)
_NOISE = re.compile(r"^(и|and|др|others?|etc\.?|прочие)$", re.IGNORECASE)


def hosts(text: str) -> list[str]:
    found = []
    for raw in _URL.findall(text or ""):
        host = (urlparse(raw).hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        if host:
            found.append(host)
    return found


def tier_of(host: str) -> int:
    for known in ACADEMIC_OR_ANALYST:
        if host == known or host.endswith("." + known):
            return 1
    for known in TRADE:
        if host == known or host.endswith("." + known):
            return 2
    for known in PR_OR_VENDOR:
        if host == known or host.endswith("." + known):
            return 3
    if host.endswith(".edu") or host.endswith(".ac.uk"):
        return 1
    return 0


def venue_without_recognition(sources_text: str) -> tuple[float | None, int, str]:
    found = hosts(sources_text)
    if not found:
        return None, 0, ""

    tiers = [tier_of(h) for h in found]
    n = len(tiers)
    recognising = sum(1 for t in tiers if t in (1, 2))
    wires = sum(1 for t in tiers if t == 3)
    niche = sum(1 for t in tiers if t == 0)

    value = ((wires + niche) - recognising) / n
    note = (
        f"{n} hosts: {recognising} recognising, {wires} wire/vendor, {niche} niche"
    )
    return round(max(-1.0, min(1.0, value)), 2), n, note


def named_organisations(companies_text: str) -> list[str]:
    parts = [p.strip(" .•-—\t") for p in _SPLIT_ORGS.split(companies_text or "")]
    out: list[str] = []
    for part in parts:
        cleaned = re.sub(r"\([^)]*\)", "", part).strip()
        if len(cleaned) < 2 or _NOISE.match(cleaned):
            continue
        if cleaned.lower() not in {o.lower() for o in out}:
            out.append(cleaned)
    return out


def actor_count(companies_text: str) -> tuple[float | None, int, str]:
    orgs = named_organisations(companies_text)
    n = len(orgs)
    if n == 0:
        return None, 0, ""
    if n == 1:
        value = -0.30
    elif n == 2:
        value = 0.20
    elif n <= 5:
        value = 0.60
    elif n <= 8:
        value = 0.40
    else:
        value = -0.20
    return value, n, f"{n} named: " + ", ".join(orgs[:6])
