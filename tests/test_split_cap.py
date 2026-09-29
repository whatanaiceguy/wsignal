import json
from types import SimpleNamespace

import pytest

from wsignal.config import Settings
from wsignal.inference.agent import AgentResult
from wsignal.inference.orchestration import Orchestration


def _fields(count):
    return json.dumps({"fields": [
        {"area": "a", "focus": f"field {index}", "rationale": "r", "technology_id": index}
        for index in range(1, count + 1)
    ]})


def _orchestration(monkeypatch, answers, cap=3):
    settings = Settings(_env_file=None, split_max_fields=cap)
    monkeypatch.setattr("wsignal.inference.orchestration.get_settings", lambda: settings)
    messages = []
    queue = list(answers)

    async def create(**_kwargs):
        return SimpleNamespace(id=7)

    async def run(_agent, message, **_kwargs):
        messages.append(message)
        return AgentResult(7, queue.pop(0), 1, "answered", 1, None)

    async def resume(_agent_id, message, **_kwargs):
        messages.append(message)
        return AgentResult(7, queue.pop(0), 1, "answered", 1, None)

    async def problems(_items):
        return {"unresolved": [], "shared": [], "near": []}

    async def write(items):
        return [
            {"field_id": index, "focus": item["focus"], "technology_id": item["technology_id"]}
            for index, item in enumerate(items, start=1)
        ]

    orchestration = object.__new__(Orchestration)
    orchestration._run = SimpleNamespace(id=9, query="медицина", seeded=False)
    orchestration._prompts = {"assistant": "split"}
    orchestration._runtime = SimpleNamespace(create=create, run=run, resume=resume)
    orchestration._agent_max_steps = 5
    orchestration._sink = None
    orchestration._field_problems = problems
    orchestration._write_fields = write
    return orchestration, messages


@pytest.mark.asyncio
async def test_the_cap_reaches_the_assistant_whatever_the_instruction_says(monkeypatch):
    orchestration, messages = _orchestration(monkeypatch, [_fields(2)])

    result = await orchestration._split_direction({"instruction": "aim for 40+"}, 1)

    assert "aim for 40+" in messages[0]
    assert "At most 3 fields in total" in messages[0]
    assert result["fields_written"] == 2
    assert "fields_over_cap_dropped" not in result


@pytest.mark.asyncio
async def test_an_over_cap_split_is_trimmed_by_the_assistant_once(monkeypatch):
    orchestration, messages = _orchestration(monkeypatch, [_fields(5), _fields(3)])

    result = await orchestration._split_direction({}, 1)

    assert len(messages) == 2
    assert "You sent 5 fields; the limit is 3" in messages[1]
    assert result["fields_written"] == 3
    assert "fields_over_cap_dropped" not in result


@pytest.mark.asyncio
async def test_a_split_still_over_cap_keeps_the_first_fields_and_says_so(monkeypatch):
    orchestration, _messages = _orchestration(monkeypatch, [_fields(5), _fields(4)])

    result = await orchestration._split_direction({}, 1)

    assert result["fields_written"] == 3
    assert result["fields_over_cap_dropped"] == 1
    assert [field["focus"] for field in result["fields"]] == ["field 1", "field 2", "field 3"]


def test_the_split_cap_defaults_to_thirty():
    assert Settings(_env_file=None).split_max_fields == 30
