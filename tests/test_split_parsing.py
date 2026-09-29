from wsignal.inference.orchestration import parse_corrections, parse_fields


def test_correction_wrapped_in_prose():
    parsed, error = parse_corrections(
        'Готово. {"fixes": [{"focus": "a", "technology_id": 12}], "merges": [], "keep": []}'
    )
    assert error is None
    assert parsed["fixes"][0]["technology_id"] == 12


def test_full_field_list_is_detected_by_correction_parser():
    parsed, error = parse_corrections('{"fields": [{"focus": "a"}]}')
    assert error is None
    assert isinstance(parsed, list)
    assert parsed[0]["focus"] == "a"


def test_plain_object():
    items, error = parse_fields('{"fields": [{"focus": "a"}, {"focus": "b"}]}')
    assert error is None
    assert [i["focus"] for i in items] == ["a", "b"]


def test_bare_list():
    items, error = parse_fields('[{"focus": "a"}]')
    assert error is None
    assert len(items) == 1


def test_trailing_commas_are_repaired():
    raw = """{
      "fields": [
        {
          "area": "identity",
          "focus": "agent IAM",
          "rationale": "look in tenders",
        },
        {
          "area": "supply chain",
          "focus": "model provenance",
          "rationale": "look in standards",
        },
      ],
    }"""
    items, error = parse_fields(raw)
    assert error is None
    assert [i["focus"] for i in items] == ["agent IAM", "model provenance"]


def test_code_fences_are_stripped():
    items, error = parse_fields('```json\n{"fields": [{"focus": "a"}]}\n```')
    assert error is None
    assert len(items) == 1


def test_prose_around_the_object_is_ignored():
    items, error = parse_fields('Вот список:\n{"fields": [{"focus": "a"}]}\nГотово.')
    assert error is None
    assert len(items) == 1


def test_prose_only_is_a_reported_failure_not_a_field():
    items, error = parse_fields("Я не смог разрезать это направление.")
    assert items == []
    assert error


def test_empty_fields_list_is_a_failure():
    items, error = parse_fields('{"fields": []}')
    assert items == []
    assert error


def test_empty_answer_is_a_failure():
    items, error = parse_fields("")
    assert items == []
    assert error


def test_a_comma_inside_a_string_is_left_alone():
    items, error = parse_fields('{"fields": [{"focus": "a, }b", "rationale": "x, ]y"}]}')
    assert error is None
    assert items[0]["focus"] == "a, }b"
    assert items[0]["rationale"] == "x, ]y"
