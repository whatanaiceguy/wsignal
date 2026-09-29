from wsignal.inference.orchestration import Orchestration, _resolution_demand, apply_corrections
from wsignal.inference.prompts import load_prompts
from wsignal.inference.tools import ROLE_TOOLS


def _unwrapped(text: str) -> str:
    """One line, so an assertion survives the prose being rewrapped.

    Both of the first two failures here were a phrase split across a line break,
    which is a test failing for the formatting of the thing it is checking.
    """
    return " ".join(text.split())

class _Store:
    """Just enough session to answer `get`, which is all `_technology_of` asks."""

    def __init__(self, existing: set[int]) -> None:
        self._existing = existing

    async def get(self, _model, candidate):
        return object() if candidate in self._existing else None


def _orchestration(existing: set[int]) -> Orchestration:
    orchestration = object.__new__(Orchestration)
    orchestration._session = _Store(existing)

    async def _no_near(_ids):
        return []

    orchestration._near_technologies = _no_near
    return orchestration


async def test_a_field_id_in_the_technology_slot_resolves_to_nothing():
    """The exact run 5 mistake: 122 is a field, and there is no technology 122."""
    orchestration = _orchestration({1, 2, 3})
    assert await orchestration._technology_of({"technology_id": 2}) == 2
    assert await orchestration._technology_of({"technology_id": 122}) is None
    assert await orchestration._technology_of({"technology_id": "потом"}) is None
    assert await orchestration._technology_of({"technology_id": 0}) is None
    assert await orchestration._technology_of({}) is None


async def test_the_audit_separates_a_missing_technology_from_a_shared_one():
    orchestration = _orchestration({1, 2})
    problems = await orchestration._field_problems(
        [
            {"focus": "persistent memory for personal AI", "technology_id": 1},
            {"focus": "user context memory layer", "technology_id": 1},
            {"focus": "portable AI identity", "technology_id": 2},
            {"focus": "personal knowledge graphs", "technology_id": 99},
            {"focus": "on-device personal AI"},
        ]
    )
    assert problems["unresolved"] == ["personal knowledge graphs", "on-device personal AI"]
    assert problems["shared"] == [
        {
            "technology_id": 1,
            "fields": ["persistent memory for personal AI", "user context memory layer"],
        }
    ]


async def test_one_field_per_technology_is_not_a_problem():
    orchestration = _orchestration({1, 2})
    problems = await orchestration._field_problems(
        [
            {"focus": "portable AI identity", "technology_id": 1},
            {"focus": "delegated agent payments", "technology_id": 2},
        ]
    )
    assert problems == {"unresolved": [], "shared": [], "near": []}


def test_the_demand_names_every_problem_and_asks_for_targeted_corrections():
    """A demand that summarises instead of naming leaves it to guess which ones."""
    demand = _resolution_demand(
        {
            "unresolved": ["personal knowledge graphs"],
            "shared": [{"technology_id": 7, "fields": ["память ИИ", "memory layer"]}],
            "near": [
                {
                    "left_id": 3,
                    "left": "квантовые технологии",
                    "right_id": 9,
                    "right": "технологии в квантовой области",
                    "closeness": 0.41,
                }
            ],
        }
    )
    assert "personal knowledge graphs" in demand
    assert "technology_id=7" in demand
    assert "память ИИ" in demand and "memory layer" in demand
    assert "квантовые технологии" in demand
    assert "технологии в квантовой области" in demand
    assert "ВЕСЬ список заново" not in demand
    assert '"fixes"' in demand and '"merges"' in demand and '"keep"' in demand
    assert "known_technologies" in demand and "open_technology" in demand


def test_corrections_apply_each_operation_and_preserve_rationale():
    items = [
        {"focus": "Missing", "rationale": "initial"},
        {"focus": "Duplicate"},
        {"focus": "Survivor"},
    ]
    result, report = apply_corrections(
        items,
        {
            "fixes": [{"focus": " missing ", "technology_id": 12}],
            "merges": [{"focus": "Duplicate", "into_focus": "Survivor"}],
            "keep": [{"focus": "SURVIVOR", "why": "Это отдельный переход."}],
        },
    )
    assert [item["focus"] for item in result] == ["Missing", "Survivor"]
    assert result[0]["technology_id"] == 12
    assert result[1]["rationale"] == "Это отдельный переход."
    assert len(report["applied"]) == 3


def test_corrections_report_missing_focus_and_merge_target():
    result, report = apply_corrections(
        [{"focus": "one"}],
        {
            "fixes": [{"focus": "absent", "technology_id": 1}],
            "merges": [{"focus": "one", "into_focus": "absent"}],
        },
    )
    assert result == [{"focus": "one"}]
    assert len(report["unmatched"]) == 2


def test_malformed_corrections_are_reported_without_crashing():
    result, report = apply_corrections(
        [{"focus": "one", "rationale": "old"}],
        {"fixes": [{"focus": "one", "technology_id": "bad"}], "keep": [None]},
    )
    assert result == [{"focus": "one", "rationale": "old"}]
    assert len(report["skipped"]) == 2


def test_the_orchestrator_can_open_a_technology_and_not_only_look_one_up():
    """It is refused an entry with no technology, so it needs a way to make one."""
    assert "open_technology" in ROLE_TOOLS["orchestrator"]
    assert "known_technologies" in ROLE_TOOLS["orchestrator"]
    assert "fetch" not in ROLE_TOOLS["orchestrator"]
    assert "search" in ROLE_TOOLS["orchestrator"]
    assert "open_technology" in ROLE_TOOLS["assistant"]
    assert "open_technology" not in ROLE_TOOLS["researcher"]
    assert "open_technology" not in ROLE_TOOLS["refuter"]
    assert "rename_topic" in ROLE_TOOLS["researcher"]
    assert "rename_topic" in ROLE_TOOLS["refuter"]
    assert "rename_topic" in ROLE_TOOLS["orchestrator"]


def test_prompts_warn_field_ids_are_not_technology_ids():
    prompts = load_prompts()
    assert "field id is not a technology id" in _unwrapped(prompts["orchestrator"]).lower()
    assistant = _unwrapped(prompts["assistant"]).lower()
    assert "technology_id" in assistant and "required" in assistant and "every field" in assistant


def test_assistant_requires_a_resolved_technology_for_every_field():
    assistant = _unwrapped(load_prompts()["assistant"])
    assert "required on every field" in assistant
