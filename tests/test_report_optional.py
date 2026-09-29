from datetime import UTC, datetime

import pytest

from wsignal.interface.report import render
from wsignal.interface.schemas import Entry, SearchResponse, SearchStats


def _response(**optional_fields):
    entry = Entry(
        id=101,
        rank=1,
        name_ru="Технология",
        name_en=None,
        transition_ru="transition",
        score=0.8,
        state="banked",
        why_ru="Основное обоснование.",
        searches_run=1,
        sources_checked=1,
        **optional_fields,
    )
    return SearchResponse(
        run_id=1,
        state="finished",
        query="query",
        generated_at=datetime(2026, 9, 23, tzinfo=UTC),
        stats=SearchStats(
            candidates_found=1,
            sources_processed=0,
            high_confidence_count=0,
            agents_spawned=0,
            citations_verified=0,
            citations_total=0,
            elapsed_seconds=0,
        ),
        entries=[entry],
        models_used={},
    )


@pytest.mark.parametrize(
    "field,label,value",
    [
        ("what_would_refute_ru", "Что опровергло бы:", "Факт опровержения."),
        ("current_state_ru", "Текущее состояние:", "Состояние сейчас."),
        ("dynamics_ru", "Динамика:", "Наблюдаемая динамика."),
        ("problem_ru", "Какую проблему решает:", "Проблема клиента."),
        ("advantage_ru", "Потенциальное преимущество:", "Преимущество технологии."),
        ("case_example_ru", "Кейс:", "Пример внедрения."),
    ],
)
def test_populated_optional_prose_field_is_rendered(field, label, value):
    text = render(_response(**{field: value}), top=5)

    assert label in text
    assert value in text


@pytest.mark.parametrize(
    "field",
    [
        "what_would_refute_ru",
        "current_state_ru",
        "dynamics_ru",
        "problem_ru",
        "advantage_ru",
        "case_example_ru",
    ],
)
@pytest.mark.parametrize("blank", [None, "", "  \n  "])
def test_blank_optional_prose_field_is_omitted(field, blank):
    text = render(_response(**{field: blank}), top=5)

    label = {
        "what_would_refute_ru": "Что опровергло бы:",
        "current_state_ru": "Текущее состояние:",
        "dynamics_ru": "Динамика:",
        "problem_ru": "Какую проблему решает:",
        "advantage_ru": "Потенциальное преимущество:",
        "case_example_ru": "Кейс:",
    }[field]
    assert label not in text
