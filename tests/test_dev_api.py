from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from wsignal.interface import dev_api
from wsignal.models import (
    Agent,
    Citation,
    Document,
    Entry,
    Refutation,
    Run,
    RunEvent,
    SourceEvent,
    Turn,
)


@pytest.mark.asyncio
async def test_run_subresources_return_404_for_missing_run():
    class Session:
        async def get(self, _model, _run_id):
            return None

    session = Session()
    calls = (
        dev_api.run_agents(999, session),
        dev_api.run_entries(999, session),
        dev_api.run_turns(999, session),
        dev_api.run_source_events(999, session, 2000),
        dev_api.run_errors(999, session),
    )
    for call in calls:
        with pytest.raises(HTTPException) as raised:
            await call
        assert raised.value.status_code == 404


@pytest.mark.asyncio
async def test_run_errors_aggregates_error_sources_and_filters_by_kind():
    now = datetime.now(UTC)
    run = Run(id=7, query="query", state="running", started_at=now)
    agent = Agent(
        id=10, run_id=7, role="researcher", model="model", state="failed", created_at=now,
    )
    turns = [
        Turn(
            id=20, agent_id=10, seq=1, kind="assistant",
            content={"error": "provider down", "reason": "timeout"}, created_at=now,
        ),
        Turn(
            id=21, agent_id=10, seq=2, kind="tool_result",
            content={"name": "submit", "payload": {"error": "invalid submission"}}, created_at=now,
        ),
    ]
    source = SourceEvent(
        id=30, agent_id=10, ts=now, adapter="search", ok=False, error="fetch failed",
    )
    refutation = Refutation(
        id=40, run_id=7, researcher_agent_id=10, refuter_agent_id=10,
        rebuttal_required=True, state="cancelled", outcome={"reason": "cancelled"},
        created_at=now,
    )
    event = RunEvent(id=50, run_id=7, ts=now, kind="agent_failed", payload={
        "agent_id": 10, "reason": "provider error", "error": "provider down",
    })
    document = Document(
        id=60, url="https://example.test", content="fetch failed", content_sha="sha",
        title="Failed fetch", source_name="test", source_type="fetch_failure",
        source_lang="en", source_tier=1, retrieved=False, fetched_by_agent_id=10,
        fetched_at=now,
    )
    entry = Entry(
        id=70, run_id=7, name_ru="entry", transition_ru="transition", state="parked",
        score=0.2, why_ru="why", searches_run=0, sources_checked=0, rebutted=False,
        created_at=now, written_by_agent_id=10,
    )
    citation = Citation(
        id=80, entry_id=70, document_id=60, quote="missing quote", verified=False,
        verified_at=now,
    )

    class Session:
        async def get(self, model, _run_id):
            return run if model is Run else None

        async def scalars(self, statement):
            entity = statement.column_descriptions[0]["entity"]
            values = {
                Agent: [agent],
                Turn: turns,
                SourceEvent: [source],
                Refutation: [refutation],
                RunEvent: [event],
                Document: [document],
            }
            return SimpleNamespace(all=lambda: values[entity])

        async def execute(self, _statement):
            return SimpleNamespace(all=lambda: [(citation, entry)])

    response = await dev_api.run_errors(7, Session())
    kinds = {item.kind for item in response.errors}
    assert {"agent", "model", "tool", "source", "refutation", "fetch", "citation"} <= kinds
    assert response.counts["tool"] == 1
    assert response.total == len(response.errors)
    assert response.errors[0].created_at == now

    filtered = await dev_api.run_errors(7, Session(), kind=["tool"])
    assert [item.kind for item in filtered.errors] == ["tool"]
    assert filtered.counts["source"] == 1


def test_agent_metrics_serialization_carries_model_calls_and_orchestrator_budget():
    metrics = dev_api.AgentMetrics(
        turns=603,
        model_calls=200,
        model_call_budget=300,
        cost_usd=1.0,
        prompt_tokens=0,
        completion_tokens=0,
        cached_tokens=0,
        wall_time_s=0,
        error_turns=0,
    )
    assert metrics.model_dump()["model_calls"] == 200
    assert metrics.model_dump()["model_call_budget"] == 300
    assert metrics.model_dump()["turns"] == 603


@pytest.mark.asyncio
async def test_agent_api_uses_run_budget_and_omits_missing_budget():
    class Session:
        def __init__(self):
            self.results = iter(
                [
                    [
                        SimpleNamespace(
                            agent_id=10,
                            turns=4,
                            model_calls=200,
                            cost_usd=1,
                            prompt_tokens=0,
                            completion_tokens=0,
                            cached_tokens=0,
                            wall_ms=0,
                            error_turns=0,
                        ),
                        SimpleNamespace(
                            agent_id=11,
                            turns=4,
                            model_calls=200,
                            cost_usd=1,
                            prompt_tokens=0,
                            completion_tokens=0,
                            cached_tokens=0,
                            wall_ms=0,
                            error_turns=0,
                        ),
                    ],
                    [(7, "running", None), (8, "running", None)],
                    [(7, 200), (8, None)],
                ]
            )

        async def execute(self, _statement):
            return SimpleNamespace(all=lambda: next(self.results))

    now = datetime.now(UTC)
    agents = [
        Agent(
            id=10,
            run_id=7,
            role="orchestrator",
            model="model",
            state="running",
            created_at=now,
        ),
        Agent(
            id=11,
            run_id=8,
            role="orchestrator",
            model="model",
            state="running",
            created_at=now,
        ),
    ]

    output = await dev_api._agent_out(Session(), agents)

    assert output[0].metrics.model_calls == 200
    assert output[0].metrics.model_call_budget == 200
    assert output[1].metrics.model_call_budget is None


def test_dev_entry_serialization_includes_topic_rename_chain_and_verdict():
    entry = dev_api.EntryOut.model_construct(
        topic_original_focus="SSD",
        topic_rename_history=[
            {
                "previous_focus": "SSD",
                "new_focus": "SSD as KV-cache tier for inference",
                "reason": "Evidence points to inference caches.",
                "verdict_on_previous": "Mainstream, strong signal.",
            }
        ],
    )
    serialized = entry.model_dump()
    assert serialized["topic_original_focus"] == "SSD"
    assert serialized["topic_rename_history"][0]["verdict_on_previous"] == (
        "Mainstream, strong signal."
    )


def test_dev_entry_serialization_includes_dimensions_and_pattern_strength():
    pattern = dev_api.EntryPatternOut.model_construct(strength=0.6)
    entry = dev_api.EntryOut.model_construct(
        score=0.7,
        weak_score=0.6,
        signal_class="weak",
        substance=0.8,
        momentum=0.4,
        faintness=0.2,
        entry_patterns=[pattern],
    )

    serialized = entry.model_dump()
    assert (serialized["substance"], serialized["momentum"], serialized["faintness"]) == (
        0.8, 0.4, 0.2
    )
    assert serialized["entry_patterns"][0]["strength"] == 0.6
    assert serialized["weak_score"] == 0.6
    assert serialized["signal_class"] == "weak"

