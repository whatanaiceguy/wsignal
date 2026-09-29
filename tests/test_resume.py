from wsignal.inference.pipeline import (
    _limits_from_opening_message,
    _opening_message,
    _resume_message,
    _stored_shard_size,
)


def test_opening_brief_round_trips_limits_and_carries_shard_size():
    message = _opening_message("Q", 7, 15, 4)

    assert _limits_from_opening_message(message) == (7, 15)
    assert "Размер шарда оркестратора: 4 полей" in _resume_message(
        "Q", 7, 15, {}, 4
    )


def test_resume_uses_settings_when_an_old_run_has_no_stored_shard_size():
    run = type("RunStub", (), {"shard_size": None})()

    assert _stored_shard_size(run, None, 7) == 7
    assert run.shard_size == 7


def test_resume_keeps_the_stored_shard_size_unless_overridden():
    run = type("RunStub", (), {"shard_size": 4})()

    assert _stored_shard_size(run, None, 10) == 4
    assert run.shard_size == 4
    assert _stored_shard_size(run, 6, 10) == 6
    assert run.shard_size == 6


def test_historical_run_limits_are_recovered_from_the_opening_message():
    assert _limits_from_opening_message(_opening_message("Q", 7, 15, 4)) == (7, 15)


def test_a_resume_reports_counts_and_agents_waiting_for_replay():
    message = _resume_message(
        "слабые сигналы в области кибербезопасности",
        15,
        15,
        {
            "returns_replayed": 5,
            "entries_already_written": 0,
            "fields_counted_against_the_ceiling": 10,
            "agents_with_nothing_to_replay": [141, 145],
        },
    )
    assert "слабые сигналы в области кибербезопасности" in message
    assert "Возвратов исследователей ждёт в очереди: 5" in message
    assert "Технологий взято в работу: 10 из 15" in message
    assert "141, 145" in message


def test_a_resume_with_nothing_to_replay_still_reads_as_a_sentence():
    message = _resume_message(
        "Q", 15, 15, {"returns_replayed": 0, "entries_already_written": 0}
    )
    assert "Q" in message
    assert "0" in message
    assert "оборвались до ответа" not in message


def test_a_resume_message_tolerates_a_restore_that_returned_nothing():
    message = _resume_message("Q", 15, 15, {})
    assert "Q" in message
    assert message.strip()
    assert "Размер шарда оркестратора: 4 полей" in _resume_message(
        "Q", 15, 15, {}, 4
    )
