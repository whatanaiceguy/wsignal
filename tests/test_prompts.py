import pytest

from wsignal.inference.prompts import (
    ROLES,
    load_assistant_lookup_prompt,
    load_prompts,
    prompts_dir,
)


def test_every_role_has_a_prompt_file():
    for role in ROLES:
        assert (prompts_dir() / f"{role}.md").is_file(), role
    assert (prompts_dir() / "assistant_lookup.md").is_file()


def test_assistant_lookup_prompt_is_retrieval_only_and_plain():
    prompt = load_assistant_lookup_prompt()
    assert "only the question asked" in prompt
    assert "only documents retrieved" in prompt
    assert "verbatim quotes" in prompt
    assert "say so plainly" in prompt
    assert "Do not speculate" in prompt


def test_shared_definition_reaches_every_role():
    for role, text in load_prompts().items():
        assert "Nothing rests on model knowledge without confirmed retrieval" in text, role
        assert "{{SHARED}}" not in text, role


def test_pattern_vocabulary_is_identical_across_roles():
    names = ["no_procurement_category", "mainstream_is_the_neighbour", "money_over_attention"]
    for role, text in load_prompts().items():
        for name in names:
            assert name in text, f"{role} is missing {name}"


def test_a_missing_prompt_raises_rather_than_falling_back(monkeypatch, tmp_path):
    load_prompts.cache_clear()
    monkeypatch.setattr("wsignal.inference.prompts.prompts_dir", lambda: tmp_path)
    with pytest.raises(FileNotFoundError):
        load_prompts()
    load_prompts.cache_clear()


def test_review_submissions_use_russian_without_translating_quotes_or_keys():
    prompts = load_prompts()
    refuter = prompts["refuter"]
    researcher = prompts["researcher"]
    assert "Write submitted `claim`, `opposite`, `effect` and `queries_and_venues` in Russian" in (
        refuter
    )
    assert "Keep JSON keys and `verdict` values unchanged" in refuter
    assert "quotes verbatim in the source language" in refuter
    researcher_words = " ".join(researcher.split())
    assert "`attack`, `response` and `changes` fields in Russian" in researcher_words
    assert "Keep JSON keys unchanged" in researcher_words
    assert "quotations verbatim in the source language" in researcher
    for prompt in (refuter, researcher):
        assert "takes precedence over the English" in prompt


def test_the_searching_roles_are_told_where_to_start():
    prompts = load_prompts()
    for role in ("researcher", "refuter"):
        assert "discovery" in prompts[role], role
        assert "list_sources" in prompts[role], role
