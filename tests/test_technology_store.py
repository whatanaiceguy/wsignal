from types import SimpleNamespace

import pytest

from wsignal.inference.tools import ROLE_TOOLS, Toolbox


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class _Session:
    def __init__(self, rows=(), same=None):
        self.rows = list(rows)
        self.same = same
        self.added = []

    async def execute(self, _statement):
        return _Rows(self.rows)

    async def scalar(self, _statement):
        return self.same

    async def get(self, _model, _key):
        return None

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        pass


def _technology(technology_id, name):
    return SimpleNamespace(id=technology_id, canonical_name_ru=None, canonical_name_en=name)


@pytest.mark.asyncio
async def test_known_technologies_labels_how_each_row_matched():
    rows = [
        (_technology(20, "Agent sandboxes"), "agent sandbox", 0.55),
        (_technology(33, "Agentic payments"), "agentic payments", 0.31),
    ]
    toolbox = Toolbox(_Session(rows), None)
    result = await toolbox._known_technologies({"name": "quantum agent sandboxes"}, 1)
    assert result["matches"][0]["match"].startswith("yours contains the stored name")
    assert result["matches"][1]["match"] == "similar spelling only"
    assert result["matches"][0]["similarity"] == 0.55
    assert "not meaning" in result["note"]


@pytest.mark.asyncio
async def test_open_technology_binds_to_an_exact_name_instead_of_a_second_row():
    existing = _technology(20, "Agent sandboxes (isolated execution environments)")
    session = _Session(same=existing)
    toolbox = Toolbox(session, None)

    async def same_name(*_args):
        return existing

    toolbox._same_name = same_name
    session.scalar = _none
    result = await toolbox._open_technology(
        {
            "surface_form": "Agent sandboxes (isolated execution environments)",
            "name_en": "Agent sandboxes (isolated execution environments)",
            "lang": "en",
        },
        1,
    )
    assert result["technology_id"] == 20
    assert result["opened"] is False
    assert "exact name" in result["note"]
    assert [row.__class__.__name__ for row in session.added] == ["Alias"]


async def _none(_statement):
    return None


def test_only_the_orchestrator_can_merge_or_unbind():
    for role, tools in ROLE_TOOLS.items():
        assert ("merge_technologies" in tools) is (role == "orchestrator")
        assert ("unbind_alias" in tools) is (role == "orchestrator")


@pytest.mark.asyncio
async def test_merge_refuses_the_same_id_and_needs_a_reason():
    toolbox = Toolbox(_Session(), None)
    same = await toolbox._merge_technologies({"from_id": 3, "into_id": 3, "reason": "x"}, 1)
    assert "nothing to merge" in same["error"]
    no_reason = await toolbox._merge_technologies({"from_id": 3, "into_id": 4}, 1)
    assert "reason is required" in no_reason["error"]
