from types import SimpleNamespace

import pytest

from wsignal.inference.verify import quote_appears_in, verify_run

PAGE = (
    "Cloud infrastructure providers like Amazon, Google, and Microsoft\n"
    "introduced   TEEs as part of their infrastructure offerings. "
    "The category’s first independent audits appeared in 2026 — late."
)


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class _VerificationSession:
    def __init__(self, rows):
        self.rows = rows
        self.statement = None
        self.commits = 0

    async def execute(self, statement):
        self.statement = statement
        return _Result(self.rows)

    async def commit(self):
        self.commits += 1


def test_exact_span_verifies():
    assert quote_appears_in("introduced TEEs as part of their infrastructure offerings", PAGE)


def test_line_breaks_and_runs_of_spaces_do_not_break_it():
    assert quote_appears_in("Microsoft\nintroduced      TEEs", PAGE)


def test_curly_and_straight_quotes_are_the_same_character():
    assert quote_appears_in("The category's first independent audits", PAGE)


def test_em_dash_folds_to_hyphen():
    assert quote_appears_in("appeared in 2026 - late", PAGE)


def test_a_quote_that_is_not_there_fails():
    assert not quote_appears_in("introduced TEEs in 2019", PAGE)


def test_ellipsis_fragments_match_in_order():
    page = (
        "First fragment: this is a distinctive statement from the page. Middle text omitted. "
        "Second fragment: this is another distinctive statement."
    )
    quote = (
        "this is a distinctive statement from the page ... "
        "this is another distinctive statement"
    )

    assert quote_appears_in(quote, page)


def test_all_supported_ellipsis_shapes_split_fragments():
    page = (
        "A sufficiently long first sentence occurs here, followed much later by "
        "a sufficiently long second sentence."
    )

    for ellipsis in ("...", "…", "[...]", "(...)"):
        quote = (
            f"A sufficiently long first sentence occurs here, {ellipsis} "
            "a sufficiently long second sentence"
        )
        assert quote_appears_in(quote, page)


def test_punctuation_spacing_is_tolerated():
    page = "This report describes a pioneer in ai gateways, and the work continues."
    quote = "This report describes a pioneer in ai gateways , and the work continues."

    assert quote_appears_in(quote, page)


def test_absent_closing_quote_is_tolerated():
    page = "The company said its platform was designed for businesses across the world."
    quote = "The company said its platform was designed for businesses across the world.”"

    assert quote_appears_in(quote, page)


def test_small_character_differences_are_tolerated():
    page = "The service supports production deployments across multiple cloud environments."
    quote = "The service supports production deploymints across multiple cloud environments."

    assert quote_appears_in(quote, page)


def test_exact_figures_are_required():
    page = "The company invested $300 million in the project, which expanded significantly."
    quote = "The company invested $30 million in the project, which expanded significantly."

    assert not quote_appears_in(quote, page)


def test_ellipsis_fragments_must_be_in_page_order():
    page = (
        "First distinctive sentence appears later. After many words, "
        "the second distinctive sentence appears earlier."
    )
    quote = (
        "Second distinctive sentence appears earlier ... "
        "First distinctive sentence appears later"
    )

    assert not quote_appears_in(quote, page)


def test_missing_fragment_fails():
    quote = (
        "A distinctive phrase that appears on this page ... "
        "a second phrase that is not here at all"
    )

    assert not quote_appears_in(quote, PAGE)


def test_quote_with_only_short_fragments_fails():
    assert not quote_appears_in("short fragment text ... and more short text", PAGE)


def test_exact_substring_still_passes():
    assert quote_appears_in("introduced TEEs as part of their infrastructure offerings", PAGE)


def test_wrapping_quote_marks_are_stripped_from_fragments():
    quote = '“introduced TEEs as part of their infrastructure offerings”'

    assert quote_appears_in(quote, PAGE)


def test_similarity_above_threshold_does_not_allow_changed_number():
    page = "The project created 300 jobs in the region and supported local companies."
    quote = "The project created 301 jobs in the region and supported local companies."

    assert not quote_appears_in(quote, page)


def test_russian_quote_shapes_are_supported():
    page = "Компания сообщила о запуске нового сервиса и расширении производства для клиентов."
    quote = "Компания сообщила о запуске нового сервиса и расширении произврдства для клиентов."

    assert quote_appears_in(quote, page)


def test_a_number_elsewhere_on_the_page_does_not_rescue_a_changed_figure():
    page = (
        "The company invested $300 million in the project, which expanded significantly. "
        "Separately, 30 engineers joined."
    )
    quote = "The company invested $30 million in the project, which expanded significantly."

    assert not quote_appears_in(quote, page)


def test_numbers_on_both_sides_of_an_ellipsis_stay_separate():
    page = (
        "The market reached 2019 levels again this spring after a long decline. "
        "Later in the year, 300 companies had signed up for the programme."
    )
    quote = "The market reached 2019 levels again ... 300 companies had signed up"

    assert quote_appears_in(quote, page)


def test_exact_text_inside_a_longer_number_does_not_verify():
    page = "Revenue grew to $130 million in the quarter, driven by enterprise demand."
    quote = "30 million in the quarter, driven by enterprise demand"

    assert not quote_appears_in(quote, page)

def test_quote_similarity_setting_defaults_to_ninety_percent():
    from wsignal.config import Settings

    assert Settings().citation_quote_similarity_threshold == 0.90


def test_quote_appears_in_is_fast_for_long_pages():
    import time

    quote = "The company described an important advancement in technology " + "".join(
        f"in sector {index} " for index in range(20)
    )
    quote = quote[:400].ljust(400, "x")
    fragment = quote[:220] + "z" + quote[221:]
    content = "x" * 9000 + fragment + "y" * (20000 - 9000 - len(fragment))
    started = time.perf_counter()

    assert quote_appears_in(quote, content)

    elapsed = time.perf_counter() - started
    print(
        f"quote verification: {elapsed:.6f}s for 20,000-character page "
        f"and {len(quote)}-character quote"
    )
    assert elapsed < 1.0


def test_empty_quote_never_verifies():
    assert not quote_appears_in("", PAGE)
    assert not quote_appears_in("   ", PAGE)


@pytest.mark.asyncio
async def test_verify_run_updates_and_counts_citations_for_only_the_requested_run():
    good = SimpleNamespace(
        quote="introduced TEEs as part of their infrastructure offerings",
        verified=False,
        verified_at=None,
    )
    bad = SimpleNamespace(quote="not present", verified=True, verified_at=None)
    session = _VerificationSession([(good, PAGE), (bad, PAGE)])

    counts = await verify_run(session, 7)

    assert counts == {"checked": 2, "verified": 1, "failed": 1}
    assert good.verified is True and good.verified_at is not None
    assert bad.verified is False and bad.verified_at is not None
    assert session.commits == 1
    sql = str(session.statement)
    assert "entries.run_id" in sql and "= :run_id_1" in sql
    assert session.statement.compile().params["run_id_1"] == 7
    assert "citations.entry_id" in sql and "documents.id" in sql
