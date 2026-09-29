from collections.abc import Sequence

from alembic import op

revision: str = "c3a1e8f5d902"
down_revision: str | None = "b8f31d6e4a92"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE fields ADD COLUMN orchestrator_agent_id BIGINT "
        "REFERENCES agents(id) ON DELETE SET NULL"
    )
    op.execute(
        "CREATE INDEX ix_fields_orchestrator_agent "
        "ON fields (orchestrator_agent_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX ix_fields_orchestrator_agent")
    op.execute("ALTER TABLE fields DROP COLUMN orchestrator_agent_id")
