from types import SimpleNamespace

import pytest

from wsignal.inference.agent import AgentRuntime, RunCostBudget
from wsignal.inference.orchestration import Orchestration
from wsignal.inference.pipeline import _run_state
from wsignal.inference.tools import ROLE_TOOLS


class _Settings:
    repeat_tool_call_limit = 3
    repeat_tool_call_stop_after = 3


class _Session:
    def __init__(self):
        self.turns = []
        self.agent = SimpleNamespace(id=1, role="orchestrator", state="running")

    async def scalars(self, statement):
        return SimpleNamespace(all=lambda: self.turns)

    async def scalar(self, statement):
        if "max(" in str(statement).lower():
            return max((turn.seq for turn in self.turns), default=0) + 1
        return None

    def add(self, turn):
        turn.seq = max((item.seq for item in self.turns), default=0) + 1
        self.turns.append(turn)

    async def commit(self):
        return None

    async def rollback(self):
        return None


class _Toolbox:
    def schemas_for(self, _role):
        return []

    async def call(self, *_args):
        return {"ok": True}


class _Llm:
    def __init__(self, costs):
        self.costs = iter(costs)
        self.calls = 0

    def model_for(self, _role):
        return "test"

    async def converse(self, *_args, **_kwargs):
        self.calls += 1
        cost = next(self.costs)
        calls = (
            [SimpleNamespace(id="tool-1", name="search", arguments={})]
            if self.calls == 1
            else []
        )
        return SimpleNamespace(
            message={"role": "assistant", "content": "partial", "tool_calls": []},
            content="partial",
            duration_ms=1,
            cost_usd=cost,
            prompt_tokens=1,
            cached_tokens=0,
            tool_calls=calls,
            wants_tools=bool(calls),
            accounting=lambda: {"cost_usd": cost},
        )


@pytest.mark.asyncio
async def test_cost_limit_stops_before_the_next_model_call_and_preserves_entries(monkeypatch):
    monkeypatch.setattr("wsignal.inference.agent.get_settings", _Settings)
    budget = RunCostBudget(0.5)
    budget.record(0.3)
    session = _Session()
    llm = _Llm([0.2, 0.1])
    events = []
    runtime = AgentRuntime(session, llm, _Toolbox(), sink=events.append, cost_budget=budget)
    existing_entries = ["written"]

    result = await runtime.run(session.agent, "go", partial_on_failure=True)

    assert llm.calls == 1
    assert result.stopped == "cost_limit_reached"
    assert "run cost limit reached" in result.content
    assert any(turn.kind == "tool_result" for turn in session.turns)
    assert session.agent.state == "failed"
    assert existing_entries == ["written"]
    assert any(event["kind"] == "cost_limit_reached" for event in events)
    assert _run_state(None, result.stopped) == "exhausted"


@pytest.mark.asyncio
async def test_none_cost_limit_is_unlimited(monkeypatch):
    monkeypatch.setattr("wsignal.inference.agent.get_settings", _Settings)
    session = _Session()
    llm = _Llm([0.3, 0.4])
    runtime = AgentRuntime(session, llm, _Toolbox(), cost_budget=RunCostBudget(None))

    result = await runtime.run(session.agent, "go")

    assert result.stopped == "answered"
    assert llm.calls == 2


@pytest.mark.asyncio
async def test_dispatch_refuses_when_cost_limit_is_reached():
    orchestration = object.__new__(Orchestration)
    orchestration._cost_budget = RunCostBudget(1.0, 1.0)

    result = await orchestration._dispatch_researchers({"field_ids": [1]}, 1)

    assert "run cost limit reached" in result["error"]


def test_orchestrator_has_no_fetch_tool():
    assert "fetch" not in ROLE_TOOLS["orchestrator"]


@pytest.mark.asyncio
async def test_refuter_bundle_quotes_and_urls_have_no_page_content():
    class Session:
        async def scalars(self, _statement):
            return SimpleNamespace(all=lambda: [
                {"name": "search", "arguments": {"adapter": "web_search", "query": "test"}}
            ])

    orchestration = object.__new__(Orchestration)
    orchestration._session = Session()
    async def final_answer(*_args):
        return 'Claim at https://example.test with quote “quoted words here”.'

    orchestration._final_answer = final_answer

    async def documents(_statement):
        return SimpleNamespace(all=lambda: [
            SimpleNamespace(
                url="https://example.test",
                content="page SECRET page content quoted words here",
                fetched_at=1,
            )
        ])

    async def execute(_statement):
        return SimpleNamespace(all=lambda: [])

    orchestration._session.scalars = documents
    orchestration._session.execute = execute
    bundle = await orchestration._lift_bundle(4, "claim", include_citations=True)

    assert "https://example.test" in bundle
    assert "quoted words here" in bundle
    assert "SECRET page content" not in bundle


@pytest.mark.asyncio
async def test_refuter_bundle_uses_submitted_findings_when_message_text_is_empty():
    submission = {
        "claim": "Robotic ultrasound crossed FDA clearance",
        "patterns": [{
            "kind": "regulatory_trigger", "pattern": "p", "strength": 0.8,
            "url": "https://a.test", "quote": "received 510(k) clearance",
        }],
        "citations": [{"url": "https://c.test", "quote": "db quote here"}],
    }
    orchestration = object.__new__(Orchestration)

    async def stored(_agent_id, name, _session=None):
        return submission if name == "submit_findings" else None

    async def final_answer(*_args):
        return ""

    async def scalars(_statement):
        return SimpleNamespace(all=lambda: [
            SimpleNamespace(url="https://c.test", content="x db quote here x", fetched_at=1)
        ])

    rows = [[(1, "https://c.test", "db quote here")]]

    async def execute(_statement):
        return SimpleNamespace(all=lambda: rows.pop() if rows else [])

    orchestration._stored_submission = stored
    orchestration._final_answer = final_answer
    orchestration._session = SimpleNamespace(scalars=scalars, execute=execute)
    bundle = await orchestration._lift_bundle(4, "STATEMENT", include_citations=True)

    assert "Robotic ultrasound crossed FDA clearance" in bundle
    assert "  https://a.test\n    received 510(k) clearance" in bundle
    assert bundle.count("  https://c.test\n    db quote here") == 1
    assert "ЦИТАТЫ (2)" in bundle
