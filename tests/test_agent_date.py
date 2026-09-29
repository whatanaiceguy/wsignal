import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from wsignal.config import Settings
from wsignal.inference.agent import AgentRuntime
from wsignal.inference.llm import LlmClient


class _Rows:
    def __init__(self, turns):
        self._turns = turns

    def all(self):
        return sorted(self._turns, key=lambda turn: turn.seq)


class _Session:
    def __init__(self, turns):
        self.turns = turns
        self.agent = SimpleNamespace(id=1, role="researcher", state="idle")

    async def scalars(self, _statement):
        return _Rows(self.turns)

    async def scalar(self, statement):
        if "max(" in str(statement).lower():
            return max((turn.seq for turn in self.turns), default=0)
        return max(self.turns, key=lambda turn: turn.seq, default=None)

    def add(self, turn):
        turn.seq = max((item.seq for item in self.turns), default=0) + 1
        self.turns.append(turn)

    async def commit(self):
        pass

    async def rollback(self):
        pass

    async def get(self, _model, _identity):
        return self.agent


class _Llm:
    def __init__(self):
        self.messages = []
        self.model = "test-model"

    def model_for(self, _role):
        return self.model

    async def converse(self, _role, messages, _tools, session_id=None):
        self.messages.append(messages)
        return SimpleNamespace(
            message={"role": "assistant", "content": "answer"},
            content="answer",
            duration_ms=0,
            cost_usd=None,
            prompt_tokens=0,
            cached_tokens=0,
            tool_calls=[],
            wants_tools=False,
            accounting=lambda: {},
        )


class _Toolbox:
    def schemas_for(self, _role):
        return []


@pytest.mark.asyncio
@pytest.mark.parametrize("resumed", [False, True])
async def test_agent_request_adds_date_after_original_prompt(resumed):
    turns = [
        SimpleNamespace(
            seq=1,
            kind="system",
            content={"content": "original system prompt", "prompt_sha256": "cached"},
        )
    ]
    if resumed:
        turns.append(
            SimpleNamespace(
                seq=2,
                kind="assistant",
                content={"message": {"role": "assistant", "content": "earlier"}},
            )
        )
    llm = _Llm()
    runtime = AgentRuntime(_Session(turns), llm, _Toolbox())

    if resumed:
        await runtime.resume(1, "continue")
        expected_input = {"role": "user", "content": "continue"}
    else:
        await runtime.run(SimpleNamespace(id=1, role="researcher"), "start")
        expected_input = {"role": "user", "content": "start"}

    messages = llm.messages[0]
    assert messages[0] == {"role": "system", "content": "original system prompt"}
    assert messages[-2]["role"] == "system"
    assert messages[-2]["content"].startswith(
        f"Current date: {datetime.now(UTC).date().isoformat()} (UTC)."
    )
    assert messages[-1] == expected_input
    assert turns[0].content["content"] == "original system prompt"


@pytest.mark.asyncio
async def test_provider_metadata_and_usage_are_stored_on_assistant_turn(monkeypatch):
    settings = Settings(
        llm_model_researcher="requested/model",
        llm_reasoning_effort_by_role={},
        llm_retry_base_delay_s=0,
    )
    monkeypatch.setattr("wsignal.inference.llm.get_settings", lambda: settings)
    response_payload = {
        "id": "generation-123",
        "provider": "provider-x",
        "model": "resolved/model",
        "choices": [{"message": {"role": "assistant", "content": "answer"}}],
        "usage": {
            "prompt_tokens": 17,
            "completion_tokens": 8,
            "prompt_tokens_details": {"cached_tokens": 4, "cache_write_tokens": 2},
            "completion_tokens_details": {"reasoning_tokens": 3},
            "cost": 0.0042,
        },
    }
    client = LlmClient(
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json=response_payload)
            )
        )
    )
    session = _Session([SimpleNamespace(seq=1, kind="system", content={"content": "prompt"})])
    runtime = AgentRuntime(session, client, _Toolbox())
    try:
        await runtime.run(session.agent, "start")
    finally:
        await client.aclose()

    turn = next(turn for turn in session.turns if turn.kind == "assistant")
    assert turn.content["model"] == "resolved/model"
    assert turn.provider == "provider-x"
    assert turn.generation_id == "generation-123"
    assert turn.prompt_tokens == 17
    assert turn.completion_tokens == 8
    assert turn.cached_tokens == 4
    assert turn.cache_write_tokens == 2
    assert turn.reasoning_tokens == 3
    assert turn.cost_usd == 0.0042
    assert turn.duration_ms >= 0
    assert turn.attempts == 1
    model_call = client.log.calls[-1]
    assert model_call.resolved_model == "resolved/model"
    assert model_call.provider == "provider-x"
    assert model_call.generation_id == "generation-123"
    assert model_call.cached_tokens == 4
    assert model_call.cache_write_tokens == 2
    assert model_call.reasoning_tokens == 3
    assert model_call.cost_usd == 0.0042
    assert model_call.duration_s >= 0
    assert model_call.attempts == 1


@pytest.mark.asyncio
async def test_agent_model_tracks_model_used_after_configuration_changes():
    turns = [SimpleNamespace(seq=1, kind="system", content={"content": "prompt"})]
    session = _Session(turns)
    session.agent.model = "model-a"
    llm = _Llm()
    llm.model = "model-b"
    runtime = AgentRuntime(session, llm, _Toolbox())

    await runtime.resume(1, "continue")

    assert session.agent.model == "model-b"


@pytest.mark.asyncio
async def test_resume_repairs_dangling_call_before_date_and_input_and_only_once():
    call_message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call-interrupted",
                "type": "function",
                "function": {"name": "search", "arguments": "{}"},
            }
        ],
    }
    turns = [
        SimpleNamespace(seq=1, kind="system", content={"content": "system prompt"}),
        SimpleNamespace(seq=2, kind="user", content={"content": "original request"}),
        SimpleNamespace(seq=3, kind="assistant", content={"message": call_message}),
        SimpleNamespace(
            seq=4,
            kind="tool_call",
            content={"id": "call-interrupted", "name": "search", "arguments": {}},
        ),
        SimpleNamespace(
            seq=5,
            kind="assistant",
            content={"error": "CancelledError"},
        ),
    ]
    session = _Session(turns)
    llm = _Llm()
    runtime = AgentRuntime(session, llm, _Toolbox())

    await runtime.resume(1, "resume request")

    messages = llm.messages[0]
    call_index = messages.index(call_message)
    tool_message = messages[call_index + 1]
    assert tool_message["role"] == "tool"
    assert tool_message["tool_call_id"] == "call-interrupted"
    assert "no result was produced" in tool_message["content"]
    assert messages[call_index + 2]["role"] == "system"
    assert messages[call_index + 2]["content"].startswith("Current date: ")
    assert messages[call_index + 3] == {"role": "user", "content": "resume request"}

    await runtime.resume(1, "resume again")

    assert sum(
        turn.kind == "tool_result"
        and turn.content.get("id") == "call-interrupted"
        for turn in turns
    ) == 1
    assert sum(
        message.get("role") == "tool"
        and message.get("tool_call_id") == "call-interrupted"
        for message in llm.messages[1]
    ) == 1


@pytest.mark.asyncio
async def test_immediate_date_stamp_reentry_does_not_duplicate_turn():
    session = _Session([
        SimpleNamespace(
            seq=1,
            kind="system",
            content={"content": "original system prompt"},
        )
    ])
    runtime = AgentRuntime(session, _Llm(), _Toolbox())

    await runtime._append_current_date(1)
    await runtime._append_current_date(1)

    assert sum(
        turn.kind == "system"
        and isinstance(turn.content, dict)
        and str(turn.content.get("content", "")).startswith("Current date: ")
        for turn in session.turns
    ) == 1


@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "last_content,expected",
    [
        ({"message": {"content": "answer", "tool_calls": []}}, True),
        ({"error": "CancelledError"}, False),
        ({"message": {"content": None, "tool_calls": [{"id": "c"}]}}, False),
    ],
)
async def test_fleet_completion_requires_a_final_assistant_answer(last_content, expected):
    from wsignal.inference.fleet import OrchestratorFleet

    turns = [
        SimpleNamespace(kind="system", content={"content": "Current date: today (UTC)."}),
        SimpleNamespace(kind="assistant", content=last_content),
    ]

    class Session:
        async def scalar(self, _statement):
            return turns[-1]

    fleet = object.__new__(OrchestratorFleet)
    fleet._session = Session()

    completed = await fleet._durably_completed(
        SimpleNamespace(id=1, state="idle"), []
    )

    assert completed is expected


@pytest.mark.asyncio
async def test_cancellation_records_assistant_error_turn_and_reraises():
    class CancelledLlm(_Llm):
        async def converse(self, *_args, **_kwargs):
            raise asyncio.CancelledError()

    turns = [SimpleNamespace(seq=1, kind="system", content={"content": "prompt"})]
    session = _Session(turns)
    runtime = AgentRuntime(session, CancelledLlm(), _Toolbox())
    agent = SimpleNamespace(id=1, role="researcher", state="idle")

    with pytest.raises(asyncio.CancelledError):
        await runtime.run(agent, "start")

    error_turns = [turn for turn in turns if turn.kind == "assistant" and "error" in turn.content]
    assert len(error_turns) == 1
    assert error_turns[0].content["error"] == "CancelledError: "
    assert error_turns[0].content["reason"] == "CancelledError"
    assert error_turns[0].seq == max(turn.seq for turn in turns)


@pytest.mark.asyncio
async def test_repeat_guard_blocks_identical_results_and_stops_with_a_recorded_reason():
    class Session:
        def __init__(self):
            self.turns = []
            self.agent = SimpleNamespace(id=1, role="orchestrator", state="running")

        async def scalars(self, _statement):
            return _Rows(self.turns)

        async def scalar(self, statement):
            if "max(" in str(statement).lower():
                return max((turn.seq for turn in self.turns), default=0)
            return max(self.turns, key=lambda turn: turn.seq, default=None)

        def add(self, turn):
            turn.seq = max((item.seq for item in self.turns), default=0) + 1
            self.turns.append(turn)

        async def commit(self):
            pass

        async def rollback(self):
            pass

        async def get(self, _model, _identity):
            return self.agent

    class RepeatingLlm(_Llm):
        async def converse(self, *_args, **_kwargs):
            self.calls = getattr(self, "calls", 0) + 1
            call = SimpleNamespace(id=f"call-{self.calls}", name="collect", arguments={})
            return SimpleNamespace(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": "{}"},
                    }],
                },
                content=None,
                duration_ms=0,
                cost_usd=None,
                prompt_tokens=0,
                cached_tokens=0,
                tool_calls=[call],
                wants_tools=True,
                accounting=lambda: {},
            )

    class RepeatingToolbox(_Toolbox):
        def __init__(self):
            self.calls = 0

        async def call(self, *_args):
            self.calls += 1
            return {"returns": [], "still_running": [], "note": "no work"}

    session = Session()
    llm = RepeatingLlm()
    toolbox = RepeatingToolbox()
    runtime = AgentRuntime(
        session,
        llm,
        toolbox,
        repeat_tool_call_limit=3,
        repeat_tool_call_stop_after=3,
    )

    result = await runtime.run(session.agent, "start", partial_on_failure=True)

    failure = next(
        turn for turn in reversed(session.turns)
        if turn.kind == "assistant" and "error" in turn.content
    )
    blocked = [
        turn for turn in session.turns
        if turn.kind == "tool_result" and turn.content.get("payload", {}).get("stopped")
    ]
    assert toolbox.calls == 3
    assert llm.calls == 6
    assert result.stopped.startswith("failed:repeat guard stopped agent")
    assert session.agent.state == "failed"
    assert "repeat guard stopped agent" in failure.content["reason"]
    assert len(blocked) == 1
    assert "repeat guard stopped agent" in blocked[0].content["payload"]["error"]


@pytest.mark.asyncio
async def test_repeat_guard_allows_same_call_when_its_result_changes():
    class Session:
        def __init__(self):
            self.turns = []
            self.agent = SimpleNamespace(id=1, role="orchestrator", state="running")

        async def scalars(self, _statement):
            return _Rows(self.turns)

        async def scalar(self, statement):
            if "max(" in str(statement).lower():
                return max((turn.seq for turn in self.turns), default=0)
            return max(self.turns, key=lambda turn: turn.seq, default=None)

        def add(self, turn):
            turn.seq = max((item.seq for item in self.turns), default=0) + 1
            self.turns.append(turn)

        async def commit(self):
            pass

        async def rollback(self):
            pass

        async def get(self, _model, _identity):
            return self.agent

    class ChangingLlm(_Llm):
        async def converse(self, *_args, **_kwargs):
            self.calls = getattr(self, "calls", 0) + 1
            if self.calls <= 3:
                call = SimpleNamespace(
                    id=f"call-{self.calls}", name="collect", arguments={}
                )
                return SimpleNamespace(
                    message={"role": "assistant", "content": None, "tool_calls": [{
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": "{}"},
                    }]},
                    content=None,
                    duration_ms=0,
                    cost_usd=None,
                    prompt_tokens=0,
                    cached_tokens=0,
                    tool_calls=[call],
                    wants_tools=True,
                    accounting=lambda: {},
                )
            return SimpleNamespace(
                message={"role": "assistant", "content": "done"},
                content="done",
                duration_ms=0,
                cost_usd=None,
                prompt_tokens=0,
                cached_tokens=0,
                tool_calls=[],
                wants_tools=False,
                accounting=lambda: {},
            )

    class ChangingToolbox(_Toolbox):
        def __init__(self):
            self.calls = 0

        async def call(self, *_args):
            self.calls += 1
            return {"returns": [{"field_id": self.calls}]}

    session = Session()
    llm = ChangingLlm()
    toolbox = ChangingToolbox()
    runtime = AgentRuntime(
        session, llm, toolbox, repeat_tool_call_limit=2, repeat_tool_call_stop_after=1
    )

    result = await runtime.run(session.agent, "start", partial_on_failure=True)

    assert result.stopped == "answered"
    assert toolbox.calls == 3
    assert llm.calls == 4


@pytest.mark.asyncio
async def test_append_retries_failed_commit_and_assigns_next_sequence():
    class RetrySession:
        def __init__(self):
            self.turns = [SimpleNamespace(seq=3, kind="user", content={"content": "old"})]
            self.pending = []
            self.commit_calls = 0
            self.rollback_calls = 0

        async def scalar(self, statement):
            if "max(" in str(statement).lower():
                return max((turn.seq for turn in self.turns), default=0) + 1
            return max(self.turns, key=lambda turn: turn.seq, default=None)

        def add(self, turn):
            self.pending.append(turn)

        async def commit(self):
            self.commit_calls += 1
            if self.commit_calls == 1:
                raise RuntimeError("transient commit failure")
            self.turns.extend(self.pending)
            self.pending.clear()

        async def rollback(self):
            self.rollback_calls += 1
            self.pending.clear()

    session = RetrySession()
    runtime = AgentRuntime(session, _Llm(), _Toolbox())

    appended = await runtime._append(1, "assistant", {"message": {"content": "ok"}})

    assert appended.seq == 4
    assert [turn.seq for turn in session.turns] == [3, 4]
    assert session.commit_calls == 2
    assert session.rollback_calls == 1
