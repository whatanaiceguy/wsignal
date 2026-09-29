from collections.abc import Sequence

from alembic import op

revision: str = "d4b2f9a6e013"
down_revision: str | None = "c3a1e8f5d902"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE runs DROP CONSTRAINT ck_runs_state")
    op.execute(
        "ALTER TABLE runs ADD CONSTRAINT ck_runs_state "
        "CHECK (state IN ('running', 'finished', 'exhausted', 'failed'))"
    )


def downgrade() -> None:
    op.execute("UPDATE runs SET state = 'failed' WHERE state = 'exhausted'")
    op.execute("ALTER TABLE runs DROP CONSTRAINT ck_runs_state")
    op.execute(
        "ALTER TABLE runs ADD CONSTRAINT ck_runs_state "
        "CHECK (state IN ('running', 'finished', 'failed'))"
    )
