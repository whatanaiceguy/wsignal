import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

import wsignal.cli as cli
from wsignal.inference.orchestration import Orchestration
from wsignal.inference.pipeline import (
    _create_seed_fields,
    _existing_run,
    _record_orchestrator_budget,
    _seeded_opening_message,
    _seeded_resume_fields,
    _seeded_resume_message,
    run_query,
)
from wsignal.inference.tools import Toolbox
from wsignal.models import Alias, Field, Run, Technology


class _SeedSession:
    def __init__(self):
        self.technologies = {}
        self.aliases = {}
        self.added = []
        self.flushed = set()
        self.next_ids = {Technology: 1, Field: 1, Alias: 1}

    async def scalar(self, statement):
        params = statement.compile().params
        name = params.get("canonical_name_ru_1")
        technology = self.technologies.get(name)
        if technology is not None:
            return technology
        surface = params.get("lower_1")
        if surface is None:
            surface = params.get("lower_2")
        lang = params.get("lang_1") or params.get("lang_2")
        if surface is not None:
            return self.aliases.get((str(surface).strip("% ").casefold(), lang))
        return None

    async def execute(self, statement):
        self.statement = statement
        params = statement.compile().params
        terms = [
            value.strip("% ").casefold()
            for key, value in params.items()
            if key.startswith("lower_") and isinstance(value, str)
        ]
        matched = []
        for alias in self.aliases.values():
            if any(term in alias.surface_form.casefold() for term in terms):
                technology = next(
                    (
                        tech
                        for tech in self.technologies.values()
                        if tech.id == alias.technology_id
                    ),
                    None,
                )
                matched.append((technology, alias.surface_form))
        return SimpleNamespace(all=lambda: matched)

    def add(self, item):
        self.added.append(item)

    async def flush(self):
        for item in self.added:
            if id(item) in self.flushed:
                continue
            if getattr(item, "id", None) is None:
                model = type(item)
                item.id = self.next_ids[model]
                self.next_ids[model] += 1
            if isinstance(item, Technology):
                self.technologies[item.canonical_name_ru] = item
            if isinstance(item, Alias):
                self.aliases[(item.surface_form.casefold(), item.lang)] = item
            self.flushed.add(id(item))


class _Noop:
    def __init__(self):
        self.models_used = {}
        self.stats = SimpleNamespace(requests=0)

    async def aclose(self):
        return None


class _Toolbox:
    def __init__(self):
        self.registered = []
        self._technology_opener = None

    def register(self, schema, _handler, _roles):
        self.registered.append(schema["function"]["name"])


class _RunSession:
    def __init__(self, run):
        self.run = run

    async def get(self, _model, _identity):
        return self.run


class _ResumeFieldsSession:
    def __init__(self, fields):
        self.fields = fields
        self.statement = None

    async def scalars(self, statement):
        self.statement = statement
        return SimpleNamespace(all=lambda: self.fields)


@pytest.mark.asyncio
async def test_seed_fields_reuse_existing_technology_id():
    session = _SeedSession()
    existing = Technology(id=41, canonical_name_ru="Технология А")
    session.technologies[existing.canonical_name_ru] = existing
    run = SimpleNamespace(id=7)

    await _create_seed_fields(
        session,
        run,
        [{"n": 1, "tech": "Технология А", "domain": "область А"}],
    )

    stored = [item for item in session.added if isinstance(item, Field)]
    assert len(stored) == 1
    assert stored[0].technology_id == existing.id
    assert stored[0].rationale == "seed 1"
    assert [item for item in session.added if isinstance(item, Technology)] == []
    assert any(
        isinstance(item, Alias) and item.surface_form == "Технология А"
        for item in session.added
    )


@pytest.mark.asyncio
async def test_seed_fields_register_short_russian_and_latin_aliases():
    session = _SeedSession()
    title = "Тиринг KV-кэша: выгрузка контекста в DRAM/NVMe/параллельные ФС"

    await _create_seed_fields(
        session,
        SimpleNamespace(id=7),
        [{"n": 1, "tech": title, "domain": "Инфраструктура ИИ"}],
    )

    aliases = [item for item in session.added if isinstance(item, Alias)]
    forms = {(alias.surface_form, alias.lang, alias.technology_id) for alias in aliases}
    assert ("Тиринг KV-кэша", "ru", 1) in forms
    assert not any(lang == "en" for _surface, lang, _id in forms)
    assert all(alias.technology_id == 1 for alias in aliases)

    for query in ("Тиринг KV-кэша",):
        result = await Toolbox(session, None)._known_technologies({"name": query}, None)
        assert result["matches"]
        assert result["matches"][0]["technology_id"] == 1


@pytest.mark.asyncio
async def test_known_technology_matching_includes_prefix_and_partial_like_clauses():
    session = _SeedSession()
    toolbox = Toolbox(session, None)

    await toolbox._known_technologies({"name": "KV cache"}, None)

    sql = str(session.statement.compile(dialect=postgresql.dialect()))
    assert " LIKE " in sql
    assert "similarity(" in sql
    assert "ILIKE" not in sql


@pytest.mark.asyncio
async def test_seed_fields_carry_only_allowed_metadata_and_keep_seed_label():
    session = _SeedSession()
    run = SimpleNamespace(id=7)
    fields = await _create_seed_fields(
        session,
        run,
        [
            {
                "n": 17,
                "tech": "Технология А",
                "domain": "область А",
                "companies": "Компания А",
                "why": "Причина supplied",
                "stage": "Пилот",
                "trend": "Растёт",
                "sources": "[Источник](https://example.com)",
                "score": 0.9,
                "stage_lvl": 4,
                "trend_lvl": 3,
                "pred": 7,
            },
            {
                "n": "B",
                "tech": "Технология Б",
                "domain": "область Б",
                "why": "Вторая причина",
            },
        ],
    )

    stored = [item for item in session.added if isinstance(item, Field)]
    assert [field["focus"] for field in fields] == ["Технология А", "Технология Б"]
    assert fields[0]["metadata"] == {
        "companies": "Компания А",
        "why": "Причина supplied",
        "stage": "Пилот",
        "trend": "Растёт",
        "sources": "[Источник](https://example.com)",
    }
    assert fields[1]["metadata"] == {"why": "Вторая причина"}
    assert [field.rationale.startswith("seed 17") for field in stored] == [True, False]
    assert stored[1].rationale.startswith("seed B")
    assert [field.ordinal for field in stored] == [1, 2]
    assert [field.state for field in stored] == ["pending", "pending"]
    assert [field.technology_id for field in stored] == [1, 2]
    assert all(
        key not in str(vars(field))
        for field in stored
        for key in ("score", "stage_lvl", "trend_lvl", "pred")
    )
    assert '"companies": "Компания А"' in stored[0].rationale
    assert "[Источник](https://example.com)" in stored[0].rationale
    assert '"why": "Вторая причина"' in stored[1].rationale


@pytest.mark.asyncio
async def test_seeded_resume_fields_include_dispatched_fields_without_entries():
    fields = [
        SimpleNamespace(
            id=11,
            focus="Технология А",
            technology_id=101,
            state="dispatched",
            rationale='seed 1\nseed_metadata: {"why": "Сохранённая причина"}',
        ),
        SimpleNamespace(
            id=12, focus="Технология Б", technology_id=102, state="pending", rationale="seed 2"
        ),
    ]
    session = _ResumeFieldsSession(fields)

    result = await _seeded_resume_fields(session, 7)
    message = _seeded_resume_message("Seeded run", 3, 2, {}, 10, result)

    assert result == [
        {
            "id": 11,
            "focus": "Технология А",
            "technology_id": 101,
            "metadata": {"why": "Сохранённая причина"},
        },
        {"id": 12, "focus": "Технология Б", "technology_id": 102, "metadata": {}},
    ]
    assert "field_id=11 technology_id=101: Технология А" in message
    assert "why: Сохранённая причина" in message
    assert "field_id=12 technology_id=102: Технология Б" in message
    assert "pending fields" not in message
    assert "поля без записей" in message
    compiled = str(
        session.statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "entries.field_id = fields.id" in compiled
    assert "fields.state =" not in compiled


def test_seed_opening_lists_fields_and_directs_shards_dispatch_refutation_and_writes():
    message = _seeded_opening_message(
        "Seeded run from signals.json",
        3,
        2,
        2,
        [
            {
                "id": 11,
                "focus": "Технология А",
                "technology_id": 101,
                "metadata": {
                    "companies": "Компания А",
                    "why": "Причина",
                    "stage": "Пилот",
                    "trend": "Растёт",
                    "sources": "[Источник](https://example.com)",
                },
            },
            {"id": 12, "focus": "Технология Б", "technology_id": 102},
            {"id": 13, "focus": "Технология В", "technology_id": 103},
        ],
    )

    assert "field_id=11 technology_id=101: Технология А" in message
    assert "field_id=12 technology_id=102: Технология Б" in message
    assert "field_id=13 technology_id=103: Технология В" in message
    assert "The claim as supplied: test it, do not trust it" in message
    assert "do not narrow it and do not substitute a neighbouring technology" in message
    assert "companies: Компания А" in message
    assert "[Источник](https://example.com)" in message
    resume = _seeded_resume_message(
        "Seeded run", 3, 2, {}, 2,
        [
            {
                "id": 11,
                "focus": "Технология А",
                "technology_id": 101,
                "metadata": {"why": "Причина resume"},
            }
        ],
    )
    assert "field_id=11 technology_id=101: Технология А" in resume
    assert "why: Причина resume" in resume
    assert "summon_orchestrators" in message
    assert "split_direction" in message
    assert "open_technology" in message
    assert "refute" in message
    assert "noise" in message and "insufficient" in message


@pytest.mark.asyncio
async def test_seeded_researcher_brief_carries_claim_metadata():
    from wsignal.inference.seeded import seed_rationale

    field = SimpleNamespace(
        id=5,
        run_id=9,
        focus="Технология",
        rationale=seed_rationale(23, {"why": "Supplied rationale", "stage": "Pilot"}),
        state="pending",
        orchestrator_agent_id=None,
        agent_id=None,
    )
    orchestration = Orchestration(
        session=None,
        sessionmaker=None,
        runtime=None,
        llm=None,
        fetcher=None,
        run=SimpleNamespace(id=9, seeded=True),
        prompts={"researcher": "system"},
        max_research=1,
        shard_size=10,
    )
    session = _DispatchSession(field)
    orchestration._session = session
    orchestration._runtime = _BriefRuntime()

    async def researcher_task(*_args):
        return None

    orchestration._researcher_task = researcher_task
    result = await orchestration._dispatch_researchers(
        {"field_ids": [5], "brief": "Review."}, 4
    )
    await asyncio.gather(*orchestration._inflight.values())

    assert result["dispatched"][0]["field_id"] == 5
    brief = orchestration._runtime.created[0]["brief"]
    assert "The claim as supplied: test it, do not trust it" in brief
    assert "why: Supplied rationale" in brief
    assert "stage: Pilot" in brief
    assert "do not narrow it" in brief


class _DispatchSession:
    def __init__(self, field):
        self.field = field
        self.created = []

    async def scalars(self, _statement):
        return SimpleNamespace(all=lambda: [self.field])

    async def scalar(self, _statement):
        return None

    async def commit(self):
        return None


class _BriefRuntime:
    async def create(self, **kwargs):
        self.created.append(kwargs)
        return SimpleNamespace(id=77)

    def __init__(self):
        self.created = []


@pytest.mark.asyncio
async def test_split_is_unavailable_after_loading_a_seeded_run():
    restored_run = Run(id=8, query="Seeded run from seed.json", seeded=True)
    loaded = await _existing_run(_RunSession(restored_run), 8)
    orchestration = Orchestration(
        session=None,
        sessionmaker=None,
        runtime=None,
        llm=None,
        fetcher=None,
        run=loaded,
        prompts={},
    )
    toolbox = _Toolbox()

    orchestration.install(toolbox)

    assert "split_direction" not in toolbox.registered
    assert await toolbox._technology_opener({}, 1) == {
        "error": "open_technology is unavailable on a seeded run"
    }
    assert await orchestration._split_direction({}, 1) == {
        "error": "split_direction is unavailable on a seeded run"
    }


def test_seed_cli_ignores_untrusted_keys_and_defaults_max_to_seed_count(monkeypatch, tmp_path):
    seed_path = tmp_path / "signals.json"
    seed_path.write_text(
        json.dumps(
            [
                {
                    "n": 1,
                    "tech": "Технология А",
                    "domain": "область А",
                    "companies": "Компания А",
                    "why": "Причина",
                    "stage": "Пилот",
                    "trend": "Растёт",
                    "sources": "[Источник](https://example.com)",
                    "score": 0.99,
                    "stage_lvl": 4,
                    "trend_lvl": 3,
                    "pred": 7,
                },
                {"n": 2, "tech": "Технология Б", "domain": "область Б"},
            ]
        ),
        encoding="utf-8",
    )
    received = {}

    async def fake_main(query, top, max_research, **kwargs):
        received.update(query=query, top=top, max_research=max_research, **kwargs)
        return 0

    monkeypatch.setattr(cli, "main", fake_main)
    monkeypatch.setattr(cli, "_present", lambda *_args: None)
    monkeypatch.setattr(
        cli,
        "get_settings",
        lambda: SimpleNamespace(top_n=9, max_research=30, shard_size=4, debug=False),
    )
    monkeypatch.setattr(cli.sys, "argv", ["ask", "--seed", str(seed_path)])

    assert cli.run() == 0
    assert received == {
        "query": "",
        "top": 9,
        "max_research": 2,
        "shard_size": 4,
        "seed": [
            {
                "n": 1,
                "tech": "Технология А",
                "domain": "область А",
                "companies": "Компания А",
                "why": "Причина",
                "stage": "Пилот",
                "trend": "Растёт",
                "sources": "[Источник](https://example.com)",
            },
            {"n": 2, "tech": "Технология Б", "domain": "область Б"},
        ],
        "seed_filename": "signals.json",
        "max_cost_usd": None,
    }
    assert "score" not in str(received)
    assert "stage_lvl" not in str(received)
    assert "trend_lvl" not in str(received)
    assert "pred" not in str(received)


def test_resume_keeps_recorded_orchestrator_budget_or_records_missing_value():
    existing = SimpleNamespace(orchestrator_max_steps=200)
    missing = SimpleNamespace(orchestrator_max_steps=None)

    assert _record_orchestrator_budget(existing, 300) == 200
    assert existing.orchestrator_max_steps == 200
    assert _record_orchestrator_budget(missing, 300) == 300
    assert missing.orchestrator_max_steps == 300


def test_seeded_run_persists_seed_marker():
    run = Run(query="Seeded run from x.json", max_research=2, top_n=2, seeded=True)

    assert run.seeded is True
    assert Run.__table__.c.seeded.nullable is False
    assert Run.__table__.c.seeded.server_default.arg.text == "false"
    assert Run.__table__.c.orchestrator_max_steps.nullable is True


@pytest.mark.asyncio
async def test_seed_count_sets_the_default_research_ceiling(monkeypatch):
    class Settings:
        agent_max_steps = 5
        top_n = 2
        max_research = 30
        shard_size = 10
        run_budget_minutes = 10
        orchestrator_max_steps = 5
        heartbeat_s = 100
        heartbeat_write_s = 100

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def commit(self):
            return None

        async def execute(self, _statement):
            return None

    class Runtime:
        def __init__(self, *_args):
            pass

        async def create_run(self, _query, max_research, _top, shard_size=None):
            captured["run_max"] = max_research
            captured["shard_size"] = shard_size
            run = SimpleNamespace(id=3, seeded=False)
            captured["run"] = run
            return run

        async def create(self, **_kwargs):
            return SimpleNamespace(id=4)

        async def commit(self):
            return None

    class Sink:
        async def flush(self):
            return None

        async def drain(self):
            return None

    class Fleet:
        def __init__(self, **kwargs):
            orchestration = kwargs["orchestration"]
            captured["fleet_max"] = orchestration._max_research
            captured["seeded"] = orchestration._run.seeded
            captured["split_registered"] = "split_direction" in toolbox.registered

        async def start(self, _agent, message):
            captured["opening"] = message

        async def wait(self):
            return None, 0, "answered"

        async def shutdown(self):
            return None

    captured = {}
    session = Session()
    monkeypatch.setattr("wsignal.inference.pipeline.get_settings", lambda: Settings())
    monkeypatch.setattr(
        "wsignal.inference.pipeline.load_prompts",
        lambda: {"orchestrator": "system"},
    )
    monkeypatch.setattr("wsignal.inference.pipeline.LlmClient", _Noop)
    monkeypatch.setattr("wsignal.inference.pipeline.Fetcher", _Noop)
    monkeypatch.setattr("wsignal.inference.pipeline.RetrievalCache", _Noop)
    monkeypatch.setattr("wsignal.inference.pipeline.Toolbox", lambda *_args: toolbox)
    toolbox = _Toolbox()
    monkeypatch.setattr("wsignal.inference.pipeline.AgentRuntime", Runtime)
    monkeypatch.setattr("wsignal.inference.pipeline.OrchestratorFleet", Fleet)
    monkeypatch.setattr("wsignal.inference.pipeline.wrap_sink", lambda *_args, **_kwargs: Sink())
    async def create_fields(_session, _run, _seeds):
        return [
            {
                "id": 10,
                "focus": "A",
                "technology_id": 100,
                "metadata": {"why": "Supplied rationale"},
            },
            {"id": 11, "focus": "B", "technology_id": 101, "metadata": {}},
        ]

    monkeypatch.setattr("wsignal.inference.pipeline._create_seed_fields", create_fields)
    monkeypatch.setattr(
        "wsignal.inference.pipeline.verify_run",
        lambda *_args: asyncio.sleep(
            0, result={"checked": 0, "verified": 0, "failed": 0}
        ),
    )
    monkeypatch.setattr(
        "wsignal.inference.pipeline.per_run", lambda *_args: asyncio.sleep(0, result={})
    )
    monkeypatch.setattr(
        "wsignal.inference.pipeline.per_agent", lambda *_args: asyncio.sleep(0, result=[])
    )
    monkeypatch.setattr(
        "wsignal.inference.pipeline.shutdown_page_processor",
        lambda: asyncio.sleep(0),
    )
    monkeypatch.setattr("wsignal.inference.pipeline.describe", lambda: {})
    monkeypatch.setattr(
        "wsignal.inference.pipeline.datetime",
        SimpleNamespace(now=lambda _tz: SimpleNamespace()),
    )
    monkeypatch.setattr(
        "wsignal.inference.pipeline.get_sessionmaker", lambda: lambda: session
    )
    monkeypatch.setattr("wsignal.inference.pipeline.emit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "wsignal.inference.pipeline.datetime",
        SimpleNamespace(now=lambda _tz: SimpleNamespace()),
    )

    outcome = await run_query(
        "",
        top=2,
        sessionmaker=lambda: session,
        seed=[
            {"n": 1, "tech": "A", "domain": "D"},
            {"n": 2, "tech": "B", "domain": "D"},
        ],
        seed_filename="seed.json",
    )

    assert captured["run_max"] == captured["fleet_max"] == 2
    assert captured["run"].orchestrator_max_steps == Settings.orchestrator_max_steps
    assert captured["seeded"] is True
    assert captured["split_registered"] is False
    assert "field_id=10 technology_id=100: A" in captured["opening"]
    assert "The claim as supplied" in captured["opening"]
    assert "why: Supplied rationale" in captured["opening"]
    assert "stage_lvl" not in captured["opening"]
    assert outcome.max_research == 2
