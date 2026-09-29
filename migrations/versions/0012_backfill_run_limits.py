from collections.abc import Sequence

from alembic import op

revision: str = "b8f31d6e4a92"
down_revision: str | None = "a4e7c91d2b60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        r"""
        WITH opening AS (
            SELECT DISTINCT ON (a.run_id)
                a.run_id,
                (regexp_match(
                    t.content->>'content',
                    'исследовать не более ([0-9]+) технолог',
                    'i'
                ))[1]::integer AS max_research,
                (regexp_match(
                    t.content->>'content',
                    'Показаны будут лучшие ([0-9]+)',
                    'i'
                ))[1]::integer AS top_n
            FROM agents a
            JOIN turns t ON t.agent_id = a.id
            WHERE a.role = 'orchestrator' AND t.kind = 'user'
            ORDER BY a.run_id, t.seq
        )
        UPDATE runs r
        SET max_research = COALESCE(r.max_research, opening.max_research),
            top_n = COALESCE(r.top_n, opening.top_n)
        FROM opening
        WHERE opening.run_id = r.id
          AND (r.max_research IS NULL OR r.top_n IS NULL)
        """
    )


def downgrade() -> None:
    pass
