from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5a1c7b40f92"
down_revision: str | None = "d83c5e21aa07"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "documents", sa.Column("adapter", sa.String(length=32), nullable=True)
    )
    op.create_index("ix_documents_adapter", "documents", ["adapter"])


def downgrade() -> None:
    op.drop_index("ix_documents_adapter", table_name="documents")
    op.drop_column("documents", "adapter")
