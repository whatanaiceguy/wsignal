from collections.abc import Sequence

from alembic import op

revision: str = "e5f6a7b8c9d0"
down_revision: str | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE entries ADD COLUMN weak_score FLOAT")
    op.execute("ALTER TABLE entries ADD COLUMN signal_class VARCHAR(16)")
    op.execute(
        "ALTER TABLE entries ADD CONSTRAINT ck_entries_weak_score "
        "CHECK (weak_score IS NULL OR (weak_score >= 0.0 AND weak_score <= 1.0))"
    )
    op.execute(
        "ALTER TABLE entries ADD CONSTRAINT ck_entries_signal_class "
        "CHECK (signal_class IS NULL OR signal_class IN ('weak', 'strong', 'noise'))"
    )
    op.execute("CREATE INDEX ix_entries_run_weak_score ON entries (run_id, weak_score)")


def downgrade() -> None:
    op.execute("DROP INDEX ix_entries_run_weak_score")
    op.execute("ALTER TABLE entries DROP CONSTRAINT ck_entries_signal_class")
    op.execute("ALTER TABLE entries DROP CONSTRAINT ck_entries_weak_score")
    op.execute("ALTER TABLE entries DROP COLUMN signal_class")
    op.execute("ALTER TABLE entries DROP COLUMN weak_score")
