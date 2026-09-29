from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'bb5e1aff5346'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table('runs',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('query', sa.Text(), nullable=False),
    sa.Column('direction_ru', sa.Text(), nullable=True),
    sa.Column('direction_en', sa.Text(), nullable=True),
    sa.Column('state', sa.String(length=16), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("state IN ('running', 'finished', 'failed')", name='ck_runs_state'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('technologies',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('canonical_name_ru', sa.Text(), nullable=True),
    sa.Column('canonical_name_en', sa.Text(), nullable=True),
    sa.Column('first_seen', sa.Date(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('canonical_name_ru IS NOT NULL OR canonical_name_en IS NOT NULL', name='ck_technologies_named'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('agents',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('run_id', sa.BigInteger(), nullable=False),
    sa.Column('parent_agent_id', sa.BigInteger(), nullable=True),
    sa.Column('role', sa.String(length=16), nullable=False),
    sa.Column('model', sa.Text(), nullable=False),
    sa.Column('field', sa.Text(), nullable=True),
    sa.Column('brief', sa.Text(), nullable=True),
    sa.Column('state', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('last_called_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('returned_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("role IN ('orchestrator', 'assistant', 'researcher', 'refuter')", name='ck_agents_role'),
    sa.CheckConstraint("state IN ('running', 'idle', 'done', 'failed')", name='ck_agents_state'),
    sa.ForeignKeyConstraint(['parent_agent_id'], ['agents.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['run_id'], ['runs.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_agents_run_role', 'agents', ['run_id', 'role'], unique=False)
    op.create_table('aliases',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('technology_id', sa.BigInteger(), nullable=False),
    sa.Column('surface_form', sa.Text(), nullable=False),
    sa.Column('lang', sa.String(length=8), nullable=False),
    sa.Column('first_seen_by_agent_id', sa.BigInteger(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['first_seen_by_agent_id'], ['agents.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['technology_id'], ['technologies.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_aliases_surface_trgm', 'aliases', ['surface_form'], unique=False, postgresql_using='gin', postgresql_ops={'surface_form': 'gin_trgm_ops'})
    op.create_index('uq_aliases_surface', 'aliases', [sa.literal_column('lower(surface_form)'), 'lang'], unique=True)
    op.create_table('documents',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('url', sa.Text(), nullable=False),
    sa.Column('title', sa.Text(), nullable=True),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('content_sha', sa.String(length=64), nullable=False),
    sa.Column('retrieved', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('links_to', sa.Text(), nullable=True),
    sa.Column('source_name', sa.Text(), nullable=False),
    sa.Column('source_type', sa.String(length=32), nullable=False),
    sa.Column('source_lang', sa.String(length=8), nullable=False),
    sa.Column('source_tier', sa.SmallInteger(), nullable=False),
    sa.Column('published_at', sa.Date(), nullable=True),
    sa.Column('fetched_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('fetched_by_agent_id', sa.BigInteger(), nullable=True),
    sa.ForeignKeyConstraint(['fetched_by_agent_id'], ['agents.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('url', 'content_sha', name='uq_documents_url_content')
    )
    op.create_index('ix_documents_url_fetched', 'documents', ['url', 'fetched_at'], unique=False)
    op.create_index('ix_documents_published_at', 'documents', ['published_at'], unique=False)
    op.create_table('fields',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('run_id', sa.BigInteger(), nullable=False),
    sa.Column('area', sa.Text(), nullable=True),
    sa.Column('focus', sa.Text(), nullable=False),
    sa.Column('rationale', sa.Text(), nullable=True),
    sa.Column('technology_id', sa.BigInteger(), nullable=True),
    sa.Column('agent_id', sa.BigInteger(), nullable=True),
    sa.Column('state', sa.String(length=16), nullable=False),
    sa.Column('ordinal', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("state IN ('pending', 'dispatched', 'done', 'dropped')", name='ck_fields_state'),
    sa.ForeignKeyConstraint(['agent_id'], ['agents.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['run_id'], ['runs.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['technology_id'], ['technologies.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_fields_run_state', 'fields', ['run_id', 'state'], unique=False)
    op.create_table('turns',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('agent_id', sa.BigInteger(), nullable=False),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('content', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('prompt_tokens', sa.Integer(), nullable=True),
    sa.Column('completion_tokens', sa.Integer(), nullable=True),
    sa.Column('cached_tokens', sa.Integer(), nullable=True),
    sa.Column('cache_write_tokens', sa.Integer(), nullable=True),
    sa.Column('reasoning_tokens', sa.Integer(), nullable=True),
    sa.Column('cost_usd', sa.Float(), nullable=True),
    sa.Column('duration_ms', sa.Integer(), nullable=True),
    sa.Column('attempts', sa.Integer(), nullable=True),
    sa.Column('provider', sa.Text(), nullable=True),
    sa.Column('generation_id', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("kind IN ('system', 'user', 'assistant', 'tool_call', 'tool_result')", name='ck_turns_kind'),
    sa.ForeignKeyConstraint(['agent_id'], ['agents.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('agent_id', 'seq', name='uq_turns_agent_seq')
    )
    op.create_index('ix_turns_agent_kind', 'turns', ['agent_id', 'kind'], unique=False)
    op.create_table('entries',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('run_id', sa.BigInteger(), nullable=False),
    sa.Column('field_id', sa.BigInteger(), nullable=True),
    sa.Column('researched_by_agent_id', sa.BigInteger(), nullable=True),
    sa.Column('written_by_agent_id', sa.BigInteger(), nullable=True),
    sa.Column('technology_id', sa.BigInteger(), nullable=True),
    sa.Column('name_ru', sa.Text(), nullable=False),
    sa.Column('name_en', sa.Text(), nullable=True),
    sa.Column('transition_ru', sa.Text(), nullable=False),
    sa.Column('state', sa.String(length=16), nullable=False),
    sa.Column('score', sa.Float(), nullable=False),
    sa.Column('why_ru', sa.Text(), nullable=False),
    sa.Column('current_state_ru', sa.Text(), nullable=True),
    sa.Column('dynamics_ru', sa.Text(), nullable=True),
    sa.Column('what_would_refute_ru', sa.Text(), nullable=True),
    sa.Column('searches_run', sa.Integer(), nullable=False),
    sa.Column('sources_checked', sa.Integer(), nullable=False),
    sa.Column('problem_ru', sa.Text(), nullable=True),
    sa.Column('advantage_ru', sa.Text(), nullable=True),
    sa.Column('case_example_ru', sa.Text(), nullable=True),
    sa.Column('refuter_agent_id', sa.BigInteger(), nullable=True),
    sa.Column('rebutted', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("state IN ('banked', 'parked', 'noise', 'insufficient')", name='ck_entries_state'),
    sa.CheckConstraint('score >= 0.0 AND score <= 1.0', name='ck_entries_score'),
    sa.ForeignKeyConstraint(['field_id'], ['fields.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['refuter_agent_id'], ['agents.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['researched_by_agent_id'], ['agents.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['run_id'], ['runs.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['technology_id'], ['technologies.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['written_by_agent_id'], ['agents.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_entries_run_score', 'entries', ['run_id', 'score'], unique=False)
    op.create_index('ix_entries_technology', 'entries', ['technology_id'], unique=False)
    op.create_table('citations',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('entry_id', sa.BigInteger(), nullable=False),
    sa.Column('document_id', sa.BigInteger(), nullable=False),
    sa.Column('quote', sa.Text(), nullable=False),
    sa.Column('verified', sa.Boolean(), nullable=False),
    sa.Column('verified_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['entry_id'], ['entries.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_citations_entry', 'citations', ['entry_id'], unique=False)
    op.create_table('entry_patterns',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('entry_id', sa.BigInteger(), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('pattern', sa.String(length=64), nullable=False),
    sa.Column('citation_id', sa.BigInteger(), nullable=True),
    sa.CheckConstraint("kind IN ('faintness', 'substance')", name='ck_entry_patterns_kind'),
    sa.ForeignKeyConstraint(['citation_id'], ['citations.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['entry_id'], ['entries.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('entry_id', 'pattern', name='uq_entry_patterns')
    )


def downgrade() -> None:
    op.drop_table('entry_patterns')
    op.drop_index('ix_citations_entry', table_name='citations')
    op.drop_table('citations')
    op.drop_index('ix_entries_technology', table_name='entries')
    op.drop_index('ix_entries_run_score', table_name='entries')
    op.drop_table('entries')
    op.drop_index('ix_turns_agent_kind', table_name='turns')
    op.drop_table('turns')
    op.drop_index('ix_fields_run_state', table_name='fields')
    op.drop_table('fields')
    op.drop_index('ix_documents_published_at', table_name='documents')
    op.drop_index('ix_documents_url_fetched', table_name='documents')
    op.drop_table('documents')
    op.drop_index('uq_aliases_surface', table_name='aliases')
    op.drop_index('ix_aliases_surface_trgm', table_name='aliases', postgresql_using='gin', postgresql_ops={'surface_form': 'gin_trgm_ops'})
    op.drop_table('aliases')
    op.drop_index('ix_agents_run_role', table_name='agents')
    op.drop_table('agents')
    op.drop_table('technologies')
    op.drop_table('runs')
