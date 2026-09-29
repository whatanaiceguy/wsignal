from __future__ import annotations

FTS_CONFIG = "english"
FTS_MAX_CHARS = 500_000

FTS_EXPRESSION = (
    f"to_tsvector('{FTS_CONFIG}', "
    f"left(coalesce(title, '') || ' ' || content, {FTS_MAX_CHARS}))"
)

FTS_COLUMN = "fts"
FTS_INDEX = "ix_documents_fts_col"
