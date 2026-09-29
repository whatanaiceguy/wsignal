from wsignal.interface.report import signal_bands


def test_bands_split_at_their_edges():
    bands = signal_bands([0.49, 0.5, 0.64, 0.65, 0.75, 0.76, 0.89, 0.9, 1.0])
    assert bands["signals_count"] == 8
    assert bands["probable_count"] == 2
    assert bands["likely_count"] == 4
    assert bands["very_high_count"] == 2


def test_the_tz_counter_is_strictly_above_three_quarters():
    assert signal_bands([0.75])["high_confidence_count"] == 0
    assert signal_bands([0.751, 0.9])["high_confidence_count"] == 2


def test_no_entries_count_nothing():
    assert set(signal_bands([]).values()) == {0}


def test_band_counts_leave_out_entries_the_report_excludes():
    from types import SimpleNamespace

    from wsignal.interface.report import band_scores

    entries = [
        SimpleNamespace(score=0.8, signal_class="weak"),
        SimpleNamespace(score=0.95, signal_class="strong"),
        SimpleNamespace(score=0.9, signal_class="noise"),
        SimpleNamespace(score=0.7, signal_class=None),
    ]
    assert band_scores(entries) == [0.8, 0.7]
    assert signal_bands(band_scores(entries))["high_confidence_count"] == 1
