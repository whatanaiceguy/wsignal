import asyncio
from datetime import UTC, datetime

import pytest

from tests.helpers_api import FakeSession, FakeSessionmaker
from wsignal.inference.events import wrap_sink
from wsignal.interface.jobs import RunAlreadyActive, RunSlot, iter_events
from wsignal.models import RunEvent


@pytest.mark.asyncio
async def test_persisted_sink_logs_failed_event_persistence(caplog):
    class FailedSession(FakeSession):
        async def flush(self):
            raise RuntimeError("insert failed")

    class FailedMaker:
        def __call__(self):
            return FailedSession()

    sink = wrap_sink(None, FailedMaker())
    sink({"kind": "run_started", "run_id": 3})
    await sink.drain()
    assert sink.failed
    errors = [record for record in caplog.records if record.levelname == "ERROR"]
    assert errors
    assert any(
        record.exc_info
        and record.exc_info[0] is RuntimeError
        and str(record.exc_info[1]) == "insert failed"
        for record in errors
    )


@pytest.mark.asyncio
async def test_persisted_sink_forwards_buffers_and_skips_heartbeat():
    stored = []
    maker = FakeSessionmaker()
    original_factory = maker.__call__

    def factory():
        session = original_factory()
        stored.append(session)
        return session

    maker = factory
    received = []
    sink = wrap_sink(received.append, maker)
    sink({"kind": "agent_created", "agent_id": 3})
    sink({"kind": "heartbeat", "elapsed_s": 2})
    sink({"kind": "run_started", "run_id": 9, "query": "q"})
    sink({"kind": "verified", "count": 1})
    await sink.drain()
    assert received == [
        {"kind": "agent_created", "agent_id": 3},
        {"kind": "heartbeat", "elapsed_s": 2},
        {"kind": "run_started", "run_id": 9, "query": "q"},
        {"kind": "verified", "count": 1},
    ]
    persisted = [row for session in stored for row in session.added]
    assert [row.kind for row in persisted] == ["agent_created", "run_started", "verified"]
    assert [row.run_id for row in persisted] == [9, 9, 9]


@pytest.mark.asyncio
async def test_run_slot_refuses_second_job_then_accepts_after_completion(monkeypatch):
    import wsignal.interface.jobs as jobs

    release = asyncio.Event()
    next_id = 40

    async def fake_run_query(_query, on_run_id, **_kwargs):
        nonlocal next_id
        next_id += 1
        on_run_id(next_id)
        await release.wait()

    monkeypatch.setattr(jobs, "run_query", fake_run_query)
    slot = RunSlot()
    assert await slot.start("first") == 41
    with pytest.raises(RunAlreadyActive):
        await slot.start("second")
    release.set()
    await slot.task
    release.clear()
    assert await slot.start("third") == 42
    release.set()
    await slot.task


@pytest.mark.asyncio
async def test_run_slot_logs_background_failure_after_run_id(monkeypatch, caplog):
    import wsignal.interface.jobs as jobs

    async def failed_run_query(_query, on_run_id, **_kwargs):
        on_run_id(42)
        raise ValueError("background failed")

    monkeypatch.setattr(jobs, "run_query", failed_run_query)
    slot = RunSlot()
    assert await slot.start("query") == 42
    with pytest.raises(ValueError, match="background failed"):
        await slot.task
    errors = [record for record in caplog.records if record.levelname == "ERROR"]
    assert errors
    assert any(
        record.exc_info
        and record.exc_info[0] is ValueError
        and str(record.exc_info[1]) == "background failed"
        for record in errors
    )


@pytest.mark.asyncio
async def test_run_slot_raises_when_task_fails_before_run_id(monkeypatch):
    import wsignal.interface.jobs as jobs

    async def failed_run_query(*_args, **_kwargs):
        raise ValueError("startup failed")

    monkeypatch.setattr(jobs, "run_query", failed_run_query)
    with pytest.raises(ValueError, match="startup failed"):
        await RunSlot().start("query")


@pytest.mark.asyncio
async def test_resume_orphan_does_nothing_when_disabled(monkeypatch):
    import wsignal.interface.jobs as jobs

    settings = type("Settings", (), {"resume_on_startup": False})()
    monkeypatch.setattr(jobs, "get_settings", lambda: settings)

    def unexpected_sessionmaker():
        raise AssertionError("disabled orphan resume must not inspect the database")

    monkeypatch.setattr(jobs, "get_sessionmaker", unexpected_sessionmaker)
    slot = RunSlot()
    assert await slot.resume_orphan() is None
    assert slot.task is None


@pytest.mark.asyncio
async def test_event_replay_drains_multiple_batches_in_order():
    rows = [
        RunEvent(
            id=event_id,
            run_id=5,
            ts=datetime(2026, 1, 1, tzinfo=UTC),
            kind="update",
            payload={"n": event_id},
        )
        for event_id in range(1, 502)
    ]
    maker = FakeSessionmaker(rows=rows, state="finished")

    output = [part async for part in iter_events(maker, 5, after=0, poll_interval=0)]

    events = output[:-1]
    assert len(events) == 501
    assert [int(event.splitlines()[0].removeprefix("id: ")) for event in events] == list(
        range(1, 502)
    )
    assert output[-1] == 'event: end\ndata: {"state": "finished"}\n\n'


@pytest.mark.asyncio
async def test_event_replay_respects_cursor_order_and_sends_end():
    rows = [
        RunEvent(id=2, run_id=5, ts=datetime(2026, 1, 1, tzinfo=UTC), kind="two", payload={"n": 2}),
        RunEvent(
            id=4,
            run_id=5,
            ts=datetime(2026, 1, 2, tzinfo=UTC),
            kind="four",
            payload={"n": 4},
        ),
    ]
    maker = FakeSessionmaker(rows=rows, state="finished")
    output = [part async for part in iter_events(maker, 5, after=0, poll_interval=0)]
    assert output[0].startswith("id: 2\nevent: two\n")
    assert output[1].startswith("id: 4\nevent: four\n")
    assert '"n": 4' in output[1]
    assert output[2] == 'event: end\ndata: {"state": "finished"}\n\n'
    resumed = [part async for part in iter_events(maker, 5, after=2, poll_interval=0)]
    assert resumed[0].startswith("id: 4\nevent: four\n")


@pytest.mark.asyncio
async def test_reaper_marks_only_runs_whose_heartbeat_did_not_move(monkeypatch):
    import wsignal.interface.jobs as jobs

    old = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
    snapshots = iter([
        {1: old, 2: old, 3: None, 4: old},
        {1: old, 2: datetime(2026, 9, 27, 12, 1, tzinfo=UTC), 3: None},
    ])

    async def heartbeats(_maker):
        return next(snapshots)

    marked = []

    async def mark(_maker, run_id, reason):
        marked.append((run_id, reason))

    monkeypatch.setattr(jobs, "running_heartbeats", heartbeats)
    monkeypatch.setattr(jobs, "mark_dead_run", mark)
    slot = RunSlot()
    assert await slot.reap_dead_runs(0, sessionmaker=object()) == [1, 3]
    assert marked == [(1, jobs.API_RESTART_REASON), (3, jobs.API_RESTART_REASON)]


@pytest.mark.asyncio
async def test_reaper_spares_the_run_this_process_is_driving(monkeypatch):
    import wsignal.interface.jobs as jobs

    old = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)

    async def heartbeats(_maker):
        return {7: old}

    async def mark(_maker, run_id, reason):
        raise AssertionError("the live run must not be marked")

    monkeypatch.setattr(jobs, "running_heartbeats", heartbeats)
    monkeypatch.setattr(jobs, "mark_dead_run", mark)
    slot = RunSlot()
    slot.run_id = 7
    slot.task = asyncio.create_task(asyncio.sleep(10))
    try:
        assert await slot.reap_dead_runs(0, sessionmaker=object()) == []
    finally:
        slot.task.cancel()


@pytest.mark.asyncio
async def test_reaper_does_not_wait_when_nothing_is_running(monkeypatch):
    import wsignal.interface.jobs as jobs

    async def heartbeats(_maker):
        return {}

    async def no_sleep(_seconds):
        raise AssertionError("nothing to reap, nothing to wait for")

    monkeypatch.setattr(jobs, "running_heartbeats", heartbeats)
    monkeypatch.setattr(jobs.asyncio, "sleep", no_sleep)
    assert await RunSlot().reap_dead_runs(45, sessionmaker=object()) == []
