from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from wsignal.interface.report import build_response
from wsignal.models import Entry, EntryEdit, EntryPattern, Refutation, Run


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["pending", "completed", "failed", "cancelled", "interrupted"])
async def test_report_links_refutation_by_refuter_and_preserves_missing_responses(state):
    entries = [
        Entry(
            id=i, run_id=7, name_ru=f"Тема {i}", transition_ru="Переход",
            state="banked", score=0.8, why_ru="Причина", searches_run=0,
            sources_checked=0, refuter_agent_id=refuter, rebutted=rebutted,
            corpus_query="photonic processors" if i == 1 else None,
        )
        for i, refuter, rebutted in [(1, 22, True), (2, None, False), (3, 33, True)]
    ]
    claim = {
        "verdict": "partly", "claim": "Утверждение", "opposite": "Возражение",
        "effect": "Слабость ниже", "url": "https://example.test", "quote": "Original quote",
    }
    response = {"attack": "Возражение", "response": "Ответ", "changes": "Без изменений"}
    reviews = [
        Refutation(
            id=1, run_id=7, refuter_agent_id=33, state="completed",
            outcome={"attack": {"claims": [claim]}, "rebuttal": {"rebuttal_responses": [response]}},
        ),
        Refutation(
            id=2, run_id=7, refuter_agent_id=22, state=state,
            outcome={"attack": {"claims": [claim], "queries_and_venues": "3 запроса"}},
        ),
    ]

    class Rows(list):
        def all(self):
            return self

    class Session:
        async def get(self, _model, _id):
            return Run(id=7, query="Тема", state="finished", started_at=datetime.now(UTC))

        async def scalars(self, statement):
            model = statement.column_descriptions[0]["entity"]
            return Rows({
                Entry: entries, EntryPattern: [], Refutation: reviews,
                EntryEdit: [EntryEdit(
                    entry_id=1, created_at=datetime.now(UTC), reason="New evidence",
                    changes={"why_ru": {"old": "old", "new": "new"}},
                )],
            }[model])

        async def execute(self, _statement):
            return Rows()

        async def scalar(self, _statement):
            return 0

    result = (await build_response(Session(), 7)).model_dump()
    first, second, third = result["entries"]
    assert first["corpus_query"] == "photonic processors"
    assert second["corpus_query"] is None
    assert first["edits"][0]["reason"] == "New evidence"
    assert first["edits"][0]["changed"] == ["why_ru"]
    assert second["edits"] == []
    assert first["refutation"] == {
        "state": state, "rebutted": True, "claims": [claim],
        "responses": [], "queries_and_venues": "3 запроса",
    }
    assert second["refutation"] is None
    assert third["refutation"]["responses"] == [response]


@pytest.mark.parametrize("outcome", [None, {"attack": "Unstructured", "rebuttal": "Text"}])
def test_refutation_without_outcome_is_still_visible(outcome):
    from wsignal.interface.report import _refutation

    result = _refutation(
        SimpleNamespace(rebutted=False), Refutation(state="pending", outcome=outcome),
    )
    assert result.model_dump() == {
        "state": "pending", "rebutted": False, "claims": [],
        "responses": [], "queries_and_venues": None,
    }
