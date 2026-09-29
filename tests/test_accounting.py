import pytest

from wsignal.inference import accounting


@pytest.mark.asyncio
async def test_per_run_sums_agent_usage_and_rolls_agents_up_by_role(monkeypatch):
    agents = [
        {
            "agent_id": 1,
            "role": "researcher",
            "model": "model-a",
            "model_calls": 2,
            "tool_calls": 3,
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "cached_tokens": 40,
            "cache_writes": 5,
            "reasoning_tokens": 7,
            "cost_usd": 0.1,
            "model_s": 1.2,
            "tool_s": 0.5,
        },
        {
            "agent_id": 2,
            "role": "researcher",
            "model": "model-b",
            "model_calls": 1,
            "tool_calls": 2,
            "prompt_tokens": 50,
            "completion_tokens": 10,
            "cached_tokens": 10,
            "cache_writes": 2,
            "reasoning_tokens": 3,
            "cost_usd": 0.2,
            "model_s": 0.8,
            "tool_s": 0.4,
        },
        {
            "agent_id": 3,
            "role": "refuter",
            "model": "model-c",
            "model_calls": 1,
            "tool_calls": 0,
            "prompt_tokens": 25,
            "completion_tokens": 5,
            "cached_tokens": 0,
            "cache_writes": 0,
            "reasoning_tokens": 1,
            "cost_usd": 0.05,
            "model_s": 0.3,
            "tool_s": 0.0,
        },
    ]

    async def fake_per_agent(_session, run_id):
        assert run_id == 12
        return agents

    monkeypatch.setattr(accounting, "per_agent", fake_per_agent)

    totals = await accounting.per_run(object(), 12)

    assert totals["agents"] == 3
    assert totals["model_calls"] == 4
    assert totals["tool_calls"] == 5
    assert totals["prompt_tokens"] == 175
    assert totals["completion_tokens"] == 35
    assert totals["cached_tokens"] == 50
    assert totals["cache_writes"] == 7
    assert totals["uncached_tokens"] == 125
    assert totals["reasoning_tokens"] == 11
    assert totals["cost_usd"] == 0.35
    assert totals["model_s"] == 2.3
    assert totals["tool_s"] == 0.9
    assert totals["by_role"] == {
        "researcher": {
            "agents": 2,
            "model_calls": 3,
            "prompt_tokens": 150,
            "cached_tokens": 50,
            "cost_usd": 0.3,
            "cache_hit_pct": 33.3,
        },
        "refuter": {
            "agents": 1,
            "model_calls": 1,
            "prompt_tokens": 25,
            "cached_tokens": 0,
            "cost_usd": 0.05,
            "cache_hit_pct": 0.0,
        },
    }
