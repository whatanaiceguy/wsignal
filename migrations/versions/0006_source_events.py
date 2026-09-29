from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a91e40c7d5b3"
down_revision: str | None = "f7d2b91c6e04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "source_events",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column(
            "ts",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("adapter", sa.String(length=32), nullable=False),
        sa.Column("host", sa.Text(), nullable=True),
        sa.Column("query", sa.Text(), nullable=True),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("items", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column(
            "cached",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("agent_id", sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_source_events_adapter_ts", "source_events", ["adapter", "ts"]
    )
    op.create_index("ix_source_events_host", "source_events", ["host"])


def downgrade() -> None:
    op.drop_index("ix_source_events_host", table_name="source_events")
    op.drop_index("ix_source_events_adapter_ts", table_name="source_events")
    op.drop_table("source_events")
