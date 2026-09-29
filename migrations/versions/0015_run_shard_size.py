from collections.abc import Sequence

from alembic import op

revision: str = "e5c3a0b7f214"
down_revision: str | None = "d4b2f9a6e013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE runs ADD COLUMN shard_size INTEGER")
    op.execute(
        "ALTER TABLE runs ADD CONSTRAINT ck_runs_shard_size "
        "CHECK (shard_size IS NULL OR shard_size > 0)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE runs DROP CONSTRAINT ck_runs_shard_size")
    op.execute("ALTER TABLE runs DROP COLUMN shard_size")
