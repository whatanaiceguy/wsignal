import asyncio
from types import SimpleNamespace

import pytest

import wsignal.inference.pipeline as pipeline


@pytest.mark.asyncio
async def test_cancelling_run_marks_its_durable_state_failed(monkeypatch):
    run = SimpleNamespace(
        id=29,
        state="running",
        finished_at=None,
        seeded=False,
        orchestrator_max_steps=None,
    )
    entered_wait = asyncio.Event()
    shutdown_called = False

    class Session:
        def __init__(self):
            self.commits = 0
            self.agents_failed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            pass

        async def commit(self):
            self.commits += 1

        async def rollback(self):
            pass

        async def get(self, _model, _identity):
            return run

        async def scalars(self, _statement):
            return SimpleNamespace(all=lambda: [])

        async def execute(self, _statement):
            self.agents_failed = True
            return SimpleNamespace()

    class Client:
        stats = SimpleNamespace(requests=0)

        async def aclose(self):
            pass

    class Runtime:
        def __init__(self, *_args):
            pass

        async def create_run(self, *_args, **_kwargs):
            return run

        async def create(self, **_kwargs):
            return SimpleNamespace(id=30)

    class Orchestration:
        def __init__(self, **_kwargs):
            pass

        def install(self, _toolbox):
            pass

    class Fleet:
        def __init__(self, **_kwargs):
            pass

        async def start(self, _agent, _opening):
            pass

        async def wait(self):
            entered_wait.set()
            await asyncio.Event().wait()

        async def shutdown(self):
            nonlocal shutdown_called
            shutdown_called = True

    class Sink:
        def __call__(self, _event):
            pass

        async def flush(self):
            pass

        async def drain(self):
            pass

    async def heartbeat(*_args, **_kwargs):
        await asyncio.Event().wait()

    async def noop():
        pass

    settings = SimpleNamespace(
        agent_max_steps=5,
        top_n=2,
        max_research=1,
        shard_size=1,
        orchestrator_max_steps=5,
        run_budget_minutes=10,
        heartbeat_s=15,
        heartbeat_write_s=3,
    )
    session = Session()
    monkeypatch.setattr(pipeline, "get_settings", lambda: settings)
    monkeypatch.setattr(pipeline, "load_prompts", lambda: {"orchestrator": "system"})
    monkeypatch.setattr(pipeline, "LlmClient", Client)
    monkeypatch.setattr(pipeline, "Fetcher", Client)
    monkeypatch.setattr(pipeline, "RetrievalCache", lambda: object())
    monkeypatch.setattr(pipeline, "Toolbox", lambda *_args: object())
    monkeypatch.setattr(pipeline, "AgentRuntime", Runtime)
    monkeypatch.setattr(pipeline, "Orchestration", Orchestration)
    monkeypatch.setattr(pipeline, "OrchestratorFleet", Fleet)
    monkeypatch.setattr(pipeline, "wrap_sink", lambda *_args, **_kwargs: Sink())
    monkeypatch.setattr(pipeline, "_heartbeat", heartbeat)
    monkeypatch.setattr(pipeline, "shutdown_page_processor", noop)

    task = asyncio.create_task(pipeline.run_query("test", sessionmaker=lambda: session))
    await entered_wait.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert shutdown_called is True
    assert run.state == "failed"
    assert run.finished_at is not None
    assert session.agents_failed is True
