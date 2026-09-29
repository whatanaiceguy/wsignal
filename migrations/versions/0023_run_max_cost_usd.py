from collections.abc import Sequence

from alembic import op

revision: str = "c7d8e9f0a1b2"
down_revision: str | None = "f6a7b8c9d0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE runs ADD COLUMN max_cost_usd FLOAT")


def downgrade() -> None:
    op.execute("ALTER TABLE runs DROP COLUMN max_cost_usd")
