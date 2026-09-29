from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

AGENT_ROLES = ("orchestrator", "assistant", "researcher", "refuter")

AGENT_STATES = ("running", "idle", "done", "failed")

TURN_KINDS = ("system", "user", "assistant", "tool_call", "tool_result")

FIELD_STATES = ("pending", "dispatched", "done", "dropped")

ENTRY_STATES = ("banked", "parked", "noise", "insufficient")

PATTERN_KINDS = ("faintness", "substance", "delivery")

PARAM_SUBJECTS = ("dataset", "entry")

RUN_STATES = ("running", "finished", "exhausted", "failed")
REFUTATION_STATES = ("pending", "completed", "failed", "cancelled", "interrupted")

HARNESS_NOTE_KINDS = ("bug", "friction", "missing", "idea")


class Base(DeclarativeBase):
    pass


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    direction_ru: Mapped[str | None] = mapped_column(Text)
    direction_en: Mapped[str | None] = mapped_column(Text)
    max_research: Mapped[int | None] = mapped_column(Integer)
    top_n: Mapped[int | None] = mapped_column(Integer)
    shard_size: Mapped[int | None] = mapped_column(Integer)
    orchestrator_max_steps: Mapped[int | None] = mapped_column(Integer)
    max_cost_usd: Mapped[float | None] = mapped_column(Float)
    seeded: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="running")

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "state IN (" + ", ".join(f"'{s}'" for s in RUN_STATES) + ")",
            name="ck_runs_state",
        ),
        CheckConstraint("max_research IS NULL OR max_research > 0", name="ck_runs_max_research"),
        CheckConstraint("top_n IS NULL OR top_n > 0", name="ck_runs_top_n"),
        CheckConstraint("shard_size IS NULL OR shard_size > 0", name="ck_runs_shard_size"),
    )


class Agent(Base):


    __tablename__ = "agents"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    parent_agent_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("agents.id", ondelete="SET NULL")
    )

    role: Mapped[str] = mapped_column(String(16), nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)

    field: Mapped[str | None] = mapped_column(Text)
    brief: Mapped[str | None] = mapped_column(Text)

    state: Mapped[str] = mapped_column(String(16), nullable=False, default="running")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_called_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    returned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "role IN (" + ", ".join(f"'{r}'" for r in AGENT_ROLES) + ")",
            name="ck_agents_role",
        ),
        CheckConstraint(
            "state IN (" + ", ".join(f"'{s}'" for s in AGENT_STATES) + ")",
            name="ck_agents_state",
        ),
        Index("ix_agents_run_role", "run_id", "role"),
    )


class Turn(Base):


    __tablename__ = "turns"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    agent_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)

    content: Mapped[dict] = mapped_column(JSONB, nullable=False)

    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    cached_tokens: Mapped[int | None] = mapped_column(Integer)
    cache_write_tokens: Mapped[int | None] = mapped_column(Integer)
    reasoning_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float | None] = mapped_column(Float)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    attempts: Mapped[int | None] = mapped_column(Integer)


    provider: Mapped[str | None] = mapped_column(Text)
    generation_id: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("agent_id", "seq", name="uq_turns_agent_seq"),
        CheckConstraint(
            "kind IN (" + ", ".join(f"'{k}'" for k in TURN_KINDS) + ")",
            name="ck_turns_kind",
        ),
        Index("ix_turns_agent_kind", "agent_id", "kind"),
    )


class Field(Base):


    __tablename__ = "fields"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )

    area: Mapped[str | None] = mapped_column(Text)
    focus: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text)

    technology_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("technologies.id", ondelete="SET NULL")
    )
    agent_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("agents.id", ondelete="SET NULL")
    )
    orchestrator_agent_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("agents.id", ondelete="SET NULL")
    )

    state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    ordinal: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "state IN (" + ", ".join(f"'{s}'" for s in FIELD_STATES) + ")",
            name="ck_fields_state",
        ),
        Index("ix_fields_run_state", "run_id", "state"),
        Index("ix_fields_orchestrator_agent", "orchestrator_agent_id"),
    )


class FieldRename(Base):
    __tablename__ = "field_renames"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    field_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("fields.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    agent_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    previous_focus: Mapped[str] = mapped_column(Text, nullable=False)
    new_focus: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    verdict_on_previous: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_field_renames_field_created", "field_id", "created_at", "id"),
        Index("ix_field_renames_run", "run_id"),
    )


class Document(Base):

    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)

    title: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha: Mapped[str] = mapped_column(String(64), nullable=False)

    retrieved: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    links_to: Mapped[str | None] = mapped_column(Text)

    source_name: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_lang: Mapped[str] = mapped_column(String(8), nullable=False)
    source_tier: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    published_at: Mapped[date | None] = mapped_column(Date)
    date_source: Mapped[str | None] = mapped_column(String(16))

    adapter: Mapped[str | None] = mapped_column(String(32))

    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    fetched_by_agent_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("agents.id", ondelete="SET NULL")
    )
    fts: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('english', left(coalesce(title, '') || ' ' || content, 500000))",
            persisted=True,
        ),
        deferred=True,
    )

    __table_args__ = (
        UniqueConstraint("url", "content_sha", name="uq_documents_url_content"),
        Index("ix_documents_fts_col", "fts", postgresql_using="gin"),
        Index("ix_documents_url_fetched", "url", "fetched_at"),
        Index("ix_documents_published_at", "published_at"),
        Index("ix_documents_adapter", "adapter"),
    )


class Technology(Base):


    __tablename__ = "technologies"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    canonical_name_ru: Mapped[str | None] = mapped_column(Text)
    canonical_name_en: Mapped[str | None] = mapped_column(Text)
    first_seen: Mapped[date | None] = mapped_column(Date)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "canonical_name_ru IS NOT NULL OR canonical_name_en IS NOT NULL",
            name="ck_technologies_named",
        ),
    )


class Alias(Base):

    __tablename__ = "aliases"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    technology_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("technologies.id", ondelete="CASCADE"), nullable=False
    )
    surface_form: Mapped[str] = mapped_column(Text, nullable=False)
    lang: Mapped[str] = mapped_column(String(8), nullable=False)
    first_seen_by_agent_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("agents.id", ondelete="SET NULL")
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("uq_aliases_surface", text("lower(surface_form)"), "lang", unique=True),
        Index(
            "ix_aliases_surface_trgm",
            "surface_form",
            postgresql_using="gin",
            postgresql_ops={"surface_form": "gin_trgm_ops"},
        ),
    )


class Entry(Base):


    __tablename__ = "entries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    field_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("fields.id", ondelete="SET NULL")
    )

    researched_by_agent_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("agents.id", ondelete="SET NULL")
    )
    written_by_agent_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("agents.id", ondelete="SET NULL")
    )

    technology_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("technologies.id", ondelete="SET NULL")
    )

    name_ru: Mapped[str] = mapped_column(Text, nullable=False)
    name_en: Mapped[str | None] = mapped_column(Text)
    corpus_query: Mapped[str | None] = mapped_column(Text)
    forced_reason: Mapped[str | None] = mapped_column(Text)

    transition_ru: Mapped[str] = mapped_column(Text, nullable=False)

    state: Mapped[str] = mapped_column(String(16), nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    substance: Mapped[float | None] = mapped_column(Float)
    momentum: Mapped[float | None] = mapped_column(Float)
    faintness: Mapped[float | None] = mapped_column(Float)
    weak_score: Mapped[float | None] = mapped_column(Float)
    signal_class: Mapped[str | None] = mapped_column(String(16))

    why_ru: Mapped[str] = mapped_column(Text, nullable=False)
    current_state_ru: Mapped[str | None] = mapped_column(Text)
    dynamics_ru: Mapped[str | None] = mapped_column(Text)

    what_would_refute_ru: Mapped[str | None] = mapped_column(Text)

    searches_run: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sources_checked: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    problem_ru: Mapped[str | None] = mapped_column(Text)
    advantage_ru: Mapped[str | None] = mapped_column(Text)
    case_example_ru: Mapped[str | None] = mapped_column(Text)

    refuter_agent_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("agents.id", ondelete="SET NULL")
    )
    rebutted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "state IN (" + ", ".join(f"'{s}'" for s in ENTRY_STATES) + ")",
            name="ck_entries_state",
        ),
        CheckConstraint("score >= 0.0 AND score <= 1.0", name="ck_entries_score"),
        CheckConstraint(
            "weak_score IS NULL OR (weak_score >= 0.0 AND weak_score <= 1.0)",
            name="ck_entries_weak_score",
        ),
        CheckConstraint(
            "signal_class IS NULL OR signal_class IN ('weak', 'strong', 'noise')",
            name="ck_entries_signal_class",
        ),
        Index("ix_entries_run_score", "run_id", "score"),
        Index("ix_entries_run_weak_score", "run_id", "weak_score"),
        Index("ix_entries_technology", "technology_id"),
    )


class Refutation(Base):
    __tablename__ = "refutations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    researcher_agent_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    refuter_agent_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    rebuttal_required: Mapped[bool] = mapped_column(Boolean, nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default=text("'pending'")
    )
    outcome: Mapped[dict | None] = mapped_column(JSONB)
    collected: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    delivery_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "state IN (" + ", ".join(f"'{s}'" for s in REFUTATION_STATES) + ")",
            name="ck_refutations_state",
        ),
        Index("ix_refutations_researcher_state", "researcher_agent_id", "state"),
        Index("ix_refutations_run_collected", "run_id", "collected"),
    )


class Citation(Base):


    __tablename__ = "citations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    entry_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("entries.id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("documents.id", ondelete="RESTRICT"), nullable=False
    )

    quote: Mapped[str] = mapped_column(Text, nullable=False)
    summary_ru: Mapped[str | None] = mapped_column(Text)
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_citations_entry", "entry_id"),)


class EntryPattern(Base):

    __tablename__ = "entry_patterns"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    entry_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("entries.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    pattern: Mapped[str] = mapped_column(String(64), nullable=False)
    strength: Mapped[float | None] = mapped_column(Float)
    citation_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("citations.id", ondelete="SET NULL")
    )

    __table_args__ = (
        CheckConstraint(
            "kind IN (" + ", ".join(f"'{k}'" for k in PATTERN_KINDS) + ")",
            name="ck_entry_patterns_kind",
        ),
        UniqueConstraint("entry_id", "pattern", name="uq_entry_patterns"),
    )


class RunEvent(Base):
    __tablename__ = "run_events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)

    __table_args__ = (Index("ix_run_events_run_id_id", "run_id", "id"),)


class SourceEvent(Base):

    __tablename__ = "source_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    adapter: Mapped[str] = mapped_column(String(32), nullable=False)
    host: Mapped[str | None] = mapped_column(Text)
    query: Mapped[str | None] = mapped_column(Text)

    via: Mapped[str | None] = mapped_column(String(32))

    ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    items: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)

    bytes: Mapped[int | None] = mapped_column(BigInteger)
    requests: Mapped[int | None] = mapped_column(Integer)
    cached: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    agent_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("agents.id", ondelete="SET NULL")
    )

    __table_args__ = (
        Index("ix_source_events_agent_adapter_via", "agent_id", "adapter", "via"),
        Index("ix_source_events_adapter_ts", "adapter", "ts"),
        Index("ix_source_events_host", "host"),
    )


class ParamReading(Base):

    __tablename__ = "param_readings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    subject_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    subject_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    pass_label: Mapped[str] = mapped_column(String(64), nullable=False)
    param: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[float | None] = mapped_column(Float)
    effort: Mapped[int | None] = mapped_column(Integer)
    evidence: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String(128))
    prompt_sha: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "subject_kind IN (" + ", ".join(f"'{s}'" for s in PARAM_SUBJECTS) + ")",
            name="ck_param_readings_subject",
        ),
        CheckConstraint(
            "value IS NULL OR (value >= -1.0 AND value <= 1.0)",
            name="ck_param_readings_range",
        ),
        CheckConstraint("effort IS NULL OR effort >= 0", name="ck_param_readings_effort"),
        UniqueConstraint(
            "subject_kind",
            "subject_ref",
            "pass_label",
            "param",
            name="uq_param_readings",
        ),
        Index("ix_param_readings_param", "param"),
        Index("ix_param_readings_subject", "subject_kind", "pass_label"),
    )


class ParamExtra(Base):

    __tablename__ = "param_extras"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    subject_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    subject_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    pass_label: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    model: Mapped[str | None] = mapped_column(String(128))
    prompt_sha: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "subject_kind IN (" + ", ".join(f"'{s}'" for s in PARAM_SUBJECTS) + ")",
            name="ck_param_extras_subject",
        ),
        UniqueConstraint(
            "subject_kind", "subject_ref", "pass_label", name="uq_param_extras"
        ),
    )


class HarnessNote(Base):
    __tablename__ = "harness_notes"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("runs.id", ondelete="CASCADE")
    )
    agent_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("agents.id", ondelete="SET NULL")
    )
    role: Mapped[str | None] = mapped_column(String(16))
    model: Mapped[str | None] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    tool: Mapped[str | None] = mapped_column(String(64))
    text: Mapped[str] = mapped_column(Text, nullable=False)
    meta: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "kind IN (" + ", ".join(f"'{k}'" for k in HARNESS_NOTE_KINDS) + ")",
            name="ck_harness_notes_kind",
        ),
        Index("ix_harness_notes_run_id", "run_id", "id"),
        Index("ix_harness_notes_tool_kind", "tool", "kind"),
    )


class EntryEdit(Base):
    __tablename__ = "entry_edits"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    entry_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("entries.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("runs.id", ondelete="CASCADE"), nullable=False
    )
    agent_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("agents.id", ondelete="SET NULL")
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    changes: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_entry_edits_entry", "entry_id", "id"),)
