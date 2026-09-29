from __future__ import annotations

from sqlalchemy import Float, Integer, Text, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from wsignal.models import Agent, SourceEvent, Turn


def _rate(cached: int, prompt: int) -> float:
    return round(100.0 * cached / prompt, 1) if prompt else 0.0


async def per_agent(session: AsyncSession, run_id: int) -> list[dict]:
    rows = (
        await session.execute(
            select(
                Agent.id,
                Agent.role,
                Agent.model,
                Agent.state,
                Agent.field,
                func.count(Turn.id).filter(Turn.kind == "assistant").label("model_calls"),
                func.count(Turn.id).filter(Turn.kind == "tool_call").label("tool_calls"),
                func.coalesce(func.sum(Turn.prompt_tokens), 0).label("prompt_tokens"),
                func.coalesce(func.sum(Turn.completion_tokens), 0).label("completion_tokens"),
                func.coalesce(func.sum(Turn.cached_tokens), 0).label("cached_tokens"),
                func.coalesce(func.sum(Turn.cache_write_tokens), 0).label("cache_writes"),
                func.coalesce(func.sum(Turn.reasoning_tokens), 0).label("reasoning_tokens"),
                func.coalesce(func.sum(cast(Turn.cost_usd, Float)), 0.0).label("cost_usd"),
                func.coalesce(
                    func.sum(cast(Turn.duration_ms, Integer)).filter(Turn.kind == "assistant"), 0
                ).label("model_ms"),
                func.coalesce(
                    func.sum(cast(Turn.duration_ms, Integer)).filter(Turn.kind == "tool_result"), 0
                ).label("tool_ms"),
            )
            .join(Turn, Turn.agent_id == Agent.id, isouter=True)
            .where(Agent.run_id == run_id)
            .group_by(Agent.id, Agent.role, Agent.model, Agent.state, Agent.field)
            .order_by(Agent.id)
        )
    ).all()

    return [
        {
            "agent_id": r.id,
            "role": r.role,
            "model": r.model,
            "state": r.state,
            "field": r.field,
            "model_calls": r.model_calls,
            "tool_calls": r.tool_calls,
            "prompt_tokens": r.prompt_tokens,
            "completion_tokens": r.completion_tokens,
            "cached_tokens": r.cached_tokens,
            "cache_writes": r.cache_writes,
            "reasoning_tokens": r.reasoning_tokens,
            "cache_hit_pct": _rate(r.cached_tokens, r.prompt_tokens),
            "cost_usd": round(float(r.cost_usd), 6),
            "model_s": round(r.model_ms / 1000, 1),
            "tool_s": round(r.tool_ms / 1000, 1),
        }
        for r in rows
    ]


async def per_run(session: AsyncSession, run_id: int) -> dict:
    agents = await per_agent(session, run_id)
    total_prompt = sum(a["prompt_tokens"] for a in agents)
    total_cached = sum(a["cached_tokens"] for a in agents)
    return {
        "agents": len(agents),
        "model_calls": sum(a["model_calls"] for a in agents),
        "tool_calls": sum(a["tool_calls"] for a in agents),
        "prompt_tokens": total_prompt,
        "completion_tokens": sum(a["completion_tokens"] for a in agents),
        "cached_tokens": total_cached,
        "cache_writes": sum(a["cache_writes"] for a in agents),
        "uncached_tokens": max(0, total_prompt - total_cached),
        "reasoning_tokens": sum(a["reasoning_tokens"] for a in agents),
        "cache_hit_pct": _rate(total_cached, total_prompt),
        "cost_usd": round(sum(a["cost_usd"] for a in agents), 6),
        "model_s": round(sum(a["model_s"] for a in agents), 1),
        "tool_s": round(sum(a["tool_s"] for a in agents), 1),
        "by_role": _by_role(agents),
    }


def _by_role(agents: list[dict]) -> dict[str, dict]:
    rolled: dict[str, dict] = {}
    for agent in agents:
        bucket = rolled.setdefault(
            agent["role"],
            {
                "agents": 0,
                "model_calls": 0,
                "prompt_tokens": 0,
                "cached_tokens": 0,
                "cost_usd": 0.0,
            },
        )
        bucket["agents"] += 1
        bucket["model_calls"] += agent["model_calls"]
        bucket["prompt_tokens"] += agent["prompt_tokens"]
        bucket["cached_tokens"] += agent["cached_tokens"]
        bucket["cost_usd"] = round(bucket["cost_usd"] + agent["cost_usd"], 6)
    for bucket in rolled.values():
        bucket["cache_hit_pct"] = _rate(bucket["cached_tokens"], bucket["prompt_tokens"])
    return rolled


async def per_source(session: AsyncSession, run_id: int) -> list[dict]:
    empty = func.coalesce(SourceEvent.items, 0) == 0
    rows = (
        await session.execute(
            select(
                SourceEvent.adapter,
                func.count().label("calls"),
                func.count().filter(SourceEvent.ok).label("ok"),
                func.count().filter(SourceEvent.ok, empty).label("empty"),
                func.count().filter(SourceEvent.cached).label("cached"),
                func.coalesce(func.sum(SourceEvent.items), 0).label("items"),
                func.coalesce(func.sum(SourceEvent.bytes), 0).label("bytes"),
                func.coalesce(func.sum(SourceEvent.requests), 0).label("requests"),
                func.coalesce(func.avg(SourceEvent.duration_ms), 0).label("avg_ms"),
            )
            .join(Agent, Agent.id == SourceEvent.agent_id)
            .where(Agent.run_id == run_id)
            .group_by(SourceEvent.adapter)
            .order_by(func.count().desc())
        )
    ).all()
    return [
        {
            "adapter": r.adapter,
            "calls": r.calls,
            "ok": r.ok,
            "failed": r.calls - r.ok,
            "empty": r.empty,
            "cached": r.cached,
            "items": r.items,
            "bytes": r.bytes,
            "requests": r.requests,
            "avg_s": round(float(r.avg_ms) / 1000, 1),
        }
        for r in rows
    ]


async def context_sources(session: AsyncSession, run_id: int, limit: int = 8) -> list[dict]:
    rows = (
        await session.execute(
            select(
                Turn.agent_id,
                Turn.seq,
                Agent.role,
                Turn.content["name"].astext.label("tool"),
                func.length(cast(Turn.content, Text)).label("chars"),
            )
            .join(Agent, Agent.id == Turn.agent_id)
            .where(Agent.run_id == run_id, Turn.kind == "tool_result")
            .order_by(func.length(cast(Turn.content, Text)).desc())
            .limit(limit)
        )
    ).all()
    return [
        {
            "agent_id": r.agent_id,
            "seq": r.seq,
            "role": r.role,
            "tool": r.tool,
            "chars": r.chars,
        }
        for r in rows
    ]
