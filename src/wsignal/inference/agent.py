from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from wsignal.config import get_settings
from wsignal.inference.events import Sink, emit
from wsignal.inference.llm import LlmClient
from wsignal.inference.operator_messages import deliver_messages
from wsignal.inference.prompts import prompt_hash
from wsignal.inference.tools import Toolbox, render_tool_result
from wsignal.models import Agent, Run, Turn


class AgentFailed(RuntimeError):
    def __init__(self, message: str, reason: str | None = None) -> None:
        self.reason = reason or message
        super().__init__(message)


@dataclass(slots=True)
class RunCostBudget:
    limit_usd: float | None
    spent_usd: float = 0.0

    @property
    def reason(self) -> str | None:
        if self.limit_usd is None or self.spent_usd < self.limit_usd:
            return None
        return (
            f"run cost limit reached: spent ${self.spent_usd:.6f} "
            f">= limit ${self.limit_usd:.6f}"
        )

    def record(self, cost_usd: float | None) -> None:
        if cost_usd is not None:
            self.spent_usd += float(cost_usd)


async def model_calls_for(session: AsyncSession, agent_id: int) -> int:
    turns = (
        await session.scalars(
            select(Turn).where(Turn.agent_id == agent_id, Turn.kind == "assistant")
        )
    ).all()
    return sum(
        1
        for turn in turns
        if isinstance(turn.content, dict)
        and (
            isinstance(turn.content.get("message"), dict)
            or (turn.content.get("error") and getattr(turn, "attempts", None) is not None)
        )
    )


def _tool_result_signature(payload: object) -> str:
    if isinstance(payload, dict):
        payload = dict(payload)
        status = payload.get("_status")
        if isinstance(status, dict):
            status = {
                key: value
                for key, value in status.items()
                if key not in {"elapsed_s", "remaining_s", "time", "model_calls", "http_requests"}
            }
            payload["_status"] = status
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _summarise(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    if "error" in payload:
        return f"error: {str(payload['error'])[:70]}"
    if "count" in payload:
        return f"{payload['count']} записей"
    for key in ("fields_written", "entry_id", "technology_id", "citations_stored"):
        if key in payload:
            return f"{key}={payload[key]}"
    if "returns" in payload:
        return f"{len(payload['returns'])} возвратов"
    if "content" in payload:
        return f"{len(str(payload['content']))} символов"
    return ""


@dataclass(slots=True)
class AgentResult:
    agent_id: int
    content: str | None
    steps: int
    stopped: str
    tool_calls: int
    submission: dict | None = None


class AgentRuntime:
    def __init__(
        self,
        session: AsyncSession,
        llm: LlmClient,
        toolbox: Toolbox,
        max_steps: int = 25,
        sink: Sink | None = None,
        repeat_tool_call_limit: int | None = None,
        repeat_tool_call_stop_after: int | None = None,
        cost_budget: RunCostBudget | None = None,
    ) -> None:
        self.operator_agent_id: int | None = None
        self._session = session
        self._llm = llm
        self._toolbox = toolbox
        settings = get_settings()
        self._max_steps = max_steps
        self._sink = sink
        self._cost_budget = cost_budget
        self._repeat_tool_call_limit = max(
            1,
            settings.repeat_tool_call_limit
            if repeat_tool_call_limit is None
            else repeat_tool_call_limit,
        )
        self._repeat_tool_call_stop_after = max(
            1,
            settings.repeat_tool_call_stop_after
            if repeat_tool_call_stop_after is None
            else repeat_tool_call_stop_after,
        )

    async def create_run(
        self, query: str, max_research: int, top_n: int, shard_size: int | None = None
    ) -> Run:
        run = Run(
            query=query,
            max_research=max_research,
            top_n=top_n,
            shard_size=shard_size,
            state="running",
        )
        self._session.add(run)
        await self._session.flush()
        return run

    async def create(
        self,
        run_id: int,
        role: str,
        system: str,
        brief: str | None = None,
        field: str | None = None,
        parent_agent_id: int | None = None,
        commit_system: bool = True,
    ) -> Agent:
        agent = Agent(
            run_id=run_id,
            parent_agent_id=parent_agent_id,
            role=role,
            model=self._llm.model_for(role),
            field=field,
            brief=brief,
            state="running",
        )
        self._session.add(agent)
        await self._session.flush()
        system_content = {
            "content": system,
            "prompt_sha256": prompt_hash(system),
            "model": agent.model,
        }
        if commit_system:
            await self._append(agent.id, "system", system_content)
        else:
            self._session.add(
                Turn(agent_id=agent.id, seq=1, kind="system", content=system_content)
            )
            await self._session.flush()
        emit(
            self._sink,
            "agent_created",
            agent_id=agent.id,
            role=role,
            model=agent.model,
            field=field,
        )
        return agent

    async def run(
        self,
        agent: Agent,
        message: str,
        max_steps: int | None = None,
        partial_on_failure: bool = False,
    ) -> AgentResult:
        return await self._start(agent, message, max_steps, partial_on_failure)

    async def _start(
        self,
        agent: Agent,
        message: str,
        max_steps: int | None,
        partial_on_failure: bool,
    ) -> AgentResult:
        await self._repair_dangling_tool_calls(agent.id)
        await self._append_current_date(agent.id)
        await self._append(agent.id, "user", {"content": message})
        if agent.id == self.operator_agent_id:
            await deliver_messages(self._session, agent.run_id, agent.id)
        return await self._loop(agent, max_steps or self._max_steps, partial_on_failure)

    async def _append_current_date(self, agent_id: int) -> None:
        last = await self._session.scalar(
            select(Turn)
            .where(Turn.agent_id == agent_id)
            .order_by(Turn.seq.desc())
            .limit(1)
        )
        today = datetime.now(UTC).date().isoformat()
        content = (
            f"Current date: {today} (UTC). Treat anything you remember as \"this year\" "
            "or \"recent\" as older than this date; put this year into dated queries."
        )
        if last is not None:
            last_content = last.content if isinstance(last.content, dict) else {}
            if last.kind == "system" and last_content.get("content") == content:
                return
        await self._append(agent_id, "system", {"content": content})

    async def resume(
        self,
        agent_id: int,
        message: str,
        max_steps: int | None = None,
        partial_on_failure: bool = False,
    ) -> AgentResult:
        agent = await self._session.get(Agent, agent_id)
        if agent is None:
            raise AgentFailed(f"no agent {agent_id}")
        agent.state = "running"
        return await self._start(agent, message, max_steps, partial_on_failure)

    async def recover_session(self, agent_id: int, exc: Exception) -> int:
        await self._session.reset()
        return await self._repair_dangling_tool_calls(agent_id, exc)

    async def _repair_dangling_tool_calls(
        self, agent_id: int, exc: Exception | None = None
    ) -> int:
        turns = (
            await self._session.scalars(
                select(Turn).where(Turn.agent_id == agent_id).order_by(Turn.seq)
            )
        ).all()
        answered = {
            str(turn.content.get("id"))
            for turn in turns
            if turn.kind == "tool_result"
            and isinstance(turn.content, dict)
            and turn.content.get("id")
        }
        latest_calls: list[dict] = []
        for turn in reversed(turns):
            content = turn.content if isinstance(turn.content, dict) else {}
            message = content.get("message")
            calls = message.get("tool_calls") if isinstance(message, dict) else None
            if turn.kind == "assistant" and isinstance(calls, list) and calls:
                latest_calls = [call for call in calls if isinstance(call, dict)]
                break

        repaired = 0
        for call in latest_calls:
            call_id = str(call.get("id") or "")
            if not call_id or call_id in answered:
                continue
            function = call.get("function") if isinstance(call.get("function"), dict) else {}
            name = str(function.get("name") or "unknown")
            if exc is None:
                payload = {
                    "error": "tool call was interrupted before it returned; no result was produced",
                    "recovery": "agent resumed after interruption",
                    "retryable": True,
                    "instruction": "Retry this tool call; no tool result was produced.",
                }
            else:
                payload = {
                    "error": f"infrastructure interruption: {type(exc).__name__}: {exc}"[:1000],
                    "recovery": "database session reset by supervisor",
                    "retryable": True,
                    "instruction": "Retry this tool call; do not interpret it as a tool result.",
                }
            await self._append(
                agent_id,
                "tool_result",
                {"id": call_id, "name": name, "payload": payload},
            )
            repaired += 1
        return repaired

    async def _loop(
        self, agent: Agent, max_steps: int, partial_on_failure: bool = False
    ) -> AgentResult:
        steps = 0
        tool_calls = 0
        last_content: str | None = None
        agent_id = agent.id
        role = agent.role
        last_signature: str | None = None
        last_result: str | None = None
        identical_result_count = 0
        blocked_repeat_count = 0
        last_payload: object = None
        initial_calls = (
            await model_calls_for(self._session, agent_id) if role == "orchestrator" else 0
        )
        remaining_calls = (
            max_steps - initial_calls if role == "orchestrator" else max_steps
        )
        if role == "orchestrator" and remaining_calls <= 0:
            reason = f"orchestrator model-call budget exhausted ({max_steps} calls)"
            agent.state = "failed"
            agent.returned_at = datetime.now(UTC)
            await self._append(agent_id, "assistant", {"error": reason, "reason": reason})
            emit(self._sink, "orchestrator_stopped", agent_id=agent_id, reason=reason)
            return AgentResult(agent_id, reason, 0, "call_budget_exhausted", tool_calls)
        while steps < remaining_calls:
            if self._cost_budget is not None and self._cost_budget.reason is not None:
                return await self._stop_for_cost_limit(agent, agent_id, role, steps, tool_calls)
            steps += 1
            try:
                agent.last_called_at = datetime.now(UTC)
                if agent_id == self.operator_agent_id:
                    await deliver_messages(self._session, agent.run_id, agent_id)
                messages = await self._replay(agent_id)
                if self._cost_budget is not None and self._cost_budget.reason is not None:
                    return await self._stop_for_cost_limit(
                        agent, agent_id, role, steps - 1, tool_calls
                    )
                emit(
                    self._sink,
                    "model_call_started",
                    agent_id=agent_id,
                    role=role,
                    step=steps,
                    messages=len(messages),
                )
                model = self._llm.model_for(role)
                reply = await self._llm.converse(
                    role,
                    messages,
                    self._toolbox.schemas_for(role),
                    session_id=f"agent-{agent_id}",
                )
                agent.model = model

                await self._append(
                    agent_id,
                    "assistant",
                    {
                        "message": reply.message,
                        "model": getattr(reply, "resolved_model", None) or model,
                    },
                    **reply.accounting(),
                )
                if self._cost_budget is not None:
                    self._cost_budget.record(reply.cost_usd)
                    await self._session.commit()
                if reply.content:
                    last_content = reply.content
                emit(
                    self._sink,
                    "model_call",
                    agent_id=agent_id,
                    role=role,
                    step=steps,
                    duration_s=round(reply.duration_ms / 1000, 1),
                    cost_usd=reply.cost_usd,
                    prompt_tokens=reply.prompt_tokens,
                    cached_tokens=reply.cached_tokens,
                    tool_calls=[c.name for c in reply.tool_calls],
                )
                if self._cost_budget is not None and self._cost_budget.reason is not None:
                    reason = self._cost_budget.reason
                    for call in reply.tool_calls:
                        await self._append(
                            agent_id,
                            "tool_call",
                            {"id": call.id, "name": call.name, "arguments": call.arguments},
                        )
                        await self._append(
                            agent_id,
                            "tool_result",
                            {"id": call.id, "name": call.name, "payload": {"error": reason}},
                        )
                    return await self._stop_for_cost_limit(agent, agent_id, role, steps, tool_calls)

                if not reply.wants_tools:
                    agent.state = "idle"
                    agent.returned_at = datetime.now(UTC)
                    await self._session.commit()
                    return AgentResult(agent_id, reply.content, steps, "answered", tool_calls)

                for call in reply.tool_calls:
                    tool_calls += 1
                    await self._append(
                        agent_id,
                        "tool_call",
                        {"id": call.id, "name": call.name, "arguments": call.arguments},
                    )
                    signature = json.dumps(
                        [call.name, call.arguments],
                        sort_keys=True,
                        separators=(",", ":"),
                        default=str,
                    )
                    same_call = signature == last_signature
                    if same_call and identical_result_count >= self._repeat_tool_call_limit:
                        blocked_repeat_count += 1
                        reason = (
                            f"repeat guard: {call.name} with identical arguments was blocked "
                            f"after {identical_result_count} identical results"
                        )
                        status = {"agent_id": agent_id, "role": role, "state": agent.state}
                        previous_status = (
                            last_payload.get("_status")
                            if isinstance(last_payload, dict)
                            else None
                        )
                        if isinstance(previous_status, dict):
                            status.update(previous_status)
                        payload = {
                            "error": (
                                f"You are repeating {call.name} with the same arguments and "
                                "getting no new result. Do not repeat this call; change the "
                                "action based on the current state or finish."
                            ),
                            "agent_state": status,
                            "identical_results": identical_result_count,
                            "blocked_repeats": blocked_repeat_count,
                        }
                        duration_ms = 0
                        if blocked_repeat_count >= self._repeat_tool_call_stop_after:
                            reason = (
                                f"repeat guard stopped agent after {blocked_repeat_count} "
                                f"blocked repetitions of {call.name} with identical arguments"
                            )
                            payload["error"] = reason
                            payload["stopped"] = True
                    else:
                        blocked_repeat_count = 0
                        started = time.monotonic()
                        payload = await self._toolbox.call(call.name, call.arguments, agent_id)
                        duration_ms = round((time.monotonic() - started) * 1000)
                        result_signature = _tool_result_signature(payload)
                        identical_result_count = (
                            identical_result_count + 1
                            if same_call and result_signature == last_result
                            else 1
                        )
                        last_signature = signature
                        last_result = result_signature
                        last_payload = payload
                    await self._append(
                        agent_id,
                        "tool_result",
                        {"id": call.id, "name": call.name, "payload": payload},
                        duration_ms=duration_ms,
                    )
                    emit(
                        self._sink,
                        "tool_done",
                        agent_id=agent_id,
                        role=role,
                        name=call.name,
                        arguments=call.arguments,
                        duration_s=round(duration_ms / 1000, 1),
                        summary=_summarise(payload),
                    )
                    if isinstance(payload, dict) and payload.get("submitted") is True:
                        agent.state = "idle"
                        agent.returned_at = datetime.now(UTC)
                        await self._session.commit()
                        return AgentResult(
                            agent_id, last_content, steps, "submitted", tool_calls,
                            payload["submission"],
                        )
                    if isinstance(payload, dict) and payload.get("run_ended") is True:
                        agent.state = "idle"
                        agent.returned_at = datetime.now(UTC)
                        await self._session.commit()
                        return AgentResult(agent_id, last_content, steps, "run_ended", tool_calls)
                    if (
                        role == "orchestrator"
                        and isinstance(payload, dict)
                        and payload.get("orchestrator_finished") is True
                    ):
                        agent.state = "done"
                        agent.returned_at = datetime.now(UTC)
                        await self._session.commit()
                        emit(
                            self._sink,
                            "orchestrator_stopped",
                            agent_id=agent_id,
                            reason=str(payload.get("note") or ""),
                        )
                        return AgentResult(
                            agent_id, last_content or str(payload.get("note") or ""),
                            steps, "answered", tool_calls,
                        )
                    if isinstance(payload, dict) and payload.get("stopped") is True:
                        failure = AgentFailed(reason, reason=reason)
                        stopped = await self._record_failure(agent, agent_id, role, failure)
                        return AgentResult(agent_id, last_content, steps, stopped, tool_calls)
            except BaseException as exc:
                stopped = await self._record_failure(agent, agent_id, role, exc)
                if not isinstance(exc, Exception):
                    raise
                if partial_on_failure:
                    return AgentResult(agent_id, last_content, steps, stopped, tool_calls)
                raise AgentFailed(f"agent {agent_id} ({role}): {exc}") from exc

        try:
            agent.state = "idle"
            agent.returned_at = datetime.now(UTC)
            await self._session.commit()
        except Exception:
            pass
        if role == "orchestrator" and steps >= remaining_calls:
            reason = f"orchestrator model-call budget exhausted ({max_steps} calls)"
            agent.state = "failed"
            agent.returned_at = datetime.now(UTC)
            await self._append(agent_id, "assistant", {"error": reason, "reason": reason})
            emit(self._sink, "orchestrator_stopped", agent_id=agent_id, reason=reason)
            return AgentResult(
                agent_id, reason, steps, "call_budget_exhausted", tool_calls
            )
        return AgentResult(agent_id, last_content, steps, "max_steps", tool_calls)

    async def _stop_for_cost_limit(
        self, agent: Agent, agent_id: int, role: str, steps: int, tool_calls: int
    ) -> AgentResult:
        reason = self._cost_budget.reason if self._cost_budget is not None else None
        reason = reason or "run cost limit reached"
        agent.state = "failed"
        agent.returned_at = datetime.now(UTC)
        await self._append(agent_id, "user", {"content": reason})
        await self._append(agent_id, "assistant", {"error": reason, "reason": reason})
        emit(self._sink, "cost_limit_reached", agent_id=agent_id, role=role, reason=reason)
        return AgentResult(agent_id, reason, steps, "cost_limit_reached", tool_calls)

    async def _record_failure(
        self, agent: Agent, agent_id: int, role: str, exc: BaseException
    ) -> str:
        reason = str(getattr(exc, "reason", "") or type(exc).__name__)
        elapsed_s = getattr(exc, "elapsed_s", None)
        try:
            await self._session.rollback()
        except Exception:
            pass
        try:
            agent.state = "failed"
        except Exception:
            pass
        try:
            await asyncio.shield(
                self._append(
                    agent_id,
                    "assistant",
                    {"error": f"{type(exc).__name__}: {exc}"[:4000], "reason": reason},
                    attempts=getattr(exc, "attempts", None),
                    duration_ms=round(elapsed_s * 1000) if elapsed_s else None,
                )
            )
        except BaseException:
            pass
        emit(
            self._sink,
            "agent_failed",
            agent_id=agent_id,
            role=role,
            reason=reason,
            attempts=getattr(exc, "attempts", None),
            elapsed_s=round(elapsed_s, 1) if elapsed_s else None,
            error=str(exc)[:200],
        )
        return f"failed:{reason}"

    async def _replay(self, agent_id: int) -> list[dict]:
        turns = (
            await self._session.scalars(
                select(Turn).where(Turn.agent_id == agent_id).order_by(Turn.seq)
            )
        ).all()

        results = {
            str(turn.content.get("id")): turn.content
            for turn in turns
            if turn.kind == "tool_result"
            and isinstance(turn.content, dict)
            and turn.content.get("id")
        }
        call_arguments = {
            str(turn.content.get("id")): turn.content.get("arguments")
            for turn in turns
            if turn.kind == "tool_call"
            and isinstance(turn.content, dict)
            and turn.content.get("id")
        }
        rendered_results: set[str] = set()
        messages: list[dict] = []
        for turn in turns:
            content = turn.content if isinstance(turn.content, dict) else {}
            if turn.kind == "system":
                messages.append({"role": "system", "content": content.get("content", "")})
            elif turn.kind == "user":
                messages.append({"role": "user", "content": content.get("content", "")})
            elif turn.kind == "assistant":
                message = content.get("message")
                if isinstance(message, dict):
                    messages.append(message)
                    calls = message.get("tool_calls")
                    if isinstance(calls, list):
                        for call in calls:
                            call_id = str(call.get("id") or "") if isinstance(call, dict) else ""
                            result = results.get(call_id)
                            if result is None:
                                continue
                            messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": call_id,
                                    "content": render_tool_result(
                                        result.get("payload"),
                                        result.get("name"),
                                        call_arguments.get(call_id),
                                    ),
                                }
                            )
                            rendered_results.add(call_id)
            elif turn.kind == "tool_result":
                call_id = str(content.get("id") or "")
                if call_id in rendered_results:
                    continue
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": content.get("id", ""),
                        "content": render_tool_result(
                            content.get("payload"),
                            content.get("name"),
                            call_arguments.get(call_id),
                        ),
                    }
                )
        return messages

    async def _append(self, agent_id: int, kind: str, content: dict, **accounting) -> Turn:
        for attempt in (1, 2):
            try:
                next_seq = (
                    await self._session.scalar(
                        select(func.coalesce(func.max(Turn.seq), 0) + 1).where(
                            Turn.agent_id == agent_id
                        )
                    )
                ) or 1
                turn = Turn(
                    agent_id=agent_id, seq=next_seq, kind=kind, content=content, **accounting
                )
                self._session.add(turn)
                await self._session.commit()
                return turn
            except Exception:
                if attempt == 2:
                    raise
                try:
                    await self._session.rollback()
                except Exception:
                    pass
        raise RuntimeError("unreachable")
