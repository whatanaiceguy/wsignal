from types import SimpleNamespace

import pytest

import wsignal.cli as cli


@pytest.mark.parametrize(
    "argv,expected_shard",
    [
        (["ask", "--resume", "9"], None),
        (["ask", "--resume", "9", "--shard-size", "4"], 4),
    ],
)
def test_resume_cli_passes_optional_limits_to_run_query(monkeypatch, argv, expected_shard):
    received = {}

    async def fake_run_query(query, **kwargs):
        received.update(query=query, **kwargs)
        return SimpleNamespace(
            run_id=9,
            state="finished",
            max_research=25,
            top_n=10,
            summary=None,
        )

    async def no_report(_outcome, _top):
        return None

    monkeypatch.setattr(cli, "run_query", fake_run_query)
    monkeypatch.setattr(cli, "_present", no_report)
    monkeypatch.setattr(cli.sys, "argv", argv)

    assert cli.run() == 0
    assert callable(received.pop("sink"))
    assert received == {
        "query": "",
        "top": None,
        "max_research": None,
        "shard_size": expected_shard,
        "resume_run_id": 9,
        "max_cost_usd": None,
    }
