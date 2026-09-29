from types import SimpleNamespace

import pytest

from wsignal.inference.tools import ROLE_TOOLS, Toolbox, tools_for


class _Scalars:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class _Session:
    def __init__(self, turns=(), written=0):
        self.agent = SimpleNamespace(id=7, run_id=3, role="researcher", model="test/model")
        self.turns = list(turns)
        self.written = written
        self.added = []

    async def get(self, _model, key):
        return self.agent if key == 7 else None

    async def scalar(self, _statement):
        return self.written

    async def scalars(self, _statement):
        return _Scalars(self.turns)

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        pass

    async def rollback(self):
        pass


def _turn(kind, content):
    return SimpleNamespace(kind=kind, content=content)


def test_every_role_has_harness_note():
    for role in ROLE_TOOLS:
        assert "harness_note" in [s["function"]["name"] for s in tools_for(role)]


@pytest.mark.asyncio
async def test_note_is_stored_with_the_last_exchange_and_answers_noted():
    turns = [
        _turn("tool_call", {"id": "b", "name": "harness_note", "arguments": {}}),
        _turn("tool_result", {"id": "a", "name": "fetch", "payload": {"error": "403"}}),
        _turn("tool_call", {"id": "a", "name": "fetch", "arguments": {"url": "u"}}),
    ]
    session = _Session(turns)
    result = await Toolbox(session, None).call(
        "harness_note", {"kind": "bug", "tool": "fetch", "text": "403 on a public page"}, 7
    )

    assert result == {"noted": True}
    [note] = session.added
    assert (note.run_id, note.agent_id, note.role, note.model) == (3, 7, "researcher", "test/model")
    assert (note.kind, note.tool, note.text) == ("bug", "fetch", "403 on a public page")
    assert note.meta["last_call"]["name"] == "fetch"
    assert '"url": "u"' in note.meta["last_call"]["arguments"]
    assert "403" in note.meta["last_call"]["result"]


@pytest.mark.asyncio
async def test_bad_kind_teaches_and_full_notes_do_not_block():
    session = _Session()
    toolbox = Toolbox(session, None)

    refused = await toolbox.call("harness_note", {"kind": "rant", "text": "x"}, 7)
    assert "friction" in refused["error"]

    session.written = 20
    full = await toolbox.call("harness_note", {"kind": "idea", "text": "x"}, 7)
    assert full["noted"] is False
    assert "carry on" in full["note"]
    assert session.added == []
