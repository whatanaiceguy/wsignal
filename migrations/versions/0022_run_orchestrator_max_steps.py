from collections.abc import Sequence

from alembic import op

revision: str = "f6a7b8c9d0e1"
down_revision: str | None = "e5f6a7b8c9d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE runs ADD COLUMN orchestrator_max_steps INTEGER")


def downgrade() -> None:
    op.execute("ALTER TABLE runs DROP COLUMN orchestrator_max_steps")
