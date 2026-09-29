from wsignal.inference.tools import empty_result_note


def test_a_source_that_answered_nothing_is_a_measurement():
    note = empty_result_note(["openalex", "hn"], {}, {})
    assert "measurement across 2 source(s) that answered" in note
    assert "NOT A MEASUREMENT" not in note


def test_nothing_asked_is_not_a_measurement():
    note = empty_result_note([], {}, {"brave": "automatic allowance spent (3/3)"})
    assert note.startswith("NOT A MEASUREMENT")
    assert "not the source" in note
    assert "source(s) that answered" not in note


def test_a_spent_budget_is_never_counted_as_a_venue_that_answered():
    note = empty_result_note([], {}, {"brave": "automatic allowance spent (1/1)"})
    assert "1 that did not" not in note
    assert "were not asked at all" in note


def test_a_real_failure_beside_a_real_answer_keeps_both():
    note = empty_result_note(["openalex"], {"ddg": "challenged"}, {"brave": "spent"})
    assert "1 source(s) that answered" in note
    assert "1 that failed" in note
    assert "1 never asked" in note
