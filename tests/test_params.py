import pytest

from wsignal.inference import paramcode
from wsignal.inference.params import (
    CODE_PARAMS,
    MODEL_PARAMS,
    PARAMS,
    Extraction,
    ExtractionIncomplete,
    ParamReadingOut,
    blank_evidence_on_null,
    dataset_subject_text,
    extractor_prompt,
    normalise,
)


def _full(**overrides) -> Extraction:
    return Extraction(
        readings=[
            ParamReadingOut(param=p, value=overrides.get(p, 0.0), evidence="x")
            for p in MODEL_PARAMS
        ]
    )


def test_every_param_is_defined_in_the_extractor_prompt():
    prompt = extractor_prompt()
    for name in MODEL_PARAMS:
        assert name in prompt, name


def test_the_counted_params_are_not_asked_of_the_model():
    prompt = extractor_prompt()
    for name in CODE_PARAMS:
        assert f"`{name}`" not in prompt, name


def test_the_prompt_invents_no_params(tmp_path):
    prompt = extractor_prompt()
    backticked = {
        token.strip("`")
        for token in prompt.split()
        if token.startswith("`") and token.endswith("`") and "_" in token
    }
    unknown = {
        name
        for name in backticked
        if name not in PARAMS
        and name not in {"null", "evidence", "quantities", "measured_against"}
        and name not in {"named_neighbour", "named_baseline"}
    }
    assert not unknown, unknown


def test_normalise_preserves_each_model_reading_value_and_evidence():
    expected = {
        param: (index / 100, f"evidence-{index}")
        for index, param in enumerate(MODEL_PARAMS)
    }
    extraction = Extraction(
        readings=[
            ParamReadingOut(param=param, value=value, evidence=evidence)
            for param, (value, evidence) in expected.items()
        ]
    )

    readings = normalise(extraction)

    assert set(readings) == set(MODEL_PARAMS)
    for param, (value, evidence) in expected.items():
        assert readings[param].value == value
        assert readings[param].evidence == evidence


def test_a_missing_param_is_not_silently_nulled():
    partial = Extraction(
        readings=[ParamReadingOut(param=p, value=0.0) for p in MODEL_PARAMS[:-1]]
    )
    with pytest.raises(ExtractionIncomplete):
        normalise(partial)


def test_an_invented_param_is_rejected_rather_than_dropped():
    invented = _full()
    invented.readings.append(ParamReadingOut(param="vibes", value=1.0))
    with pytest.raises(ExtractionIncomplete):
        normalise(invented)


def test_values_outside_the_scale_are_refused():
    with pytest.raises(ValueError):
        ParamReadingOut(param="actor_count", value=1.5)


def test_value_is_rounded_to_two_decimals():
    assert ParamReadingOut(param="actor_count", value=0.123456).value == 0.12


def test_null_carries_no_evidence():
    readings = {
        "actor_count": ParamReadingOut(param="actor_count", value=None, evidence="oops")
    }
    assert blank_evidence_on_null(readings)["actor_count"].evidence == ""


def test_tiering_separates_recognition_from_wires():
    assert paramcode.tier_of("arxiv.org") == 1
    assert paramcode.tier_of("techcrunch.com") == 2
    assert paramcode.tier_of("prnewswire.com") == 3
    assert paramcode.tier_of("some-niche-blog.example") == 0
    assert paramcode.tier_of("cs.stanford.edu") == 1


def test_venue_is_null_without_links():
    """A share needs a denominator, the same rule the values follow."""
    value, effort, _ = paramcode.venue_without_recognition("no links here")
    assert value is None and effort == 0


def test_venue_is_negative_when_recognition_dominates():
    text = "[a](https://arxiv.org/abs/1) [b](https://nature.com/x) [c](https://ieee.org/y)"
    value, effort, _ = paramcode.venue_without_recognition(text)
    assert effort == 3
    assert value is not None and value < 0


def test_venue_is_positive_when_only_wires():
    text = "[a](https://prnewswire.com/1) [b](https://businesswire.com/2)"
    value, _, _ = paramcode.venue_without_recognition(text)
    assert value == 1.0


def test_a_tail_of_unnameable_outlets_counts_as_unrecognised():
    text = "[a](https://eu-startups.com/1) [b](https://pulse2.com/2)"
    value, effort, _note = paramcode.venue_without_recognition(text)
    assert value == 1.0
    assert effort == 2


def test_actor_count_is_null_with_no_companies():
    value, effort, _ = paramcode.actor_count("")
    assert value is None and effort == 0


def test_a_lone_actor_is_weak_substance():
    solo, _, _ = paramcode.actor_count("Keycard")
    handful, _, _ = paramcode.actor_count("Keycard, Cyata, Fabrix Security")
    assert solo is not None and handful is not None
    assert solo < 0 < handful


def test_dataset_text_excludes_the_label():
    row = {
        "n": 1,
        "tech": "T",
        "domain": "D",
        "companies": "C",
        "why": "W",
        "sources": "S",
        "score": "7",
        "stage": "Раннее внедрение",
        "trend": "Растёт быстро",
    }
    row["companies"] = "Keycard, Cyata"
    row["sources"] = "https://prnewswire.com/x"
    text = dataset_subject_text(row)
    assert "W" in text
    assert "Раннее внедрение" not in text
    assert "Растёт быстро" not in text
    assert "7" not in text
    assert "prnewswire" not in text
    assert "Keycard" in text


