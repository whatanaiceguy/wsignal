import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from wsignal.inference.agent import AgentResult
from wsignal.inference.orchestration import ORCHESTRATOR_TOOLS, Orchestration
from wsignal.models import Citation, Entry, Refutation


class _Scalars:
    def __init__(self, values):
        self.values = values

    def all(self):
        return self.values


class _Session:
    def __init__(self, values, scalar_value=1):
        self.values = values
        self.scalar_value = scalar_value
        self.document = None
        self.document_results = []
        self.document_statements = []
        self.field_statement = None
        self.refutation_rows = []
        self.delivered_result = False
        self.added = []
        self.commits = 0
        self.next_id = 100
        self.entry_id = None
        self.entries = []

    async def scalars(self, statement):
        if "turns.content" in str(statement):
            return _Scalars([{}] if self.delivered_result else [])
        if "fields." in str(statement):
            self.field_statement = statement
        if "refutations" in str(statement):
            return _Scalars(self.refutation_rows)
        if "fields." in str(statement):
            rows = getattr(self, "field_rows", self.values)
            if "orchestrator_agent_id IS NOT NULL" in str(statement):
                rows = [row for row in rows if row.orchestrator_agent_id is not None]
            return _Scalars(rows)
        return _Scalars(self.values)

    async def scalar(self, statement):
        if "fields.id" in str(statement) and " LIMIT " in str(statement):
            if not getattr(self, "partitioned", False):
                self.partitioned = True
                return None
            return 1
        if "entries.id" in str(statement):
            if self.entry_id is not None:
                return self.entry_id
            existing = next(iter(self.entries), None)
            return getattr(existing, "id", None)
        if "documents" in str(statement):
            self.document_statements.append(statement)
            if self.document_results:
                return self.document_results.pop(0)
            if self.document is not None:
                return self.document
        if "refutations" in str(statement):
            return None
        return self.scalar_value

    async def execute(self, _statement):
        return _Scalars([])

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        for row in self.added:
            if getattr(row, "id", None) is None:
                row.id = self.next_id
                self.next_id += 1
        for row in self.added:
            if row.__class__.__name__ == "Entry" and row not in self.entries:
                self.entries.append(row)

    async def get(self, _model, identity):
        return self.values.get(identity) if isinstance(self.values, dict) else None

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
async def test_child_field_reads_are_shard_scoped():
    fields = [
        SimpleNamespace(
            id=1,
            area=None,
            focus="owned",
            rationale=None,
            state="pending",
            agent_id=None,
        ),
        SimpleNamespace(
            id=2,
            area=None,
            focus="foreign",
            rationale=None,
            state="pending",
            agent_id=None,
        ),
    ]
    orchestration = object.__new__(Orchestration)
    session = _Session(fields)
    orchestration._session = session
    orchestration._run = SimpleNamespace(id=9)
    orchestration._owner_agent_id = 20
    orchestration._partition_worker = True
    orchestration._child_shard = True

    result = await orchestration._read_fields({}, 20)
    compiled = str(session.field_statement.compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
    ))

    assert "fields.orchestrator_agent_id = 20" in compiled
    assert [field["id"] for field in result["fields"]] == [1, 2]


@pytest.mark.asyncio
async def test_collect_waits_for_a_return_while_work_is_running():
    class CollectSession(_Session):
        async def scalars(self, _statement):
            return _Scalars([])

        async def commit(self):
            pass

    orchestration = object.__new__(Orchestration)
    orchestration._partition_worker = False
    orchestration._owner_agent_id = 20
    orchestration._run = SimpleNamespace(id=9)
    orchestration._session = CollectSession([])
    orchestration._inflight = {30: object()}
    orchestration._returns = asyncio.Queue()
    orchestration._sink = None

    async def deliver():
        await asyncio.sleep(0)
        orchestration._returns.put_nowait(
            {"kind": "research", "field_id": 4, "analysis": "finished"}
        )

    delivery = asyncio.create_task(deliver())
    result = await orchestration._collect({"wait_s": 1}, 20)
    await delivery

    assert result["returns"] == [
        {"kind": "research", "field_id": 4, "analysis": "finished"}
    ]
    assert result["still_running"] == [30]


@pytest.mark.asyncio
async def test_collect_without_running_work_names_owned_pending_fields():
    fields = _fields(2)
    for field in fields:
        field.orchestrator_agent_id = 20

    class CollectSession(_Session):
        async def scalars(self, statement):
            if "fields." in str(statement):
                self.field_statement = statement
                return _Scalars(fields)
            return _Scalars([])

        async def commit(self):
            pass

    orchestration = object.__new__(Orchestration)
    orchestration._partition_worker = True
    orchestration._owner_agent_id = 20
    orchestration._run = SimpleNamespace(id=9)
    orchestration._session = CollectSession([])
    orchestration._inflight = {}
    orchestration._returns = asyncio.Queue()
    orchestration._sink = None

    orchestration._research_budget = 2
    orchestration._researched = set()
    result = await orchestration._collect({}, 20)

    assert result["pending_field_ids"] == [0, 1]
    assert result["error"]
    assert "dispatch_researchers" in result["next_action"]


@pytest.mark.asyncio
async def test_idle_collect_auto_dispatch_uses_research_ceiling():
    fields = _fields(2)
    for field in fields:
        field.orchestrator_agent_id = 20
    orchestration = _dispatcher(fields, ceiling=1)
    orchestration._child_shard = True
    orchestration._partition_worker = True
    orchestration._returns = asyncio.Queue()
    orchestration._sink = None
    previous = None
    scalar = orchestration._session.scalar

    async def stored_result(statement):
        if "turns.content" in str(statement):
            return previous
        return await scalar(statement)

    created = []

    async def create(**kwargs):
        created.append(kwargs)
        return SimpleNamespace(id=200)

    async def researcher_task(*_args):
        return None

    orchestration._session.scalar = stored_result
    orchestration._runtime.create = create
    orchestration._researcher_task = researcher_task
    first = await orchestration._collect({}, 20)
    assert first["error"]
    assert not created
    previous = {"name": "collect", "payload": first}
    second = await orchestration._collect({}, 20)
    await asyncio.gather(*orchestration._inflight.values())
    assert [item["field_id"] for item in second["dispatch"]["dispatched"]] == [0]
    assert second["dispatch"]["not_dispatched"] == [
        {"field_id": 1, "reason": "research budget exhausted"},
    ]
    assert second["still_running"] == [200]
    assert orchestration._researched == {0}
    assert created[0]["parent_agent_id"] == 20
    assert fields[1].state == "pending"


@pytest.mark.asyncio
async def test_collect_delivers_returns_and_names_any_still_pending_fields():
    fields = _fields(1)
    fields[0].orchestrator_agent_id = 20

    class CollectSession(_Session):
        async def scalars(self, _statement):
            return _Scalars(fields)

        async def commit(self):
            pass

    orchestration = object.__new__(Orchestration)
    orchestration._partition_worker = True
    orchestration._owner_agent_id = 20
    orchestration._run = SimpleNamespace(id=9)
    orchestration._session = CollectSession([])
    orchestration._selected_fields = {9}
    orchestration._inflight = {}
    orchestration._returns = asyncio.Queue()
    orchestration._returns.put_nowait(
        {"kind": "research", "field_id": 9, "analysis": "settled"}
    )
    orchestration._sink = None

    result = await orchestration._collect({}, 20)

    assert result["collected"] == 1
    assert result["pending_field_ids"] == [0]
    assert "dispatch_researchers" in result["next_action"]


@pytest.mark.asyncio
async def test_collect_without_running_work_and_no_pending_fields_says_finish_or_write():
    class CollectSession(_Session):
        async def scalars(self, _statement):
            return _Scalars([])

        async def commit(self):
            pass

    orchestration = object.__new__(Orchestration)
    orchestration._partition_worker = False
    orchestration._owner_agent_id = 20
    orchestration._run = SimpleNamespace(id=9)
    orchestration._session = CollectSession([])
    orchestration._inflight = {}
    orchestration._selected_fields = set()
    orchestration._returns = asyncio.Queue()
    orchestration._sink = None

    result = await orchestration._collect({}, 20)

    assert result["done"] is True
    assert "remaining entries" in result["next_action"]


@pytest.mark.asyncio
async def test_collect_rejects_wrong_writer_before_reading_returns():
    orchestration = object.__new__(Orchestration)
    orchestration._partition_worker = True
    orchestration._owner_agent_id = 20
    orchestration._returns = asyncio.Queue()
    orchestration._returns.put_nowait({"kind": "research", "field_id": 1})

    result = await orchestration._collect({}, 21)

    assert "shard orchestrator" in result["error"]
    assert orchestration._returns.qsize() == 1


@pytest.mark.asyncio
async def test_summon_returns_the_existing_partition_on_second_call():
    fields = [
        SimpleNamespace(
            id=index,
            focus=f"field {index}",
            rationale="why",
            ordinal=index,
            orchestrator_agent_id=20 if index < 6 else 21,
            state="dispatched" if index < 6 else "pending",
        )
        for index in range(11)
    ]
    orchestration = object.__new__(Orchestration)
    orchestration._session = _Session(fields, scalar_value=None)
    orchestration._run = SimpleNamespace(id=9)
    orchestration._initial_agent_id = 20
    orchestration._owner_agent_id = 20
    orchestration._child_shard = False
    orchestration._partition_worker = True
    orchestration._researched = set(range(6))
    orchestration._run_ceiling = 20
    orchestration._shard_size = 10
    orchestration._prompts = {"orchestrator": "prompt"}
    orchestration._summon_callback = None

    created = []

    async def create(**_kwargs):
        created.append(21)
        return SimpleNamespace(id=21)

    orchestration._runtime = SimpleNamespace(create=create)

    result = await orchestration._summon_orchestrators(
        {"field_ids": list(range(11)), "brief": "brief"}, 20
    )
    orchestration._session.partitioned = True
    existing = await orchestration._summon_orchestrators(
        {"field_ids": list(range(11)), "brief": "brief"}, 20
    )

    assert "error" not in result
    assert result["count"] == existing["count"] == 2
    assert [field.orchestrator_agent_id for field in fields] == [20] * 6 + [21] * 5
    assert created == [21]


@pytest.mark.asyncio
async def test_summon_has_no_thirty_field_cap():
    fields = [
        SimpleNamespace(
            id=index,
            focus=f"field {index}",
            rationale="why",
            ordinal=index,
            state="pending",
            run_id=9,
            orchestrator_agent_id=None,
        )
        for index in range(31)
    ]
    orchestration = object.__new__(Orchestration)
    orchestration._session = _Session(fields, scalar_value=None)
    orchestration._run = SimpleNamespace(id=9)
    orchestration._initial_agent_id = 20
    orchestration._owner_agent_id = 20
    orchestration._child_shard = False
    orchestration._partition_worker = False
    orchestration._shard_size = 10
    orchestration._prompts = {"orchestrator": "system"}
    orchestration._researched = set()
    orchestration._selected_fields = set()
    orchestration._research_budget = 31
    orchestration._max_research = 31
    orchestration._run_ceiling = 31
    orchestration._summon_callback = None
    next_owner = iter(range(21, 24))

    async def create(**_kwargs):
        return SimpleNamespace(id=next(next_owner))

    orchestration._runtime = SimpleNamespace(create=create)

    result = await orchestration._summon_orchestrators(
        {"field_ids": list(range(31)), "brief": "focus"}, 20
    )

    assert [len(shard["field_ids"]) for shard in result["orchestrators"]] == [8, 8, 8, 7]


@pytest.mark.asyncio
async def test_dispatch_skips_non_pending_fields_and_reports_reasons():
    fields = _fields(4, dispatched=(1,))
    fields[2].state = "done"
    orchestration = _dispatcher(fields, ceiling=4, scalar_values=[None] * 4)
    created = []

    async def create(**kwargs):
        created.append(kwargs["field"])
        return SimpleNamespace(id=200 + len(created))

    async def researcher_task(*_args):
        return None

    orchestration._runtime.create = create
    orchestration._researcher_task = researcher_task

    result = await orchestration._dispatch_researchers({"field_ids": [0, 1, 2, 3]}, 20)
    await asyncio.gather(*orchestration._inflight.values())

    assert created == ["field 0", "field 3"]
    assert [item["field_id"] for item in result["dispatched"]] == [0, 3]
    assert {item["field_id"]: item["reason"] for item in result["not_dispatched"]} == {
        1: "field is already dispatched",
        2: "field is done",
    }


@pytest.mark.asyncio
async def test_dispatch_skips_field_with_existing_entry():
    fields = _fields(2)
    orchestration = _dispatcher(fields, ceiling=2)
    orchestration._session.existing_field_entries.add(1)
    created = []

    async def create(**kwargs):
        created.append(kwargs["field"])
        return SimpleNamespace(id=200 + len(created))

    async def researcher_task(*_args):
        return None

    orchestration._runtime.create = create
    orchestration._researcher_task = researcher_task

    result = await orchestration._dispatch_researchers({"field_ids": [0, 1]}, 20)
    await asyncio.gather(*orchestration._inflight.values())

    assert created == ["field 0"]
    assert len(result["not_dispatched"]) == 1
    assert result["not_dispatched"][0]["field_id"] == 1
    assert "already has an entry" in result["not_dispatched"][0]["reason"]


@pytest.mark.asyncio
async def test_dispatch_reports_unknown_ids_and_respects_request_order_when_budget_short():
    fields = _fields(3)
    orchestration = _dispatcher(fields, ceiling=2, scalar_values=[None] * 3)
    created = []

    async def create(**kwargs):
        created.append(kwargs["field"])
        return SimpleNamespace(id=200 + len(created))

    async def researcher_task(*_args):
        return None

    orchestration._runtime.create = create
    orchestration._researcher_task = researcher_task

    result = await orchestration._dispatch_researchers(
        {"field_ids": [2, 999, 0, 1]}, 20
    )
    await asyncio.gather(*orchestration._inflight.values())

    assert created == ["field 2", "field 0"]
    assert result["unknown_field_ids"] == [999]
    assert [item["field_id"] for item in result["dispatched"]] == [2, 0]
    assert {item["field_id"]: item["reason"] for item in result["not_dispatched"]} == {
        999: "field does not exist in this run",
        1: "research budget exhausted",
    }


@pytest.mark.asyncio
async def test_read_limits_are_clamped_to_one_through_two_hundred():
    class ReadSession(_Session):
        async def scalars(self, statement):
            self.statement = statement
            return _Scalars([])

    orchestration = object.__new__(Orchestration)
    orchestration._session = ReadSession([])
    orchestration._run = SimpleNamespace(id=9)
    orchestration._partition_worker = False
    orchestration._child_shard = False
    for limit, expected in ((-5, "LIMIT 1"), (999, "LIMIT 200")):
        await orchestration._read_fields({"limit": limit}, 20)
        actual = list(orchestration._session.statement.compile().params.values())[-1]
        assert actual == int(expected.split()[1])

    orchestration._session.scalar_value = None
    for limit, expected in ((0, "LIMIT 1"), (999, "LIMIT 200")):
        await orchestration._read_entries({"limit": limit}, 20)
        actual = list(orchestration._session.statement.compile().params.values())[-1]
        assert actual == int(expected.split()[1])


@pytest.mark.asyncio
async def test_child_dispatch_rejects_a_foreign_field():
    foreign = SimpleNamespace(id=9, orchestrator_agent_id=21)
    orchestration = object.__new__(Orchestration)
    orchestration._session = _Session([foreign], scalar_value=1)
    orchestration._run = SimpleNamespace(id=9)
    orchestration._owner_agent_id = 20
    orchestration._partition_worker = True
    orchestration._child_shard = True
    orchestration._selected_fields = {8}
    orchestration._research_budget = 10
    orchestration._researched = set()

    result = await orchestration._dispatch_researchers({"field_ids": [9]}, 20)

    assert result["field_ids"] == [9]
    assert "another orchestrator shard" in result["error"]


@pytest.mark.asyncio
async def test_child_write_entry_rejects_a_foreign_field():
    writer = SimpleNamespace(id=20, run_id=9, role="orchestrator")
    foreign = SimpleNamespace(id=9, run_id=9, orchestrator_agent_id=21)
    orchestration = object.__new__(Orchestration)
    orchestration._session = _Session({20: writer, 9: foreign})
    orchestration._run = SimpleNamespace(id=9)
    orchestration._owner_agent_id = 20
    orchestration._partition_worker = True
    orchestration._child_shard = True
    orchestration._selected_fields = {8}

    result = await orchestration._write_entry(
        {
            "field_id": 9,
            "citations": [],
            "name_ru": "name",
            "transition_ru": "transition",
            "corpus_query": "photonic processors",
            "why_ru": "why",
            "current_state_ru": "state",
            "dynamics_ru": "dynamics",
            "what_would_refute_ru": "refute",
            "problem_ru": "problem",
            "advantage_ru": "advantage",
            "case_example_ru": "example",
            "state": "noise",
            "substance": 0.2,
            "momentum": 0.2,
            "faintness": 0.8,
            "score": 0.2,
        },
        20,
    )

    assert "another orchestrator shard" in result["error"]


@pytest.mark.asyncio
async def test_summon_creates_shards_and_assigns_every_selected_field():
    fields = [
        SimpleNamespace(
            id=index,
            focus=f"field {index}",
            rationale="why",
            ordinal=index,
            state="pending",
            run_id=9,
            orchestrator_agent_id=None,
        )
        for index in range(11)
    ]
    orchestration = object.__new__(Orchestration)
    orchestration._session = _Session(fields, scalar_value=None)
    orchestration._run = SimpleNamespace(id=9)
    orchestration._initial_agent_id = 20
    orchestration._owner_agent_id = 20
    orchestration._child_shard = False
    orchestration._partition_worker = False
    orchestration._shard_size = 10
    orchestration._runtime = SimpleNamespace(create=None)

    async def create(**_kwargs):
        return SimpleNamespace(id=21)

    orchestration._runtime.create = create
    orchestration._prompts = {"orchestrator": "system"}
    orchestration._researched = set()
    orchestration._selected_fields = set()
    orchestration._research_budget = 20
    orchestration._max_research = 20
    orchestration._run_ceiling = 20
    orchestration._summon_callback = None

    result = await orchestration._summon_orchestrators(
        {"field_ids": list(range(11)), "brief": "focus"}, 20
    )

    assert [len(shard["field_ids"]) for shard in result["orchestrators"]] == [6, 5]
    assert sorted(
        field_id
        for shard in result["orchestrators"]
        for field_id in shard["field_ids"]
    ) == list(range(11))
    assert len(
        {
            field_id
            for shard in result["orchestrators"]
            for field_id in shard["field_ids"]
        }
    ) == 11
    assert {field.orchestrator_agent_id for field in fields} == {20, 21}
    assert all(
        shard["ceiling"] == len(shard["field_ids"])
        for shard in result["orchestrators"]
    )


def _dispatcher(fields, ceiling=30, researched=(), scalar_values=None):
    orchestration = object.__new__(Orchestration)
    session = _Session({field.id: field for field in fields}, scalar_value=None)
    session.field_rows = fields
    session.existing_field_entries = set()

    async def scalar(statement):
        if "entries.id" in str(statement):
            params = statement.compile().params
            field_id = next(value for key, value in params.items() if key.startswith("field_id"))
            return 700 + field_id if field_id in session.existing_field_entries else None
        return None

    session.field_rows = fields
    session.scalar = scalar
    orchestration._session = session
    orchestration._run = SimpleNamespace(id=9)
    orchestration._owner_agent_id = 20
    orchestration._partition_worker = False
    orchestration._child_shard = False
    orchestration._selected_fields = set()
    orchestration._research_budget = ceiling
    orchestration._researched = set(researched)
    orchestration._shard_size = 100
    orchestration._inflight = {}
    orchestration._prompts = {"researcher": "research"}
    orchestration._runtime = SimpleNamespace(create=None)
    return orchestration


def _summoner(fields, researched=(), ceiling=30, shard_size=10):
    orchestration = object.__new__(Orchestration)
    orchestration._session = _Session(fields, scalar_value=None)
    orchestration._run = SimpleNamespace(id=9)
    orchestration._initial_agent_id = 20
    orchestration._owner_agent_id = 20
    orchestration._child_shard = False
    orchestration._partition_worker = False
    orchestration._shard_size = shard_size
    orchestration._prompts = {"orchestrator": "system"}
    orchestration._researched = set(researched)
    orchestration._selected_fields = set(researched)
    orchestration._research_budget = ceiling
    orchestration._max_research = ceiling
    orchestration._run_ceiling = ceiling
    orchestration._summon_callback = None
    next_owner = iter(range(21, 30))

    async def create(**_kwargs):
        return SimpleNamespace(id=next(next_owner))

    orchestration._runtime = SimpleNamespace(create=create)
    return orchestration


@pytest.mark.asyncio
async def test_seeded_shard_size_one_summon_names_the_summoners_owned_field():
    fields = _fields(2)
    orchestration = _summoner(fields, ceiling=2, shard_size=1)
    orchestration._run.seeded = True

    async def seeded_scalars(statement):
        if "fields.state IN" in str(statement):
            return _Scalars([field.id for field in fields])
        if "orchestrator_agent_id IS NOT NULL" in str(statement):
            return _Scalars([])
        if "fields." in str(statement):
            return _Scalars(fields)
        return _Scalars([])

    orchestration._session.scalars = seeded_scalars
    orchestration._initial_agent_id = 20
    orchestration._owner_agent_id = 20

    result = await orchestration._summon_orchestrators(
        {"field_ids": [0, 1], "brief": "seeded"}, 20
    )

    assert result["count"] == 2
    assert [shard["field_ids"] for shard in result["orchestrators"]] == [[0], [1]]
    assert result["summoner_owned_field_ids"] == [0]
    assert "Dispatch every pending field" in result["instruction"]
    assert fields[0].orchestrator_agent_id == 20
    assert fields[1].orchestrator_agent_id != 20


@pytest.mark.asyncio
async def test_summon_partitions_ten_fields_into_four_three_three():
    fields = _fields(10)
    orchestration = _summoner(fields, ceiling=10, shard_size=4)

    result = await orchestration._summon_orchestrators(
        {"field_ids": list(range(10)), "brief": "b"}, 20
    )

    assert [len(shard["field_ids"]) for shard in result["orchestrators"]] == [4, 3, 3]


@pytest.mark.asyncio
async def test_summon_refuses_when_total_does_not_exceed_shard_size():
    orchestration = _summoner(_fields(4), ceiling=4, shard_size=4)

    result = await orchestration._summon_orchestrators(
        {"field_ids": list(range(4)), "brief": "b"}, 20
    )

    assert "4 or fewer fields" in result["error"]


@pytest.mark.asyncio
async def test_dispatch_refuses_past_configured_shard_size():
    orchestration = _summoner(_fields(5), ceiling=5, shard_size=4)

    result = await orchestration._dispatch_researchers({"field_ids": list(range(5))}, 20)

    assert "at most 4 fields" in result["error"]
    assert "summon_orchestrators" in result["error"]


def _citation_writer(language):
    document = SimpleNamespace(
        id=50,
        url=f"https://source.test/{language}",
        source_lang=language,
        title="Source title",
        source_name="Source",
        source_type="page",
        source_tier=2,
        published_at=None,
        fetched_at=None,
        retrieved=True,
        adapter=None,
        content=f"content-{language}",
    )
    writer = SimpleNamespace(id=20, run_id=9, role="orchestrator")
    field = SimpleNamespace(
        id=1,
        run_id=9,
        focus="SSD as KV-cache tier for inference",
        agent_id=30,
        technology_id=40,
        orchestrator_agent_id=None,
        state="dispatched",
    )
    researcher = SimpleNamespace(
        id=30, run_id=9, role="researcher", parent_agent_id=20, state="idle"
    )
    technology = SimpleNamespace(
        id=40, canonical_name_ru="Технология поля", canonical_name_en="Field technology"
    )
    session = _Session(
        {20: writer, 30: researcher, 1: field, 40: technology}, scalar_value=None
    )
    session.document = document
    orchestration = object.__new__(Orchestration)
    orchestration._session = session
    orchestration._run = SimpleNamespace(id=9)
    orchestration._partition_worker = False
    orchestration._inflight = {}
    orchestration._sink = None
    return orchestration, session, document


def _citation_page_and_snippet():
    orchestration, session, _ = _citation_writer("ru")
    page = SimpleNamespace(
        id=51,
        url="https://source.test/shared",
        source_lang="en",
        title="Fetched page",
        source_name="Source",
        source_type="page",
        source_tier=2,
        published_at=None,
        fetched_at=1,
        retrieved=True,
        adapter="fetch",
        content="Fetched page content",
    )
    snippet = SimpleNamespace(
        id=52,
        url=page.url,
        source_lang="und",
        title="Search snippet",
        source_name="Source",
        source_type="page",
        source_tier=2,
        published_at=None,
        fetched_at=2,
        retrieved=False,
        adapter="brave",
        content="Search snippet content",
    )
    session.document = None
    session.document_results = [page]
    return orchestration, session, page, snippet


def _citation_arguments(url, summary=None):
    citation = {"url": url, "quote": "verbatim quote"}
    if summary is not None:
        citation["summary_ru"] = summary
    return {
        "field_id": 1,
        "researched_by_agent_id": 30,
        "technology_id": 40,
        "name_ru": "Название",
        "transition_ru": "Переход",
        "corpus_query": "photonic processors",
        "state": "noise",
        "substance": 0.2,
        "momentum": 0.2,
        "faintness": 0.8,
        "score": 0.99,
        "why_ru": "Причина",
        "current_state_ru": "Состояние",
        "dynamics_ru": "Динамика",
        "what_would_refute_ru": "Опровержение",
        "problem_ru": "Проблема",
        "advantage_ru": "Преимущество",
        "case_example_ru": "Пример",
        "review_incomplete_reason": "Проверка не завершена",
        "citations": [citation],
    }


@pytest.mark.asyncio
async def test_write_entry_prefers_older_fetched_page_to_newer_snippet(monkeypatch):
    async def effort_for(_session, _agent_ids):
        return {"searches_run": 0, "sources_checked": 0}

    monkeypatch.setattr("wsignal.inference.orchestration.effort_for", effort_for)
    orchestration, session, page, _snippet = _citation_page_and_snippet()
    summary = "Краткое изложение содержания страницы на русском языке."

    result = await orchestration._write_entry(
        _citation_arguments(page.url, summary), 20
    )

    stored = next(row for row in session.added if isinstance(row, Citation))
    fetchable_sql = str(session.document_statements[0].compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
    ))
    assert "documents.retrieved IS true" in fetchable_sql
    assert "documents.source_type NOT IN ('paper', 'patent')" in fetchable_sql
    assert "ORDER BY documents.fetched_at DESC" in fetchable_sql
    assert result["citations_stored"] == 1
    assert stored.document_id == page.id
    assert page.source_lang == "en"
    assert page.content == "Fetched page content"


@pytest.mark.asyncio
async def test_write_entry_falls_back_to_snippet_when_no_page_exists(monkeypatch):
    async def effort_for(_session, _agent_ids):
        return {"searches_run": 0, "sources_checked": 0}

    monkeypatch.setattr("wsignal.inference.orchestration.effort_for", effort_for)
    orchestration, session, _page, snippet = _citation_page_and_snippet()
    session.document_results = [None, snippet]

    result = await orchestration._write_entry(
        _citation_arguments(snippet.url, "Источник описан по-русски."), 20
    )

    stored = next(row for row in session.added if isinstance(row, Citation))
    fetchable_sql = str(session.document_statements[0].compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
    ))
    fallback_sql = str(session.document_statements[1].compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
    ))
    assert "documents.retrieved IS true" in fetchable_sql
    assert "documents.url = 'https://source.test/shared'" in fallback_sql
    assert "WHERE documents.url" in fallback_sql
    assert "documents.retrieved IS true" not in fallback_sql
    assert result["citations_stored"] == 1
    assert stored.document_id == snippet.id
    assert snippet.source_lang == "und"
    assert snippet.content == "Search snippet content"


@pytest.mark.asyncio
async def test_write_entry_defaults_name_to_renamed_field_focus(monkeypatch):
    async def effort_for(_session, _agent_ids):
        return {"searches_run": 0, "sources_checked": 0}

    monkeypatch.setattr("wsignal.inference.orchestration.effort_for", effort_for)
    orchestration, session, document = _citation_writer("ru")
    arguments = _citation_arguments(document.url)
    arguments.pop("name_ru")
    arguments["citations"] = []

    result = await orchestration._write_entry(arguments, 20)

    entry = next(row for row in session.added if isinstance(row, Entry))
    assert entry.name_ru == "SSD as KV-cache tier for inference"
    assert result["entry_id"] == entry.id


@pytest.mark.asyncio
async def test_write_entry_summary_precheck_uses_fetched_page_language(monkeypatch):
    async def effort_for(_session, _agent_ids):
        return {"searches_run": 0, "sources_checked": 0}

    monkeypatch.setattr("wsignal.inference.orchestration.effort_for", effort_for)

    orchestration, session, page, _snippet = _citation_page_and_snippet()
    session.document_results = [page]
    result = await orchestration._write_entry(_citation_arguments(page.url), 20)
    assert result["urls"] == [page.url]
    assert session.added == []

    page.source_lang = "ru"
    session.document_results = [page]
    result = await orchestration._write_entry(_citation_arguments(page.url), 20)
    assert result["citations_stored"] == 1
    stored = next(row for row in session.added if isinstance(row, Citation))
    assert stored.document_id == page.id


@pytest.mark.asyncio
async def test_write_entry_computes_score_from_dimensions_and_ignores_passed_score(monkeypatch):
    async def effort_for(_session, _agent_ids):
        return {"searches_run": 0, "sources_checked": 0}

    monkeypatch.setattr("wsignal.inference.orchestration.effort_for", effort_for)
    monkeypatch.setattr(
        "wsignal.inference.orchestration.get_settings",
        lambda: SimpleNamespace(
            entry_score_substance_weight=0.75,
            entry_score_momentum_weight=0.25,
            signal_noise_below=0.25,
            signal_faintness_gate_centre=0.275,
            signal_faintness_gate_width=0.05,
            signal_strong_substance_at_least=0.6,
        ),
    )
    orchestration, session, document = _citation_writer("ru")
    arguments = _citation_arguments(document.url)
    arguments.update(substance=0.8, momentum=0.4, faintness=0.9, score=0.01, citations=[])

    result = await orchestration._write_entry(arguments, 20)

    from wsignal.models import Entry

    entry = next(row for row in session.added if isinstance(row, Entry))
    assert entry.score == 0.7
    assert entry.weak_score == 0.581
    assert entry.signal_class == "weak"
    assert (entry.substance, entry.momentum, entry.faintness) == (0.8, 0.4, 0.9)
    assert result["entry_id"] == entry.id


@pytest.mark.asyncio
async def test_write_entry_snapshots_response_before_commit_expires_entry(monkeypatch):
    from sqlalchemy import inspect

    from wsignal.models import Entry

    async def effort_for(_session, _agent_ids):
        return {"searches_run": 0, "sources_checked": 0}

    monkeypatch.setattr("wsignal.inference.orchestration.effort_for", effort_for)
    orchestration, session, document = _citation_writer("ru")

    async def expire_on_commit():
        session.commits += 1
        entry = next(row for row in session.added if isinstance(row, Entry))
        inspect(entry)._expire_attributes(
            entry.__dict__, set(Entry.__mapper__.column_attrs.keys())
        )

    session.commit = expire_on_commit
    arguments = _citation_arguments(document.url)
    arguments["citations"] = []

    result = await orchestration._write_entry(arguments, 20)

    assert result["entry_id"] > 0
    assert result["searches_run"] == 0
    assert session.commits == 1


def test_write_entry_schema_makes_technology_id_optional_and_nullable():
    schema = next(
        tool["function"]
        for tool in ORCHESTRATOR_TOOLS
        if tool["function"]["name"] == "write_entry"
    )

    assert schema["parameters"]["properties"]["technology_id"]["type"] == [
        "integer",
        "null",
    ]
    assert {"substance", "momentum", "faintness"} <= set(
        schema["parameters"]["required"]
    )
    assert "score" not in schema["parameters"]["properties"]
    assert "technology_id" not in schema["parameters"].get("required", [])


@pytest.mark.asyncio
async def test_write_entry_inherits_field_technology_when_id_is_omitted(monkeypatch):
    async def effort_for(_session, _agent_ids):
        return {"searches_run": 0, "sources_checked": 0}

    monkeypatch.setattr("wsignal.inference.orchestration.effort_for", effort_for)
    orchestration, session, document = _citation_writer("ru")
    arguments = _citation_arguments(document.url)
    arguments.pop("technology_id")
    arguments["citations"] = []

    result = await orchestration._write_entry(arguments, 20)

    from wsignal.models import Entry

    entry = next(row for row in session.added if isinstance(row, Entry))
    assert result["technology_id"] == entry.technology_id == 40


@pytest.mark.asyncio
@pytest.mark.parametrize("technology_id", [None, 0])
async def test_write_entry_inherits_field_technology_for_null_or_zero_id(
    monkeypatch, technology_id
):
    async def effort_for(_session, _agent_ids):
        return {"searches_run": 0, "sources_checked": 0}

    monkeypatch.setattr("wsignal.inference.orchestration.effort_for", effort_for)
    orchestration, session, document = _citation_writer("ru")
    arguments = _citation_arguments(document.url)
    arguments["technology_id"] = technology_id
    arguments["citations"] = []

    result = await orchestration._write_entry(arguments, 20)

    from wsignal.models import Entry

    entry = next(row for row in session.added if isinstance(row, Entry))
    assert result["technology_id"] == entry.technology_id == 40


@pytest.mark.asyncio
async def test_write_entry_rejects_conflicting_field_technology_id():
    orchestration, session, document = _citation_writer("ru")
    arguments = _citation_arguments(document.url)
    arguments["technology_id"] = 41

    result = await orchestration._write_entry(arguments, 20)

    assert "technology_id=40" in result["error"]
    assert "Технология поля" in result["error"]
    assert "omit technology_id or pass null/0" in result["error"]
    assert session.added == []
    assert session.commits == 0


@pytest.mark.asyncio
async def test_seeded_write_entry_uses_field_technology_for_a_differing_id(monkeypatch):
    async def effort_for(_session, _agent_ids):
        return {"searches_run": 0, "sources_checked": 0}

    monkeypatch.setattr("wsignal.inference.orchestration.effort_for", effort_for)
    orchestration, session, document = _citation_writer("ru")
    orchestration._run = SimpleNamespace(id=9, seeded=True)
    arguments = _citation_arguments(document.url)
    arguments["technology_id"] = 41
    arguments["citations"] = []

    result = await orchestration._write_entry(arguments, 20)

    from wsignal.models import Entry

    entry = next(row for row in session.added if isinstance(row, Entry))
    assert result["technology_id"] == entry.technology_id == 40


@pytest.mark.asyncio
async def test_write_entry_retry_refuses_duplicate_field_and_returns_existing_id(monkeypatch):
    async def effort_for(_session, _agent_ids):
        return {"searches_run": 0, "sources_checked": 0}

    monkeypatch.setattr("wsignal.inference.orchestration.effort_for", effort_for)
    orchestration, session, _document = _citation_writer("ru")
    arguments = _citation_arguments("https://source.test/ru")

    first = await orchestration._write_entry(arguments, 20)
    second = await orchestration._write_entry(arguments, 20)

    assert second["existing_entry_id"] == first["entry_id"]
    assert "already has entry" in second["error"]
    assert len([row for row in session.added if row.__class__.__name__ == "Entry"]) == 1
    assert session.commits == 1


@pytest.mark.asyncio
async def test_write_entry_accepts_delivery_pattern_and_reports_unmatched_quote(monkeypatch):
    async def effort_for(_session, _agent_ids):
        return {"searches_run": 0, "sources_checked": 0}

    monkeypatch.setattr("wsignal.inference.orchestration.effort_for", effort_for)
    orchestration, session, document = _citation_writer("ru")
    arguments = _citation_arguments(document.url)
    arguments["patterns"] = [
        {
            "kind": "delivery",
            "pattern": "delivery_gap",
            "quote": "not stored",
            "strength": 0.7,
        }
    ]

    result = await orchestration._write_entry(arguments, 20)

    pattern = next(row for row in session.added if row.__class__.__name__ == "EntryPattern")
    assert pattern.kind == "delivery"
    assert pattern.strength == 0.7
    assert pattern.citation_id is None
    assert result["patterns_without_matching_citation_quote"] == [
        {"kind": "delivery", "pattern": "delivery_gap", "quote": "not stored"}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("extended", [False, True])
async def test_structured_refutation_rebuttal_can_be_collected_and_banked(monkeypatch, extended):
    attack = {
        "claims": [{
            "claim": "Переход есть.", "opposite": "Перехода нет.",
            "verdict": "partly", "effect": "Deployment evidence narrows the claim.",
            "url": "https://source.test/ru", "quote": "Verbatim.",
        }],
        "queries_and_venues": "Three searches across two venues.",
    }
    rebuttal = {
        "claim": "The transition remains early.",
        "substance": {"value": 0.8, "reason": "Independent deployments."},
        "momentum": {"value": 0.7, "reason": "More projects."},
        "faintness": {"value": 0.8, "reason": "Limited recognition."},
        "patterns": [],
        "delivery_gap": "Announced, not routine.",
        "scale_against_field": "Small relative to the field.",
        "what_would_refute": "A mature procurement category.",
        "searched": "Three queries across two venues.",
        "citations": [],
    }
    orchestration, session, document = _citation_writer("ru")
    if extended:
        from datetime import UTC, datetime, timedelta

        from wsignal.inference.orchestration import Budget

        orchestration._budget = Budget(datetime.now(UTC) - timedelta(seconds=61), 1)
        assert orchestration._budget.extend({"fields_unwritten": 1})
    session.values[30].field = "test field"
    session.values[44] = SimpleNamespace(id=44, state="running", role="refuter")
    session.field_rows = []
    orchestration._prompts = {"refuter": "Test the claim."}
    orchestration._owner_agent_id = 20
    orchestration._returns = asyncio.Queue()
    orchestration._sessionmaker = lambda: SessionContext()
    orchestration._llm = object()
    orchestration._fetcher = object()
    orchestration._cache = object()
    orchestration._agent_max_steps = 2
    orchestration._max_model_calls = 300
    messages = []

    class SessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *_exc):
            pass

    async def create(**_kwargs):
        return session.values[44]

    async def no_conflict(_target):
        return None

    original_commit = session.commit
    original_scalars = session.scalars

    async def scalars(statement):
        if "FROM documents" in str(statement):
            return _Scalars([])
        return await original_scalars(statement)

    async def commit():
        await session.flush()
        for row in session.added:
            if isinstance(row, Refutation):
                if row.delivery_version is None:
                    row.delivery_version = 0
                session.values[row.id] = row
                session.refutation_rows = [row]
        await original_commit()

    class Runtime:
        def __init__(self, *_args, **_kwargs):
            pass

        async def run(self, agent, _message, **_kwargs):
            assert agent.id == 44
            return AgentResult(44, None, 1, "submitted", 1, attack)

        async def resume(self, agent_id, message, **_kwargs):
            assert agent_id == 30
            messages.append(message)
            return AgentResult(30, None, 1, "submitted", 1, rebuttal)

    async def effort_for(_session, _agent_ids):
        return {"searches_run": 3, "sources_checked": 1, "tool_calls": 2}

    session.commit = commit
    session.scalars = scalars
    orchestration._runtime = SimpleNamespace(create=create)
    orchestration._research_change_error = no_conflict
    monkeypatch.setattr("wsignal.inference.orchestration.AgentRuntime", Runtime)
    monkeypatch.setattr("wsignal.inference.orchestration.effort_for", effort_for)

    started = await orchestration._refute({"agent_id": 30}, 20)
    await asyncio.gather(*orchestration._inflight.values())
    review = session.values[started["refutation_id"]]
    assert review.state == "completed"
    assert review.outcome["attack"] == attack
    assert review.outcome["rebuttal"] == rebuttal
    assert "Перехода нет" in messages[0]
    assert "Three searches across two venues" in messages[0]

    collected = await orchestration._collect({}, 20)
    assert collected["returns"][0]["attack"] == attack
    assert collected["returns"][0]["rebuttal"] == rebuttal
    assert review.collected is True

    async def delivered(saved_review, _writer_id):
        return any(
            item["refutation_id"] == saved_review.id
            and item["delivery_version"] == saved_review.delivery_version
            for item in collected["returns"]
        )

    orchestration._review_delivered = delivered
    arguments = _citation_arguments(document.url)
    arguments.update(state="banked", substance=0.8, momentum=0.7, faintness=0.8)
    result = await orchestration._write_entry(arguments, 20)

    assert result["entry_id"]
    entry = next(row for row in session.added if isinstance(row, Entry))
    assert entry.state == "banked"
    assert entry.rebutted is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "reviews", "delivered", "reason", "expected_error"),
    [
        (
            "banked", [SimpleNamespace(id=1, state="completed", refuter_agent_id=44)],
            set(), None, "collect",
        ),
        (
            "parked", [SimpleNamespace(id=1, state="completed", refuter_agent_id=44)],
            {1}, None, None,
        ),
        (
            "banked", [SimpleNamespace(id=1, state="pending", refuter_agent_id=44)],
            {1}, None, "still running",
        ),
        (
            "parked", [SimpleNamespace(id=1, state="failed", refuter_agent_id=44)],
            {1}, None, "needs a completed",
        ),
        ("noise", [], set(), None, "review_incomplete_reason"),
        ("insufficient", [], set(), None, "review_incomplete_reason"),
        (
            "noise", [SimpleNamespace(id=1, state="failed", refuter_agent_id=44)],
            {1}, "failed review", None,
        ),
    ],
)
async def test_write_entry_refutation_gates(
    state, reviews, delivered, reason, expected_error, monkeypatch
):
    async def effort_for(_session, _agent_ids):
        return {"searches_run": 0, "sources_checked": 0}

    monkeypatch.setattr("wsignal.inference.orchestration.effort_for", effort_for)
    orchestration, session, document = _citation_writer("ru")
    session.refutation_rows = reviews

    async def review_delivered(review, _writer_id):
        return review.id in delivered

    orchestration._review_delivered = review_delivered
    arguments = _citation_arguments(document.url)
    arguments["state"] = state
    arguments["review_incomplete_reason"] = reason
    if reviews:
        session.values[30].state = "idle"
    if (
        reviews and state in ("banked", "parked") and delivered
        and all(review.state != "pending" for review in reviews)
        and expected_error is None
    ):
        session.refutation_rows = [
            SimpleNamespace(
                id=review.id,
                state="completed",
                refuter_agent_id=review.refuter_agent_id,
                rebuttal_required=False,
                outcome={},
            )
            for review in reviews
        ]

    result = await orchestration._write_entry(arguments, 20)

    if expected_error is not None:
        assert expected_error in result["error"]
        assert session.added == []
        assert session.commits == 0
    else:
        assert result["entry_id"]
        assert session.commits == 1


@pytest.mark.asyncio
async def test_successful_write_stores_entry_citation_pattern_and_completes_field(monkeypatch):
    async def effort_for(_session, _agent_ids):
        return {"searches_run": 3, "sources_checked": 2}

    monkeypatch.setattr("wsignal.inference.orchestration.effort_for", effort_for)
    orchestration, session, document = _citation_writer("ru")
    arguments = _citation_arguments(document.url)
    arguments.update(state="banked", review_incomplete_reason=None)
    arguments["patterns"] = [
        {
            "kind": "substance",
            "pattern": "funding_trajectory",
            "quote": "verbatim quote",
            "strength": 0.85,
        }
    ]
    review = SimpleNamespace(
        id=12,
        state="completed",
        refuter_agent_id=44,
        rebuttal_required=True,
        outcome={"rebuttal": "answer"},
    )
    session.refutation_rows = [review]
    async def delivered(_review, _writer_id):
        return True

    orchestration._review_delivered = delivered

    result = await orchestration._write_entry(arguments, 20)

    from wsignal.models import Entry, EntryPattern

    entry = next(row for row in session.added if isinstance(row, Entry))
    citation = next(row for row in session.added if isinstance(row, Citation))
    pattern = next(row for row in session.added if isinstance(row, EntryPattern))
    assert result["entry_id"] == entry.id
    assert (entry.field_id, entry.researched_by_agent_id, entry.written_by_agent_id) == (
        1, 30, 20
    )
    assert (
        entry.searches_run,
        entry.sources_checked,
        entry.refuter_agent_id,
        entry.rebutted,
    ) == (3, 2, 44, True)
    assert (citation.entry_id, citation.document_id, citation.quote) == (
        entry.id, document.id, "verbatim quote"
    )
    assert (pattern.entry_id, pattern.pattern, pattern.citation_id) == (
        entry.id, "funding_trajectory", citation.id
    )
    assert pattern.strength == 0.85
    assert session.values[1].state == "done"
    assert session.commits == 1


@pytest.mark.asyncio
async def test_failed_citation_fetch_stores_unverified_citation_and_writes_entry(monkeypatch):
    async def effort_for(_session, _agent_ids):
        return {"searches_run": 0, "sources_checked": 0}

    async def fetch_failure(_url, _writer_id):
        raise OSError("connection refused")

    monkeypatch.setattr("wsignal.inference.orchestration.effort_for", effort_for)
    orchestration, session, _document = _citation_writer("ru")
    orchestration._fetch_citation_page = fetch_failure
    session.document = None
    url = "https://missing.test/no-copy"

    result = await orchestration._write_entry(_citation_arguments(url), 20)

    from wsignal.models import Document, Entry

    citation = next(row for row in session.added if isinstance(row, Citation))
    failure = next(row for row in session.added if isinstance(row, Document))
    assert result["urls_not_in_store"] == [f"{url} (OSError: connection refused)"]
    assert result["entry_id"]
    assert citation.document_id == failure.id
    assert failure.source_type == "fetch_failure"
    assert "connection refused" in failure.content
    assert any(isinstance(row, Entry) for row in session.added)
    assert session.values[1].state == "done"
    assert session.commits == 1


@pytest.mark.asyncio
async def test_write_entry_reports_unknown_pattern_kinds(monkeypatch):
    async def effort_for(_session, _agent_ids):
        return {"searches_run": 0, "sources_checked": 0}

    monkeypatch.setattr("wsignal.inference.orchestration.effort_for", effort_for)
    orchestration, _session, document = _citation_writer("ru")
    arguments = _citation_arguments(document.url)
    arguments["patterns"] = [
        {"kind": "future_axis", "pattern": "test", "quote": "verbatim quote", "strength": 0.1}
    ]

    result = await orchestration._write_entry(arguments, 20)

    assert result["dropped_pattern_kinds"] == ["future_axis"]


def _fields(count, dispatched=()):
    return [
        SimpleNamespace(
            id=index,
            focus=f"field {index}",
            rationale="why",
            ordinal=index,
            state="dispatched" if index in dispatched else "pending",
            run_id=9,
            orchestrator_agent_id=None,
        )
        for index in range(count)
    ]


@pytest.mark.asyncio
async def test_summon_refuses_more_fields_than_the_run_may_research():
    fields = _fields(20, dispatched=range(6))
    orchestration = _summoner(fields, researched=range(6), ceiling=12)

    result = await orchestration._summon_orchestrators(
        {"field_ids": list(range(6, 13)), "brief": "b"}, 20
    )

    assert result["research_ceiling"] == 12
    assert result["already_researched"] == 6
    assert result["may_still_add"] == 6
    assert result["asked_to_add"] == 7
    assert all(field.orchestrator_agent_id is None for field in fields)
    assert orchestration._partition_worker is False


@pytest.mark.asyncio
async def test_summon_keeps_already_dispatched_fields_with_the_initial_orchestrator():
    fields = _fields(12, dispatched=range(6))
    orchestration = _summoner(fields, researched=range(6), ceiling=20)

    result = await orchestration._summon_orchestrators(
        {"field_ids": list(range(6, 12)), "brief": "b"}, 20
    )

    shards = result["orchestrators"]
    assert [shard["orchestrator_agent_id"] for shard in shards] == [20, 21]
    assert shards[0]["field_ids"] == [0, 1, 2, 3, 4, 5]
    assert shards[1]["field_ids"] == [6, 7, 8, 9, 10, 11]
    assert orchestration._selected_fields == {0, 1, 2, 3, 4, 5}
    assert orchestration._research_budget == 6
    assert orchestration._run_ceiling == 20


@pytest.mark.asyncio
async def test_summon_gives_a_full_first_shard_to_the_initial_when_it_already_holds_ten():
    fields = _fields(13, dispatched=range(10))
    orchestration = _summoner(fields, researched=range(10), ceiling=20)

    result = await orchestration._summon_orchestrators(
        {"field_ids": [10, 11, 12], "brief": "b"}, 20
    )

    assert [shard["field_ids"] for shard in result["orchestrators"]] == [
        list(range(10)),
        [10, 11, 12],
    ]


@pytest.mark.asyncio
async def test_summon_rejects_fields_that_are_neither_pending_nor_already_dispatched():
    fields = _fields(12)
    fields[3].state = "done"
    orchestration = _summoner(fields)

    result = await orchestration._summon_orchestrators(
        {"field_ids": list(range(12)), "brief": "b"}, 20
    )

    assert result["field_ids"] == [3]


@pytest.mark.asyncio
async def test_dispatch_past_ten_points_at_summon_including_dispatched_fields():
    orchestration = _summoner(_fields(12, dispatched=range(6)), researched=range(6))

    result = await orchestration._dispatch_researchers({"field_ids": list(range(6, 12))}, 20)

    assert "summon_orchestrators" in result["error"]
    assert "already dispatched" in result["error"]


@pytest.mark.asyncio
async def test_collect_delivers_owned_returns_when_a_foreign_one_is_in_the_queue():
    owned = {"kind": "research", "field_id": 1, "analysis": "mine"}
    foreign = {"kind": "research", "field_id": 2, "agent_id": 7, "analysis": "theirs"}
    foreign_review = {"kind": "refutation", "refutation_id": 50}
    events = []
    orchestration = object.__new__(Orchestration)
    orchestration._partition_worker = True
    orchestration._owner_agent_id = 20
    orchestration._selected_fields = {1}
    orchestration._run = SimpleNamespace(id=9)
    orchestration._inflight = {}
    orchestration._sink = events.append
    orchestration._session = _Session(
        {
            50: SimpleNamespace(researcher_agent_id=60, state="completed", delivery_version=1),
            60: SimpleNamespace(parent_agent_id=21),
        }
    )
    async def no_pending_fields(_statement):
        return _Scalars([])

    orchestration._session.scalars = no_pending_fields
    orchestration._returns = asyncio.Queue()
    for item in (owned, foreign, foreign_review):
        orchestration._returns.put_nowait(item)

    result = await orchestration._collect({}, 20)

    assert result["returns"] == [owned]
    assert result["refutations_delivered"] == []
    assert orchestration._returns.empty()
    assert [event["kind"] for event in events] == ["foreign_returns"]
    assert [item["field_id"] for item in events[0]["items"]] == [2, None]


@pytest.mark.asyncio
async def test_forced_entry_is_written_without_a_researched_field():
    orchestration, session, _document = _citation_writer("ru")
    arguments = _citation_arguments("https://source.test/ru")
    arguments.update(
        field_id=None,
        researched_by_agent_id=None,
        citations=[],
        force=True,
        force_reason="Системный тест без агентов",
    )

    result = await orchestration._write_entry(arguments, 20)

    entries = [row for row in session.added if row.__class__.__name__ == "Entry"]
    assert result["forced"] is True
    assert result["field_id"] is None
    assert len(entries) == 1
    assert entries[0].forced_reason == "Системный тест без агентов"
    assert entries[0].researched_by_agent_id is None
    assert entries[0].technology_id == 40


@pytest.mark.asyncio
async def test_forced_entry_needs_a_reason_and_unforced_names_the_way_out():
    orchestration, session, _document = _citation_writer("ru")
    arguments = _citation_arguments("https://source.test/ru")
    arguments.update(field_id=None, researched_by_agent_id=None, citations=[], force=True)

    refused = await orchestration._write_entry(arguments, 20)
    assert "force_reason" in refused["error"]

    arguments.pop("force")
    unforced = await orchestration._write_entry(arguments, 20)
    assert "no field_id was given" in unforced["error"]
    assert "force=true" in unforced["error"]
    assert not [row for row in session.added if row.__class__.__name__ == "Entry"]


def _entry_editor():
    orchestration, session, document = _citation_writer("ru")
    entry = Entry(
        id=70, run_id=9, field_id=1, researched_by_agent_id=30,
        name_ru="Original", name_en="Original", transition_ru="Old transition",
        state="noise", score=0.1, weak_score=0.1, signal_class="noise",
        substance=0.2, momentum=0.3, faintness=0.4, why_ru="Old reason",
        corpus_query="photonic processors",
    )
    session.values[70] = entry
    session.pairs = []
    session.patterns = []
    session.deleted = []
    original_scalar = session.scalar

    async def scalar(statement):
        if statement.column_descriptions[0]["entity"] is Entry:
            params = statement.compile().params
            return entry if (
                params.get("id_1") == entry.id and params.get("run_id_1") == entry.run_id
            ) else None
        return await original_scalar(statement)

    async def scalars(statement):
        from wsignal.models import EntryPattern

        entity = statement.column_descriptions[0]["entity"]
        return _Scalars({
            EntryPattern: session.patterns, Refutation: session.refutation_rows,
        }.get(entity, []))

    async def execute(statement):
        if statement.column_descriptions[0]["entity"] is Entry:
            return _Scalars([SimpleNamespace(id=entry.id, name_ru=entry.name_ru)])
        return _Scalars(session.pairs)

    async def delete(row):
        session.deleted.append(row)

    session.scalar = scalar
    session.scalars = scalars
    session.execute = execute
    session.delete = delete
    return orchestration, session, entry, document


@pytest.mark.asyncio
async def test_edit_entry_changes_prose_and_logs_old_new():
    from wsignal.models import EntryEdit

    orchestration, session, entry, _ = _entry_editor()
    entry.forced_reason = "User requested an unresearched entry"
    result = await orchestration._edit_entry({
        "entry_id": 70, "reason": "New evidence", "changes": {"why_ru": "New reason"},
    }, 20)
    assert result["changed"] == ["why_ru"]
    assert entry.why_ru == "New reason"
    assert entry.forced_reason == "User requested an unresearched entry"
    edit = next(row for row in session.added if isinstance(row, EntryEdit))
    assert edit.changes == {"why_ru": {"old": "Old reason", "new": "New reason"}}
    assert (edit.entry_id, edit.run_id, edit.agent_id, edit.reason) == (70, 9, 20, "New evidence")
    assert result["edit_id"] == edit.id
    assert session.commits == 1


@pytest.mark.asyncio
async def test_edit_entry_recomputes_all_scores():
    from wsignal.config import get_settings
    from wsignal.inference.scoring import classify_signal, compute_entry_score, compute_weak_score

    orchestration, _, entry, _ = _entry_editor()
    result = await orchestration._edit_entry({
        "entry_id": 70, "reason": "Wrong metric",
        "changes": {"substance": 0.9, "momentum": 0.8, "faintness": 0.7},
    }, 20)
    s = get_settings()
    assert result["score"] == compute_entry_score(
        0.9, 0.8, s.entry_score_substance_weight, s.entry_score_momentum_weight,
    )
    assert result["weak_score"] == compute_weak_score(
        0.9, 0.8, 0.7, s.signal_faintness_gate_centre, s.signal_faintness_gate_width,
    )
    assert result["signal_class"] == classify_signal(
        0.9, 0.7, s.signal_noise_below, s.signal_faintness_gate_centre,
        s.signal_strong_substance_at_least,
    )
    assert entry.score == result["score"]


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["banked", "parked"])
@pytest.mark.parametrize("review_state,delivered", [
    (None, False), ("pending", False), ("failed", True), ("completed", False),
    ("completed", True),
])
async def test_edit_entry_requires_completed_collected_review(state, review_state, delivered):
    orchestration, session, entry, _ = _entry_editor()
    if review_state:
        session.refutation_rows = [SimpleNamespace(id=4, state=review_state)]

    async def review_delivered(_review, _writer):
        return delivered

    orchestration._review_delivered = review_delivered
    result = await orchestration._edit_entry({
        "entry_id": 70, "reason": "Late review", "changes": {"state": state},
    }, 20)
    if review_state == "completed" and delivered:
        assert entry.state == state
    else:
        assert "completed, collected refutation" in result["error"]
        assert entry.state == "noise"
        assert not session.added


@pytest.mark.asyncio
async def test_edit_entry_refuses_noop():
    orchestration, session, _, _ = _entry_editor()
    result = await orchestration._edit_entry({
        "entry_id": 70, "reason": "No correction", "changes": {"why_ru": "Old reason"},
    }, 20)
    assert "already has these values" in result["error"]
    assert not session.added
    assert session.commits == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign_run", [False, True])
async def test_edit_entry_unknown_or_other_run_lists_entries(foreign_run):
    orchestration, session, _, _ = _entry_editor()
    if foreign_run:
        orchestration._run.id = 99
        session.values[20].run_id = 99
    result = await orchestration._edit_entry({
        "entry_id": 70 if foreign_run else 999, "reason": "Correction",
        "changes": {"why_ru": "New"},
    }, 20)
    assert "not an entry of run" in result["error"]
    assert result["entries_in_this_run"] == [{"entry_id": 70, "name_ru": "Original"}]
    assert not session.added


@pytest.mark.asyncio
@pytest.mark.parametrize("partition", [False, True])
async def test_edit_entry_refuses_foreign_shard(partition):
    orchestration, session, _, _ = _entry_editor()
    orchestration._partition_worker = partition
    orchestration._child_shard = True
    orchestration._owner_agent_id = 20
    orchestration._selected_fields = {1}
    session.values[1].orchestrator_agent_id = 21
    result = await orchestration._edit_entry({
        "entry_id": 70, "reason": "Correction", "changes": {"why_ru": "New"},
    }, 20)
    assert "outside shard" in result["error"]
    assert not session.added


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"substance": True}, {"momentum": 2}, {"faintness": float("nan")},
    {"why_ru": ""}, {"corpus_query": ""}, {"citations": [{"url": "x"}]},
    {"patterns": [{"strength": -1}]}, {"forced_reason": None}, {"state": "done"},
])
async def test_edit_entry_validates_changed_fields(changes):
    orchestration, session, _, _ = _entry_editor()
    result = await orchestration._edit_entry({
        "entry_id": 70, "reason": "Correction", "changes": changes,
    }, 20)
    assert "error" in result
    assert not session.added


@pytest.mark.asyncio
async def test_edit_entry_replaces_citations_and_relinks_patterns():
    from wsignal.models import EntryEdit, EntryPattern

    orchestration, session, _, document = _entry_editor()
    old_citation = Citation(id=80, entry_id=70, document_id=50, quote="old")
    session.pairs = [(old_citation, document)]
    old_pattern = EntryPattern(
        id=81, entry_id=70, kind="substance", pattern="prototype", strength=0.5,
        citation_id=80,
    )
    session.patterns = [old_pattern]
    result = await orchestration._edit_entry({
        "entry_id": 70, "reason": "Better evidence",
        "changes": {
            "citations": [{"url": document.url, "quote": "new"}],
            "patterns": [{"kind": "substance", "pattern": "prototype",
                          "strength": 0.8, "quote": "new"}],
        },
    }, 20)
    assert result["changed"] == ["citations", "patterns"]
    citation = next(row for row in session.added if isinstance(row, Citation))
    pattern = next(row for row in session.added if isinstance(row, EntryPattern))
    edit = next(row for row in session.added if isinstance(row, EntryEdit))
    assert pattern.citation_id == citation.id
    assert session.deleted == [old_pattern, old_citation]
    assert edit.changes["citations"] == {
        "old": [{"url": document.url, "quote": "old"}],
        "new": [{"url": document.url, "quote": "new"}],
    }


@pytest.mark.asyncio
async def test_edit_entry_reuses_foreign_citation_summary_validation():
    orchestration, session, _, document = _entry_editor()
    document.source_lang = "en"
    result = await orchestration._edit_entry({
        "entry_id": 70, "reason": "Better evidence",
        "changes": {"citations": [{"url": document.url, "quote": "new"}]},
    }, 20)
    assert "summary_ru" in result["error"]
    assert not session.added
    assert not session.deleted


def test_entry_edits_migration_imports():
    import importlib.util
    from pathlib import Path
    from unittest.mock import patch

    path = Path(__file__).parents[1] / "migrations/versions/0028_entry_edits.py"
    spec = importlib.util.spec_from_file_location("entry_edits_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "b8c9d0e1f2a3"
    assert module.down_revision == "a7b8c9d0e1f2"
    with patch.object(module.op, "execute") as execute:
        module.upgrade()
        assert "CREATE TABLE entry_edits" in execute.call_args_list[0].args[0]
        module.downgrade()
        assert execute.call_args.args == ("DROP TABLE entry_edits",)


@pytest.mark.asyncio
async def test_edit_entry_normalizes_names_like_write_entry():
    orchestration, _, entry, _ = _entry_editor()
    result = await orchestration._edit_entry({
        "entry_id": 70, "reason": "Correct names",
        "changes": {"name_ru": "А" * 2100, "name_en": ""},
    }, 20)
    assert result["changed"] == ["name_ru", "name_en"]
    assert entry.name_ru == "А" * 2000
    assert entry.name_en is None
