import asyncio
import json
from types import SimpleNamespace

import pytest

from wsignal.config import Settings
from wsignal.inference.agent import AgentResult
from wsignal.inference.orchestration import Orchestration
from wsignal.inference.tools import Toolbox, tools_for, validate_submission


class Session:
    def __init__(self, values=None):
        self.values = values or {}
        self.turns = []
        self.commits = 0

    async def get(self, _model, identity):
        return self.values.get(identity)

    async def scalar(self, _statement):
        return None

    async def scalars(self, _statement):
        return SimpleNamespace(all=lambda: self.turns)

    async def commit(self):
        self.commits += 1


class Fetcher:
    pass


def findings():
    return {
        "claim": "A transition is occurring.",
        "substance": {"value": 0.7, "reason": "Independent deployments."},
        "momentum": {"value": 0.6, "reason": "More activity."},
        "faintness": {"value": 0.8, "reason": "Little recognition."},
        "patterns": [],
        "delivery_gap": "Announced, not running.",
        "scale_against_field": "Small against the field.",
        "what_would_refute": "A mature procurement category.",
        "searched": "12 queries across arxiv and web_search.",
        "citations": [],
    }


def test_submission_validation_rejects_missing_fields_and_out_of_range_values():
    submission = findings()
    del submission["claim"]
    with pytest.raises(ValueError, match="missing fields"):
        validate_submission("submit_findings", submission)
    submission = findings()
    submission["substance"]["value"] = 1.1
    with pytest.raises(ValueError, match="between 0 and 1"):
        validate_submission("submit_findings", submission)


def test_submission_text_caps_and_item_caps_use_settings(monkeypatch):
    monkeypatch.setattr(
        "wsignal.inference.tools.get_settings",
        lambda: Settings(
            submission_text_max_chars=20,
            submission_quote_max_chars=7,
            submission_max_patterns=1,
        ),
    )
    submission = findings()
    submission["claim"] = "123456789012345678901"
    with pytest.raises(ValueError, match="no longer than 20"):
        validate_submission("submit_findings", submission)
    submission["claim"] = "short"
    submission["substance"]["reason"] = "brief"
    submission["momentum"]["reason"] = "brief"
    submission["faintness"]["reason"] = "brief"
    submission["delivery_gap"] = "brief"
    submission["scale_against_field"] = "brief"
    submission["what_would_refute"] = "brief"
    submission["searched"] = "brief"
    submission["patterns"] = [
        {"kind": "substance", "pattern": "p", "strength": 0.5, "url": "https://x", "quote": "q"},
        {"kind": "delivery", "pattern": "p", "strength": 0.4, "url": "https://y", "quote": "q"},
    ]
    result = validate_submission("submit_findings", submission)
    assert len(result["patterns"]) == 1
    schema = next(
        tool["function"] for tool in tools_for("researcher")
        if tool["function"]["name"] == "submit_findings"
    )
    assert schema["parameters"]["properties"]["patterns"]["maxItems"] == 1
    assert schema["parameters"]["properties"]["claim"]["maxLength"] == 20


def test_default_submission_caps_allow_full_reasons_but_bound_the_return(monkeypatch):
    monkeypatch.setattr("wsignal.inference.tools.get_settings", Settings)
    submission = findings()
    submission["claim"] = "c" * 2300
    submission["faintness"]["reason"] = "r" * 2300
    submission["citations"] = [{"url": "https://source.test", "quote": "q" * 2300}]
    assert validate_submission("submit_findings", submission) == submission

    submission["claim"] = "c" * 2401
    with pytest.raises(ValueError, match="no longer than 2400"):
        validate_submission("submit_findings", submission)

    submission["claim"] = "c" * 2300
    for dimension in ("substance", "momentum", "faintness"):
        submission[dimension]["reason"] = "r" * 2300
    for field in ("delivery_gap", "scale_against_field", "what_would_refute", "searched"):
        submission[field] = "x" * 2300
    submission["citations"] = [
        {"url": "https://source.test", "quote": "q" * 2300} for _ in range(2)
    ]
    assert len(json.dumps(submission, ensure_ascii=False, separators=(",", ":"))) > 20000
    assert validate_submission("submit_findings", submission) == submission

    submission["claim"] = "c" * 2400
    for dimension in ("substance", "momentum", "faintness"):
        submission[dimension]["reason"] = "r" * 2400
    for field in ("delivery_gap", "scale_against_field", "what_would_refute", "searched"):
        submission[field] = "x" * 2400
    submission["citations"] = [
        {"url": "u" * 2400, "quote": "q" * 2400} for _ in range(3)
    ]
    with pytest.raises(ValueError, match="total character cap"):
        validate_submission("submit_findings", submission)


def test_refutation_schema_validates_verdict_and_caps_items():
    submission = {
        "claims": [{
            "claim": "Claim.", "opposite": "Opposite.", "verdict": "partly",
            "effect": "Faintness falls 0.1.", "url": "https://source.test",
            "quote": "Verbatim.",
        }],
        "queries_and_venues": "3 queries at two venues.",
    }
    assert validate_submission("submit_refutation", submission) == submission
    submission["claims"][0]["verdict"] = "maybe"
    with pytest.raises(ValueError, match="invalid value"):
        validate_submission("submit_refutation", submission)


@pytest.mark.asyncio
async def test_toolbox_returns_structured_submission_or_validation_error(monkeypatch):
    monkeypatch.setattr("wsignal.inference.tools.get_settings", Settings)
    toolbox = Toolbox(Session(), Fetcher())
    result = await toolbox.call("submit_findings", findings(), 5)
    assert result["submitted"] is True
    assert result["submission"] == findings()
    invalid = await toolbox.call("submit_findings", {"claim": "missing"}, 5)
    assert "error" in invalid


@pytest.mark.asyncio
async def test_collect_delivers_only_structured_submission_and_fallback_is_truncated(monkeypatch):
    monkeypatch.setattr(
        "wsignal.inference.orchestration.get_settings",
        lambda: Settings(return_fallback_max_chars=4),
    )
    orchestration = object.__new__(Orchestration)
    orchestration._partition_worker = False
    orchestration._run = SimpleNamespace(id=9)
    orchestration._session = Session()
    orchestration._inflight = {}
    orchestration._returns = asyncio.Queue()
    orchestration._sink = None
    orchestration._returns.put_nowait({
        "kind": "research", "agent_id": 1, "stopped": "submitted",
        "analysis": findings(), "submitted": True, "raw": "not exposed",
    })
    orchestration._returns.put_nowait({
        "kind": "research", "agent_id": 2, "stopped": "max_steps",
        "analysis": "123456789", "submitted": False,
    })
    result = await orchestration._collect({}, 10)
    assert result["returns"][0]["analysis"] == findings()
    assert "raw" not in result["returns"][0]
    assert result["returns"][1]["analysis"] == "1234"
    assert "did not submit" in result["returns"][1]["note"]


@pytest.mark.asyncio
async def test_read_return_pages_full_stored_final_text(monkeypatch):
    text = "x" * 45000
    turns = [{"message": {"content": text}}]
    agent = SimpleNamespace(id=4, run_id=9, role="researcher")
    session = Session({4: agent})
    session.turns = turns
    orchestration = object.__new__(Orchestration)
    orchestration._session = session
    orchestration._run = SimpleNamespace(id=9)
    monkeypatch.setattr(
        "wsignal.inference.orchestration.get_settings",
        lambda: Settings(read_return_part_chars=20000),
    )
    first = await orchestration._read_return({"agent_id": 4, "part": 1}, 99)
    third = await orchestration._read_return({"agent_id": 4, "part": 3}, 99)
    assert (first["total"], first["parts"], len(first["text"])) == (45000, 3, 20000)
    assert (third["part"], third["parts"], len(third["text"])) == (3, 3, 5000)
    assert first["text"] + ("x" * 20000) + third["text"] == text


@pytest.mark.asyncio
async def test_refuter_opening_bundle_keeps_researcher_full_answer():
    full_answer = "researcher's complete answer " * 1000
    researcher = SimpleNamespace(
        id=8, run_id=9, role="researcher", state="idle", field="field",
        parent_agent_id=10,
    )
    refuter = SimpleNamespace(id=11)
    session = Session({8: researcher})
    session.add = lambda _row: None
    async def scalars(_statement):
        return SimpleNamespace(all=lambda: [])
    session.scalars = scalars
    async def commit():
        pass
    session.commit = commit
    orchestration = object.__new__(Orchestration)
    orchestration._session = session
    orchestration._run = SimpleNamespace(id=9)
    orchestration._partition_worker = False
    orchestration._runtime = SimpleNamespace(create=lambda **_kwargs: _created_refuter(refuter))
    orchestration._prompts = {"refuter": "refuter prompt"}
    orchestration._owner_agent_id = 10
    orchestration._inflight = {}
    orchestration._research_change_error = lambda _target: _none()
    captured = {}
    async def lift_bundle(_target, _title, include_citations=False):
        captured["bundle"] = full_answer
        return full_answer
    orchestration._lift_bundle = lift_bundle
    async def refuter_task(*args, **_kwargs):
        captured["passed_bundle"] = args[4]
    orchestration._refuter_task = refuter_task
    result = await orchestration._refute({"agent_id": 8, "rebut": False}, 10)
    await asyncio.gather(*orchestration._inflight.values())
    assert result["attacked_agent_id"] == 8
    assert captured["passed_bundle"] == full_answer


async def _created_refuter(agent):
    return agent


async def _none():
    return None


@pytest.mark.asyncio
async def test_agent_result_has_submission_separate_from_full_text():
    result = AgentResult(1, "full transcript final answer", 2, "submitted", 1, findings())
    assert result.content == "full transcript final answer"
    assert result.submission == findings()
