import threading
import time

from wsignal.cli import Live


def _started(query):
    return {"kind": "run_started", "run_id": 1, "query": query}


def test_live_queues_without_blocking_and_reports_drops_in_order(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    written = []
    live = Live(max_lines=1, drain_timeout=1.0)
    monkeypatch.setattr(live, "_t", lambda: "time")

    def write(line):
        if line.endswith(": first"):
            entered.set()
            release.wait(2.0)
        written.append(line)

    live._write = write
    try:
        live(_started("first"))
        assert entered.wait(1.0)
        live(_started("second"))
        live(_started("dropped one"))
        live(_started("dropped two"))
    finally:
        release.set()
        live.close()

    assert written[0].endswith(": first")
    assert written[1].endswith(": second")
    reports = [line for line in written if "пропущено строк вывода: 2" in line]
    assert len(reports) == 1
    assert len([line for line in written if "пропущено строк вывода" in line]) == 1


def test_live_close_is_bounded_when_writer_is_stuck(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    live = Live(max_lines=1, drain_timeout=0.05)
    monkeypatch.setattr(live, "_t", lambda: "time")

    def write(line):
        entered.set()
        release.wait(2.0)

    live._write = write
    live(_started("blocked"))
    assert entered.wait(1.0)
    started = time.monotonic()
    live.close()
    elapsed = time.monotonic() - started
    release.set()
    live._writer.join(1.0)

    assert elapsed < 0.5
    assert not live._writer.is_alive()
