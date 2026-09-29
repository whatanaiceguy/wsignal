from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d83c5e21aa07"
down_revision: str | None = "c41f7a9d2be8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "param_extras",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("subject_kind", sa.String(length=16), nullable=False),
        sa.Column("subject_ref", sa.String(length=64), nullable=False),
        sa.Column("pass_label", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("prompt_sha", sa.String(length=16), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "subject_kind IN ('dataset', 'entry')", name="ck_param_extras_subject"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "subject_kind", "subject_ref", "pass_label", name="uq_param_extras"
        ),
    )


def downgrade() -> None:
    op.drop_table("param_extras")
