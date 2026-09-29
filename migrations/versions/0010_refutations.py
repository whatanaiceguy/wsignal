from collections.abc import Sequence

from alembic import op

revision: str = "6f2d8a91c4e7"
down_revision: str | None = "d58b3c02a7f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE refutations (
            id BIGSERIAL PRIMARY KEY,
            run_id BIGINT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
            researcher_agent_id BIGINT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
            refuter_agent_id BIGINT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
            rebuttal_required BOOLEAN NOT NULL,
            state VARCHAR(16) NOT NULL DEFAULT 'pending',
            outcome JSONB,
            collected BOOLEAN NOT NULL DEFAULT false,
            delivery_version INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            completed_at TIMESTAMPTZ,
            CONSTRAINT ck_refutations_state CHECK (state IN ('pending', 'completed', 'failed', 'cancelled', 'interrupted'))
        )
        """
    )
    op.execute("CREATE INDEX ix_refutations_researcher_state ON refutations (researcher_agent_id, state)")
    op.execute("CREATE INDEX ix_refutations_run_collected ON refutations (run_id, collected)")


def downgrade() -> None:
    op.execute("DROP TABLE refutations")
