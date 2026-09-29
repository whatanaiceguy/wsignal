from collections.abc import Sequence

from alembic import op

revision: str = "d58b3c02a7f1"
down_revision: str | None = "c47a2b91f6e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FTS_EXPRESSION = (
    "to_tsvector('english', left(coalesce(title, '') || ' ' || content, 500000))"
)


def upgrade() -> None:
    op.execute(
        "ALTER TABLE documents ADD COLUMN fts tsvector "
        f"GENERATED ALWAYS AS ({FTS_EXPRESSION}) STORED"
    )
    op.execute("CREATE INDEX ix_documents_fts_col ON documents USING gin (fts)")
    op.execute("DROP INDEX IF EXISTS ix_documents_fts")


def downgrade() -> None:
    op.execute(f"CREATE INDEX ix_documents_fts ON documents USING gin ({FTS_EXPRESSION})")
    op.execute("DROP INDEX IF EXISTS ix_documents_fts_col")
    op.execute("ALTER TABLE documents DROP COLUMN fts")
