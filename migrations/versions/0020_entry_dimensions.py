from collections.abc import Sequence

from alembic import op

revision: str = "d4e5f6a7b8c9"
down_revision: str | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE entries ADD COLUMN substance FLOAT")
    op.execute("ALTER TABLE entries ADD COLUMN momentum FLOAT")
    op.execute("ALTER TABLE entries ADD COLUMN faintness FLOAT")
    op.execute("ALTER TABLE entry_patterns ADD COLUMN strength FLOAT")


def downgrade() -> None:
    op.execute("ALTER TABLE entry_patterns DROP COLUMN strength")
    op.execute("ALTER TABLE entries DROP COLUMN faintness")
    op.execute("ALTER TABLE entries DROP COLUMN momentum")
    op.execute("ALTER TABLE entries DROP COLUMN substance")
