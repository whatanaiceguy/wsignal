from datetime import UTC, date, datetime, timedelta

import pytest

from wsignal.interface.report import _stats, render, render_meta, save
from wsignal.interface.schemas import (
    Citation,
    Entry,
    EntryState,
    Pattern,
    PatternKind,
    SearchResponse,
    SearchStats,
    SourceType,
)
from wsignal.models import ENTRY_STATES, PATTERN_KINDS, Run
from wsignal.parsing.base import TIER_SOCIAL, tier_name


def _citation(**over) -> Citation:
    base = dict(
        url="https://siliconangle.com/2026/03/04/agent-iam",
        quote="a verbatim span from the page",
        verified=True,
        name="siliconangle.com",
        published_at=date(2026, 3, 4),
        type="news",
        lang="en",
        tier="trade",
        title_original="Agent IAM startups leave stealth",
    )
    base.update(over)
    return Citation(**base)


def _entry(rank: int, **over) -> Entry:
    base = dict(
        id=100 + rank,
        rank=rank,
        name_ru=f"Технология {rank}",
        name_en=f"Technology {rank}",
        transition_ru="ПОРОГОВАЯФОРМУЛИРОВКА",
        score=1.0 - rank / 100,
        substance=0.8,
        momentum=0.6,
        faintness=0.9,
        state="banked",
        why_ru="ОБОСНОВАНИЕМОДЕЛИ связной прозой, и это то, что покупатель просил.",
        what_would_refute_ru="Одна строка в закупках",
        searches_run=40,
        sources_checked=12,
        citations=[_citation()],
    )
    base.update(over)
    return Entry(**base)


def _response(n: int = 3, **over) -> SearchResponse:
    entries = over.pop("entries", [_entry(i) for i in range(1, n + 1)])
    return SearchResponse(
        run_id=1,
        state="finished",
        query="слабые сигналы в области кибербезопасности",
        generated_at=datetime(2026, 9, 18, 3, 0, tzinfo=UTC),
        stats=SearchStats(
            candidates_found=len(entries),
            sources_processed=41,
            high_confidence_count=2,
            agents_spawned=9,
            citations_verified=2,
            citations_total=3,
            elapsed_seconds=612.0,
        ),
        entries=entries,
        models_used={"orchestrator": "openai/gpt-5.6-luna"},
        **over,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("with_events,expected", [(True, 2441.1), (False, 6062.1)])
async def test_report_elapsed_excludes_idle_gap_and_preserves_rounding(with_events, expected):
    start = datetime(2026, 9, 29, 17, 52, tzinfo=UTC)
    run = Run(
        id=3, query="resumed", state="exhausted", started_at=start,
        finished_at=start + timedelta(seconds=6062.14),
    )

    class Session:
        async def scalar(self, statement):
            return 0

        async def execute(self, statement):
            assert statement.compile().params["run_id_1"] == 3
            assert "run_events.ts, run_events.id" in str(statement)
            return [
                (kind, start + timedelta(seconds=seconds))
                for kind, seconds in [
                    ("run_started", 0), ("run_stopped", 2402),
                    ("run_started", 6023), ("run_stopped", 6062.14),
                ]
            ] if with_events else []

    stats = await _stats(Session(), run, [])

    assert stats.elapsed_seconds == expected
    assert isinstance(stats.elapsed_seconds, float)


def test_the_reasoning_is_in_the_text():
    response = _response()
    assert "ОБОСНОВАНИЕМОДЕЛИ" in render(response, top=15)
    serialized = response.model_dump()["entries"][0]
    assert (serialized["substance"], serialized["momentum"], serialized["faintness"]) == (
        0.8, 0.6, 0.9
    )
    assert "Измерения" in render(response, top=15)


def test_the_transition_stays_in_the_contract_and_out_of_the_text():
    response = _response()
    assert "ПОРОГОВАЯФОРМУЛИРОВКА" not in render(response, top=15)
    assert "ПОРОГОВАЯФОРМУЛИРОВКА" in response.model_dump_json()


def test_all_six_per_source_fields_reach_the_page():
    text = render(_response(), top=15)
    for required in ("siliconangle.com", "news", "en", "trade", "2026-03-04"):
        assert required in text
    assert "https://siliconangle.com/2026/03/04/agent-iam" in text


def test_an_unverified_quote_says_so():
    entry = _entry(1, citations=[_citation(verified=False)])
    assert "НЕ НАЙДЕНА" in render(_response(entries=[entry]), top=15)


def test_failed_citation_fetch_remains_visible_in_report():
    entry = _entry(1, citations=[_citation(type="fetch_failure", verified=False)])
    text = render(_response(entries=[entry]), top=15)
    assert "fetch_failure" in text
    assert "НЕ НАЙДЕНА" in text


def test_the_exclusion_line_lands_where_top_says():
    text = render(_response(n=5), top=2)
    assert "НИЖЕ ТОП-2" in text
    assert text.index("НИЖЕ ТОП-2") < text.index("Технология 3")
    assert text.index("Технология 2") < text.index("НИЖЕ ТОП-2")


def test_no_exclusion_line_when_nothing_is_excluded():
    assert "НИЖЕ ТОП" not in render(_response(n=3), top=15)


def test_patterns_name_the_citation_that_earns_them():
    entry = _entry(
        1, patterns=[
            Pattern(
                kind="faintness", pattern="no_procurement_category", citation_index=0,
                strength=0.7,
            )
        ]
    )
    text = render(_response(entries=[entry]), top=15)
    assert "faintness:no_procurement_category strength=0.7 [1]" in text
    assert entry.model_dump()["patterns"][0]["strength"] == 0.7


def test_an_empty_run_renders_rather_than_raising():
    assert "Ни одного" in render(_response(entries=[]), top=15)


def test_meta_survives_an_accounting_block_that_could_not_be_computed():
    text = render_meta(accounting={}, agents=[], context=[], sources=[])
    assert "СТОИМОСТЬ ЗАПУСКА" in text


def test_meta_separates_an_empty_answer_from_a_refusal():
    text = render_meta(
        accounting={},
        agents=[],
        context=[],
        sources=[
            {
                "adapter": "ddg",
                "calls": 12,
                "ok": 12,
                "failed": 0,
                "empty": 11,
                "cached": 0,
                "items": 4,
                "bytes": 2_097_152,
                "requests": 12,
                "avg_s": 1.4,
            }
        ],
    )
    assert "ddg" in text
    assert "пусто" in text


def test_save_writes_the_text_and_the_contract_beside_it(tmp_path):
    response = _response()
    written = save(7, render(response, top=15), response, out_dir=tmp_path)
    assert [p.name for p in written] == ["7.txt", "7.json"]
    assert "ОБОСНОВАНИЕМОДЕЛИ" in written[0].read_text(encoding="utf-8")
    assert "transition_ru" in written[1].read_text(encoding="utf-8")
    assert '"substance": 0.8' in written[1].read_text(encoding="utf-8")


def test_save_suffix_keeps_the_full_render_beside_the_plain_one(tmp_path):
    save(7, "plain", None, out_dir=tmp_path)
    save(7, "plain+meta", None, out_dir=tmp_path, suffix="-full")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["7-full.txt", "7.txt"]


@pytest.mark.parametrize(
    "stored,expected",
    [
        (0, "unknown"),
        (1, "authoritative"),
        (2, "trade"),
        (3, "social"),
        (9, "unknown"),
        (-1, "unknown"),
    ],
)
def test_tier_is_mapped_from_the_integer_the_column_holds(stored, expected):
    assert tier_name(stored) == expected
    assert tier_name(TIER_SOCIAL) == "social"


@pytest.mark.parametrize(
    ("contract", "stored"),
    [
        (PatternKind, PATTERN_KINDS),
        (EntryState, ENTRY_STATES),
        (SourceType, ("news", "page", "paper", "patent", "social", "fetch_failure")),
    ],
)
def test_every_value_the_store_holds_is_accepted_by_the_response(contract, stored):
    for value in stored:
        assert contract(value).value == value
