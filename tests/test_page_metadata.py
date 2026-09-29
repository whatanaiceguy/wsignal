from datetime import date

import pytest

from wsignal.parsing.text import page_metadata

WIRED = """<!doctype html><html lang="en">
<head>
<title>Character.AI CEO on Chatbots and Entertainment | WIRED</title>
<meta property="og:site_name" content="WIRED">
<meta property="og:title" content="Character.AI's CEO Says Chatbots Are Entertainment">
<meta property="article:published_time" content="2025-01-14T12:00:00.000Z">
</head><body><p>Text.</p></body></html>"""

REVERSED_ATTRS = """<html lang="ru"><head>
<meta content="2026-03-04" name="date">
<meta content="Хабр" property="og:site_name">
<title>Заголовок</title></head><body>x</body></html>"""

JSONLD = """<html><head><title>A post</title>
<script type="application/ld+json">
{"@type":"Article","datePublished":"2024-11-22T09:30:00Z"}
</script></head><body>x</body></html>"""

TIME_TAG = """<html><head><title>A post</title></head>
<body><time datetime="2023-07-01">July</time></body></html>"""

BARE = "<html><head></head><body><p>nothing declared</p></body></html>"


def test_the_publisher_name_and_title_come_off_the_page():
    meta = page_metadata(WIRED)
    assert meta["site_name"] == "WIRED"
    assert meta["title"] == "Character.AI's CEO Says Chatbots Are Entertainment"
    assert meta["lang"] == "en"
    assert meta["published_at"] == date(2025, 1, 14)


def test_meta_attributes_in_either_order():
    meta = page_metadata(REVERSED_ATTRS)
    assert meta["site_name"] == "Хабр"
    assert meta["lang"] == "ru"
    assert meta["published_at"] == date(2026, 3, 4)


def test_the_title_falls_back_to_the_title_tag():
    assert page_metadata(JSONLD)["title"] == "A post"


def test_jsonld_date_source_is_recorded():
    result = page_metadata(JSONLD)
    assert result["published_at"] == date(2024, 11, 22)
    assert result["date_source"] == "jsonld"


def test_a_time_inside_an_article_is_a_qualifying_date():
    raw = '<html><body><article><time datetime="2023-07-01">July</time></article></body></html>'
    result = page_metadata(raw)
    assert result["published_at"] == date(2023, 7, 1)
    assert result["date_source"] == "time"


def test_meta_date_precedes_jsonld_and_is_labeled_meta():
    raw = (
        '<html><head><meta name="date" content="2025-06-18">'
        '<script>{"datePublished":"2025-06-19"}</script></head><body></body></html>'
    )
    result = page_metadata(raw)
    assert result["published_at"] == date(2025, 6, 18)
    assert result["date_source"] == "meta"


def test_itemprop_and_publish_date_marked_times_are_qualifying():
    itemprop = (
        '<html><body><time itemprop="datePublished" datetime="2023-07-01">'
        "</time></body></html>"
    )
    marked = '<html><body><time class="post-date" datetime="2023-07-02"></time></body></html>'
    assert page_metadata(itemprop)["published_at"] == date(2023, 7, 1)
    assert page_metadata(marked)["published_at"] == date(2023, 7, 2)


def test_unmarked_time_widget_is_not_a_published_date():
    result = page_metadata(TIME_TAG)
    assert result["published_at"] is None
    assert result["date_source"] is None


def test_url_date_is_used_when_sidebar_times_are_outside_article():
    raw = (
        '<html><body><time datetime="2026-09-18"></time>'
        '<aside><time datetime="2026-09-17"></time></aside></body></html>'
    )
    result = page_metadata(raw, "https://example.test/2025/06/19/story/")
    assert result["published_at"] == date(2025, 6, 19)
    assert result["date_source"] == "url"


def test_url_date_is_fallback_without_any_qualifying_time():
    result = page_metadata(BARE, "https://example.test/story/2025-06-19")
    assert result["published_at"] == date(2025, 6, 19)
    assert result["date_source"] == "url"


@pytest.mark.parametrize(
    "url",
    [
        "https://example.test/2025/02/30/story/",
        "https://example.test/20250230/story/",
        f"https://example.test/{date.today().year + 1}/01/01/story/",
    ],
)
def test_impossible_and_future_url_dates_are_ignored(url):
    result = page_metadata(BARE, url)
    assert result["published_at"] is None
    assert result["date_source"] is None


def test_far_url_date_conflict_prefers_url_to_meta_date():
    raw = (
        '<html><head><meta property="article:published_time" content="2025-06-20">'
        "</head><body></body></html>"
    )
    result = page_metadata(raw, "https://example.test/2025/06/01/story/")
    assert result["published_at"] == date(2025, 6, 1)
    assert result["date_source"] == "url"


def test_near_url_date_conflict_keeps_meta_date():
    raw = (
        '<html><head><meta property="article:published_time" content="2025-06-20">'
        "</head><body></body></html>"
    )
    result = page_metadata(raw, "https://example.test/2025/06/19/story/")
    assert result["published_at"] == date(2025, 6, 20)
    assert result["date_source"] == "meta"


def test_modified_time_is_not_a_published_date():
    raw = (
        '<html><head><meta property="article:modified_time" content="2025-06-19">'
        "</head><body></body></html>"
    )
    assert page_metadata(raw)["published_at"] is None


def test_meta_jsonld_time_and_url_dates_after_today_are_rejected():
    future = date.today().year + 1
    raw = (
        f'<html><head><meta name="date" content="{future}-01-01">'
        f'<script>{{"datePublished":"{future}-01-01"}}</script></head>'
        f'<body><article><time datetime="{future}-01-01"></time></article></body></html>'
    )
    result = page_metadata(raw, f"https://example.test/{future}/01/01/story/")
    assert result["published_at"] is None
    assert result["date_source"] is None


def test_a_page_that_declares_nothing_returns_nothing():
    meta = page_metadata(BARE)
    assert meta["site_name"] == ""
    assert meta["lang"] == ""
    assert meta["published_at"] is None
    assert meta["date_source"] is None


def test_something_that_is_not_html_has_no_metadata():
    assert page_metadata("plain text from a keyed json api") == {}
    assert page_metadata("") == {}


@pytest.mark.parametrize(
    "raw",
    [
        '<html><head><meta name="date" content="not a date"></head><body>x</body></html>',
        '<html><head><meta name="date" content="14/01/2025"></head><body>x</body></html>',
        '<html><head><meta name="date" content="1776-07-04"></head><body>x</body></html>',
    ],
)
def test_an_unparseable_or_implausible_date_is_left_absent(raw):
    assert page_metadata(raw)["published_at"] is None


def test_a_locale_is_reduced_to_a_two_letter_language():
    raw = '<html><head><meta property="og:locale" content="en_GB"></head><body>x</body></html>'
    assert page_metadata(raw)["lang"] == "en"


def test_the_html_lang_attribute_is_the_last_resort():
    raw = '<html lang="de"><head><title>t</title></head><body>x</body></html>'
    assert page_metadata(raw)["lang"] == "de"
