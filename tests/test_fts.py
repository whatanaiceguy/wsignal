from pathlib import Path

from wsignal.parsing.fts import FTS_COLUMN, FTS_EXPRESSION, FTS_INDEX, FTS_MAX_CHARS

VERSIONS = Path(__file__).resolve().parents[1] / "migrations" / "versions"
COLUMN_MIGRATION = VERSIONS / "0009_documents_fts_column.py"


def test_the_column_is_generated_from_the_expression_this_module_defines():
    source = COLUMN_MIGRATION.read_text(encoding="utf-8")
    assert FTS_EXPRESSION in source, (
        "0009's generated column no longer matches "
        "wsignal.parsing.fts.FTS_EXPRESSION"
    )
    assert f"ADD COLUMN {FTS_COLUMN} tsvector " in source
    assert "GENERATED ALWAYS AS" in source and "STORED" in source


def test_the_index_is_over_the_column_and_the_old_one_is_gone():
    source = COLUMN_MIGRATION.read_text(encoding="utf-8")
    assert f"CREATE INDEX {FTS_INDEX} ON documents USING gin ({FTS_COLUMN})" in source
    assert "DROP INDEX IF EXISTS ix_documents_fts" in source


def test_the_cap_stays_under_postgres_tsvector_limit():
    assert FTS_MAX_CHARS <= 500_000
