import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from wsignal.config import Settings
from wsignal.inference.orchestration import ORCHESTRATOR_TOOLS, Orchestration
from wsignal.inference.scoring import (
    classify_signal,
    compute_entry_score,
    compute_weak_score,
    entry_ordering,
    entry_ranking_key,
)
from wsignal.models import Entry


def test_entry_score_uses_dimensions_clamps_and_rounds():
    assert compute_entry_score(0.8, 0.4) == 0.7
    assert compute_entry_score(1.0, 1.0, 2.0, 1.0) == 1.0
    assert compute_entry_score(0.0, 0.0, -1.0, 1.0) == 0.0
    assert compute_entry_score(0.12349, 0.56789) == 0.235


@pytest.mark.parametrize(
    ("substance", "momentum", "faintness", "expected"),
    [(0.5, 0.9, 0.9, 0.636), (1.0, 0.4, 0.05, 0.002), (0.1, 0.9, 0.9, 0.285)],
)
def test_weak_score_matches_reference_cases(substance, momentum, faintness, expected):
    assert compute_weak_score(substance, momentum, faintness) == expected


def test_weak_score_clamps_and_rounds():
    assert compute_weak_score(1, 1, 1) == 1.0
    assert compute_weak_score(0, 1, 1) == 0.0


def test_weak_score_faintness_gate_cases():
    assert compute_weak_score(1.0, 0.9, 0.05) < 0.02
    assert abs(compute_weak_score(0.65, 0.85, 0.55) - 0.564) <= 0.005


def test_signal_class_thresholds_and_boundaries():
    assert classify_signal(0.249, 0.9) == "noise"
    assert classify_signal(0.25, 0.9) == "weak"
    assert classify_signal(0.8, 0.35) == "weak"
    assert classify_signal(0.8, 0.275) == "weak"
    assert classify_signal(0.8, 0.25) == "strong"
    assert classify_signal(0.599, 0.1) == "weak"


def test_entry_ranking_puts_new_weak_scores_before_score_ranked_legacy_rows():
    entries = [
        type("Entry", (), {"weak_score": None, "score": 0.99})(),
        type("Entry", (), {"weak_score": 0.4, "score": 0.5})(),
        type("Entry", (), {"weak_score": 0.7, "score": 0.4})(),
    ]
    assert [entry.weak_score for entry in sorted(entries, key=entry_ranking_key)] == [
        0.7, 0.4, None
    ]


def test_database_entry_ordering_uses_weak_score_then_score_and_nulls_last():
    statement = select(Entry).order_by(
        *entry_ordering(Entry.weak_score, Entry.score, Entry.id)
    )
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert "entries.weak_score DESC NULLS LAST" in sql
    assert "entries.score DESC" in sql


def test_score_weights_are_settings_with_contract_defaults():
    settings = Settings(_env_file=None)
    assert settings.entry_score_substance_weight == 0.75
    assert settings.entry_score_momentum_weight == 0.25
    assert settings.signal_noise_below == 0.25
    assert settings.signal_faintness_gate_centre == 0.275
    assert settings.signal_faintness_gate_width == 0.05
    assert settings.signal_strong_substance_at_least == 0.6


def test_write_entry_schema_requires_dimensions_and_pattern_strength_not_score():
    schema = next(
        tool["function"] for tool in ORCHESTRATOR_TOOLS
        if tool["function"]["name"] == "write_entry"
    )["parameters"]
    assert {"substance", "momentum", "faintness"} <= set(schema["required"])
    assert "score" not in schema["required"]
    assert "score" not in schema["properties"]
    assert schema["properties"]["patterns"]["items"]["required"] == [
        "kind", "pattern", "strength"
    ]


@pytest.mark.asyncio
async def test_write_entry_accepts_dimension_and_pattern_strength_boundaries():
    class Session:
        async def get(self, _model, _identity):
            return type("Writer", (), {"id": 1, "run_id": 9, "role": "orchestrator"})()

    orchestration = object.__new__(Orchestration)
    orchestration._session = Session()
    orchestration._run = type("Run", (), {"id": 9})()
    orchestration._partition_worker = False
    arguments = {
        "name_ru": "Имя", "transition_ru": "Переход", "state": "noise",
        "corpus_query": "photonic processors",
        "citations": [],
        "why_ru": "Причина", "current_state_ru": "Состояние", "dynamics_ru": "Динамика",
        "what_would_refute_ru": "Опровержение", "problem_ru": "Проблема",
        "advantage_ru": "Преимущество", "case_example_ru": "Пример",
        "substance": 0, "momentum": 1, "faintness": 0,
        "patterns": [{"kind": "delivery", "pattern": "delivery_gap", "strength": 1}],
    }

    validated, error = await orchestration._validate_entry_arguments(arguments, 1)

    assert error is None
    assert (validated["substance"], validated["momentum"], validated["faintness"]) == (0, 1, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("substance", True),
        ("momentum", float("nan")),
        ("faintness", float("inf")),
        ("substance", -0.01),
        ("momentum", 1.01),
        ("faintness", "0.5"),
    ],
)
async def test_write_entry_rejects_invalid_dimensions(key, value):
    orchestration = object.__new__(Orchestration)
    class Session:
        async def get(self, _model, _identity):
            return type("Writer", (), {"id": 1, "run_id": 9, "role": "orchestrator"})()
    orchestration._session = Session()
    orchestration._run = type("Run", (), {"id": 9})()
    orchestration._partition_worker = False
    arguments = {
        "name_ru": "Имя", "transition_ru": "Переход", "state": "noise",
        "corpus_query": "photonic processors",
        "citations": [],
        "why_ru": "Причина", "current_state_ru": "Состояние", "dynamics_ru": "Динамика",
        "what_would_refute_ru": "Опровержение", "problem_ru": "Проблема",
        "advantage_ru": "Преимущество", "case_example_ru": "Пример",
        "substance": 0.2, "momentum": 0.3, "faintness": 0.4,
    }
    arguments[key] = value
    result, error = await orchestration._validate_entry_arguments(arguments, 1)
    assert result is None
    assert key in error["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), -0.1, 1.1, "0.5"])
async def test_write_entry_rejects_invalid_pattern_strength(value):
    orchestration = object.__new__(Orchestration)
    class Session:
        async def get(self, _model, _identity):
            return type("Writer", (), {"id": 1, "run_id": 9, "role": "orchestrator"})()
    orchestration._session = Session()
    orchestration._run = type("Run", (), {"id": 9})()
    orchestration._partition_worker = False
    arguments = {
        "name_ru": "Имя", "transition_ru": "Переход", "state": "noise",
        "corpus_query": "photonic processors",
        "citations": [],
        "why_ru": "Причина", "current_state_ru": "Состояние", "dynamics_ru": "Динамика",
        "what_would_refute_ru": "Опровержение", "problem_ru": "Проблема",
        "advantage_ru": "Преимущество", "case_example_ru": "Пример",
        "substance": 0.2, "momentum": 0.3, "faintness": 0.4,
        "patterns": [{"kind": "delivery", "pattern": "delivery_gap", "strength": value}],
    }
    result, error = await orchestration._validate_entry_arguments(arguments, 1)
    assert result is None
    assert "strength" in error["error"]


def _entry_ids_orchestration():
    class Session:
        async def get(self, _model, _identity):
            return type("Writer", (), {"id": 1, "run_id": 9, "role": "orchestrator"})()

    orchestration = object.__new__(Orchestration)
    orchestration._session = Session()
    orchestration._run = type("Run", (), {"id": 9})()
    orchestration._partition_worker = False
    arguments = {
        "name_ru": "Имя", "transition_ru": "Переход", "state": "noise",
        "corpus_query": "photonic processors",
        "citations": [],
        "why_ru": "Причина", "current_state_ru": "Состояние", "dynamics_ru": "Динамика",
        "what_would_refute_ru": "Опровержение", "problem_ru": "Проблема",
        "advantage_ru": "Преимущество", "case_example_ru": "Пример",
        "substance": 0.2, "momentum": 0.3, "faintness": 0.4,
    }
    return orchestration, arguments


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "sent", "out", "expected"),
    [
        ("technology_id", "258", "technology_id", 258),
        ("technology_id", " 162 ", "technology_id", 162),
        ("technology_id", "null", "technology_id", None),
        ("technology_id", "0", "technology_id", None),
        ("technology_id", 0, "technology_id", None),
        ("field_id", "448", "field_id", 448),
        ("researched_by_agent_id", "518", "researcher_id", 518),
    ],
)
async def test_write_entry_accepts_ids_sent_as_strings(key, sent, out, expected):
    orchestration, arguments = _entry_ids_orchestration()
    arguments[key] = sent
    validated, error = await orchestration._validate_entry_arguments(arguments, 1)
    assert error is None
    assert validated[out] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("sent", ["abc", "-3", "2.5", 2.5, True, -1])
async def test_write_entry_rejects_bad_ids_and_names_the_value(sent):
    orchestration, arguments = _entry_ids_orchestration()
    arguments["technology_id"] = sent
    result, error = await orchestration._validate_entry_arguments(arguments, 1)
    assert result is None
    assert error["error"].startswith(
        f"technology_id must be a positive integer id from this run, got {sent!r}."
    )
    assert "Omit it rather than guess" in error["error"]


def test_pattern_quotes_match_citations_despite_quote_marks_and_spacing():
    from wsignal.inference.orchestration import _match_quote

    quoted = {"A new “Is MCP” selector is available in HTTP policies.": 7}
    assert _match_quote(quoted, 'A new "Is MCP"  selector is available in HTTP policies') == 7
    assert _match_quote(quoted, "selector is available in HTTP policies") == 7
    assert _match_quote(quoted, "MCP") is None
    assert _match_quote(quoted, "something else entirely here") is None
