from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c41f7a9d2be8"
down_revision: str | None = "bb5e1aff5346"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_entry_patterns_kind", "entry_patterns", type_="check")
    op.create_check_constraint(
        "ck_entry_patterns_kind",
        "entry_patterns",
        "kind IN ('faintness', 'substance', 'delivery')",
    )

    op.create_table(
        "param_readings",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("subject_kind", sa.String(length=16), nullable=False),
        sa.Column("subject_ref", sa.String(length=64), nullable=False),
        sa.Column("pass_label", sa.String(length=64), nullable=False),
        sa.Column("param", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Float(), nullable=True),
        sa.Column("effort", sa.Integer(), nullable=True),
        sa.Column("evidence", sa.Text(), nullable=True),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("prompt_sha", sa.String(length=16), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "subject_kind IN ('dataset', 'entry')", name="ck_param_readings_subject"
        ),
        sa.CheckConstraint(
            "value IS NULL OR (value >= -1.0 AND value <= 1.0)",
            name="ck_param_readings_range",
        ),
        sa.CheckConstraint("effort IS NULL OR effort >= 0", name="ck_param_readings_effort"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "subject_kind", "subject_ref", "pass_label", "param", name="uq_param_readings"
        ),
    )
    op.create_index("ix_param_readings_param", "param_readings", ["param"])
    op.create_index(
        "ix_param_readings_subject", "param_readings", ["subject_kind", "pass_label"]
    )


def downgrade() -> None:
    op.drop_index("ix_param_readings_subject", table_name="param_readings")
    op.drop_index("ix_param_readings_param", table_name="param_readings")
    op.drop_table("param_readings")

    op.drop_constraint("ck_entry_patterns_kind", "entry_patterns", type_="check")
    op.create_check_constraint(
        "ck_entry_patterns_kind", "entry_patterns", "kind IN ('faintness', 'substance')"
    )
