from collections.abc import Sequence

from alembic import op

revision: str = "a4e7c91d2b60"
down_revision: str | None = "6f2d8a91c4e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE runs ADD COLUMN max_research INTEGER")
    op.execute("ALTER TABLE runs ADD COLUMN top_n INTEGER")
    op.execute(
        "ALTER TABLE runs ADD CONSTRAINT ck_runs_max_research "
        "CHECK (max_research IS NULL OR max_research > 0)"
    )
    op.execute(
        "ALTER TABLE runs ADD CONSTRAINT ck_runs_top_n "
        "CHECK (top_n IS NULL OR top_n > 0)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE runs DROP CONSTRAINT ck_runs_top_n")
    op.execute("ALTER TABLE runs DROP CONSTRAINT ck_runs_max_research")
    op.execute("ALTER TABLE runs DROP COLUMN top_n")
    op.execute("ALTER TABLE runs DROP COLUMN max_research")
