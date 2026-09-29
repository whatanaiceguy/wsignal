from collections.abc import Sequence

from alembic import op

revision: str = "f7d2b91c6e04"
down_revision: str | None = "e5a1c7b40f92"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FTS_EXPRESSION = (
    "to_tsvector('english', left(coalesce(title, '') || ' ' || content, 500000))"
)


def upgrade() -> None:
    op.execute(f"CREATE INDEX ix_documents_fts ON documents USING gin ({FTS_EXPRESSION})")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_documents_fts")
