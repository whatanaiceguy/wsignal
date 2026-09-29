from collections.abc import Sequence

from alembic import op

revision: str = "f7b8c9d0e1a2"
down_revision: str | None = "e5c3a0b7f214"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE citations ADD COLUMN summary_ru TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE citations DROP COLUMN summary_ru")
