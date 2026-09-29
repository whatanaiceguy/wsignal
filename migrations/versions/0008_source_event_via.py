from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c47a2b91f6e8"
down_revision: str | None = "b3c8f1d07e45"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("source_events", sa.Column("via", sa.String(32), nullable=True))
    op.create_index(
        "ix_source_events_agent_adapter_via",
        "source_events",
        ["agent_id", "adapter", "via"],
    )


def downgrade() -> None:
    op.drop_index("ix_source_events_agent_adapter_via", table_name="source_events")
    op.drop_column("source_events", "via")
