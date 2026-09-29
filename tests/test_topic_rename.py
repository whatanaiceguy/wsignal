from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from wsignal.inference.tools import ROLE_TOOLS, Toolbox, tools_for
from wsignal.interface.report import render
from wsignal.interface.schemas import Entry, SearchResponse, SearchStats
from wsignal.models import FieldRename


class _Scalars:
    def __init__(self, values):
        self.values = values

    def all(self):
        return self.values


class _Session:
    def __init__(self, agents, field):
        self.agents = agents
        self.field = field
        self.added = []

    async def get(self, model, identity):
        if model.__name__ == "Agent":
            return self.agents.get(identity)
        return None

    async def scalar(self, statement):
        sql = str(statement)
        if "refutations.refuter_agent_id" in sql:
            return SimpleNamespace(researcher_agent_id=30, state="pending")
        if "fields." in sql:
            return self.field
        return None

    async def scalars(self, statement):
        sql = str(statement)
        if "refutations.refuter_agent_id" in sql:
            return _Scalars([44])
        if "agents.id IN" in sql:
            return _Scalars([self.agents[30], self.agents[44]])
        return _Scalars([])

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        pass

    async def rollback(self):
        pass


def test_rename_topic_is_available_to_field_roles_and_requires_field_id_for_orchestrator():
    for role in ("researcher", "refuter", "orchestrator"):
        assert "rename_topic" in ROLE_TOOLS[role]
        schema = next(
            tool["function"] for tool in tools_for(role)
            if tool["function"]["name"] == "rename_topic"
        )
        required = schema["parameters"]["required"]
        assert "new_focus" in required
        assert "reason" in required
        assert "verdict_on_previous" in required
        assert ("field_id" in required) is (role == "orchestrator")


@pytest.mark.asyncio
async def test_rename_persists_history_and_updates_field_and_agents():
    researcher = SimpleNamespace(id=30, run_id=9, role="researcher", field="SSD")
    refuter = SimpleNamespace(id=44, run_id=9, role="refuter", field="SSD")
    field = SimpleNamespace(
        id=1,
        run_id=9,
        focus="SSD",
        agent_id=30,
        technology_id=50,
        state="dispatched",
    )
    session = _Session({30: researcher, 44: refuter}, field)
    toolbox = Toolbox(session, None)

    result = await toolbox.call(
        "rename_topic",
        {
            "new_focus": "SSD as KV-cache tier for inference",
            "reason": "Retrieved deployments concern inference cache tiers.",
            "verdict_on_previous": "SSD is mainstream and a strong signal.",
        },
        30,
    )

    history = next(row for row in session.added if isinstance(row, FieldRename))
    assert result["renamed"] is True
    assert (history.field_id, history.run_id, history.agent_id) == (1, 9, 30)
    assert history.previous_focus == "SSD"
    assert history.new_focus == "SSD as KV-cache tier for inference"
    assert history.verdict_on_previous == "SSD is mainstream and a strong signal."
    assert field.focus == "SSD as KV-cache tier for inference"
    assert researcher.field == field.focus
    assert refuter.field == field.focus


@pytest.mark.asyncio
async def test_refuter_rename_targets_researcher_field():
    refuter = SimpleNamespace(id=44, run_id=9, role="refuter", field="SSD")
    researcher = SimpleNamespace(id=30, run_id=9, role="researcher", field="SSD")
    field = SimpleNamespace(
        id=1,
        run_id=9,
        focus="SSD",
        agent_id=30,
        technology_id=50,
        state="dispatched",
    )
    session = _Session({30: researcher, 44: refuter}, field)

    result = await Toolbox(session, None).call(
        "rename_topic",
        {
            "new_focus": "SSD as inference cache tier",
            "reason": "Retrieved evidence points to a cache-tier transition.",
            "verdict_on_previous": "Mainstream, strong signal.",
        },
        44,
    )

    assert result["renamed"] is True
    assert field.focus == "SSD as inference cache tier"
    history = next(row for row in session.added if isinstance(row, FieldRename))
    assert history.agent_id == 44


def test_report_entry_renders_original_name_rename_chain_and_previous_verdict():
    entry = Entry(
        id=101,
        rank=1,
        name_ru="SSD as KV-cache tier for inference",
        topic_original_focus="SSD",
        topic_rename_history=[
            {
                "previous_focus": "SSD",
                "new_focus": "SSD as KV-cache tier for inference",
                "reason": "Evidence follows inference cache deployments.",
                "verdict_on_previous": "Mainstream, strong signal.",
            }
        ],
        transition_ru="Кэширование выводов в SSD.",
        score=0.8,
        substance=0.8,
        momentum=0.8,
        faintness=0.7,
        state="banked",
        why_ru="Проверяемое обоснование.",
        searches_run=1,
        sources_checked=1,
    )
    response = SearchResponse(
        run_id=9,
        state="finished",
        query="SSD",
        generated_at=datetime.now(UTC),
        stats=SearchStats(
            candidates_found=1,
            sources_processed=0,
            high_confidence_count=0,
            agents_spawned=1,
            citations_verified=0,
            citations_total=0,
            elapsed_seconds=1,
        ),
        entries=[entry],
        models_used={},
    )

    text = render(response, top=15)

    assert "Исходная тема: SSD" in text
    assert "Переименование: SSD → SSD as KV-cache tier for inference" in text
    assert "Вердикт по прежнему названию: Mainstream, strong signal." in text
