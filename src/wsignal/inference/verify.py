from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime

from rapidfuzz.distance import Levenshtein
from rapidfuzz.fuzz import partial_ratio_alignment
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from wsignal.config import get_settings
from wsignal.models import Citation, Document, Entry

_WHITESPACE = re.compile(r"\s+")
_ELLIPSIS = re.compile(r"(?:\[\.\.\.\]|\(\.\.\.\)|\.\.\.|…)")
_NUMBERS = re.compile(r"\d+(?:[.,]\d+)*")
_WRAPPING_QUOTES = "'\"‘’“”«»‹›「」『』"

_FOLD = {
    " ": " ",
    "‘": "'",
    "’": "'",
    "“": '"',
    "”": '"',
    "«": '"',
    "»": '"',
    "–": "-",
    "—": "-",
    "−": "-",
}


def normalise(text: str) -> str:

    folded = unicodedata.normalize("NFKC", text)
    for source, target in _FOLD.items():
        folded = folded.replace(source, target)
    return _WHITESPACE.sub(" ", folded).strip().casefold()


def _fragments(quote: str) -> list[str]:
    return [
        fragment.strip().strip(_WRAPPING_QUOTES).strip()
        for fragment in _ELLIPSIS.split(quote)
        if fragment.strip().strip(_WRAPPING_QUOTES).strip()
    ]


def _cuts_a_number(content: str, start: int, end: int) -> bool:
    before = start > 0 and content[start - 1].isdigit() and content[start].isdigit()
    after = end < len(content) and content[end].isdigit() and content[end - 1].isdigit()
    return before or after


def _same_numbers(fragment: str, content: str, start: int, end: int) -> bool:
    if _cuts_a_number(content, start, end):
        return False
    return _NUMBERS.findall(fragment) == _NUMBERS.findall(content[start:end])


def _best_match_end(fragment: str, content: str, start: int) -> int | None:
    exact_start = content.find(fragment, start)
    while exact_start >= 0:
        if not _cuts_a_number(content, exact_start, exact_start + len(fragment)):
            return exact_start + len(fragment)
        exact_start = content.find(fragment, exact_start + 1)

    settings = get_settings()
    threshold = settings.citation_quote_similarity_threshold
    alignment = partial_ratio_alignment(fragment, content[start:])
    if alignment is None or alignment.score < threshold * 100:
        return None

    minimum_length = max(1, (len(fragment) * 9 + 9) // 10)
    maximum_length = max(minimum_length, len(fragment) * 11 // 10)
    radius = max(1, len(fragment) - minimum_length)
    aligned_start = start + alignment.dest_start
    first_start = max(start, aligned_start - radius)
    last_start = min(len(content) - minimum_length, aligned_start + radius)
    max_distance = int(len(fragment) * (1.0 - threshold) + 1e-9)
    best_end = None

    final_window_end = min(len(content), last_start + maximum_length)
    for window_end in range(first_start + minimum_length, final_window_end + 1):
        if best_end is not None and window_end >= best_end:
            break
        first_window_start = max(first_start, window_end - maximum_length)
        last_window_start = min(last_start, window_end - minimum_length)
        for window_start in range(first_window_start, last_window_start + 1):
            window = content[window_start:window_end]
            if not minimum_length <= len(window) <= maximum_length:
                continue
            if Levenshtein.distance(
                fragment, window, score_cutoff=max_distance
            ) <= max_distance and _same_numbers(fragment, content, window_start, window_end):
                best_end = window_end
                break
    return best_end


def quote_appears_in(quote: str, content: str) -> bool:
    cleaned = normalise(quote)
    page = normalise(content)
    if not cleaned:
        return False
    fragments = [normalise(fragment) for fragment in _fragments(quote)]
    if not fragments:
        return False
    if not any(len(fragment) >= 20 for fragment in fragments):
        return False
    position = 0
    for fragment in fragments:
        match_end = _best_match_end(fragment, page, position)
        if match_end is None:
            return False
        position = match_end
    return True


async def verify_run(session: AsyncSession, run_id: int) -> dict[str, int]:
    rows = (
        await session.execute(
            select(Citation, Document.content, Document.source_type)
            .join(Entry, Entry.id == Citation.entry_id)
            .join(Document, Document.id == Citation.document_id)
            .where(Entry.run_id == run_id)
        )
    ).all()

    verified = 0
    for row in rows:
        citation, content = row[:2]
        document_type = row[2] if len(row) > 2 else None
        ok = document_type != "fetch_failure" and quote_appears_in(
            citation.quote, content or ""
        )
        citation.verified = ok
        citation.verified_at = datetime.now(UTC)
        verified += int(ok)

    await session.commit()
    return {"checked": len(rows), "verified": verified, "failed": len(rows) - verified}
