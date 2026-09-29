from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b3c8f1d07e45"
down_revision: str | None = "a91e40c7d5b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("source_events", sa.Column("bytes", sa.BigInteger(), nullable=True))
    op.add_column(
        "source_events", sa.Column("requests", sa.Integer(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("source_events", "requests")
    op.drop_column("source_events", "bytes")
