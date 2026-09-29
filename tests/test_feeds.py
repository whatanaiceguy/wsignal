import json

import pytest

from wsignal.parsing.feeds import looks_like_feed, parse_feed
from wsignal.parsing.hosts import load_register, pollable_hosts

RSS2 = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <title>SiliconANGLE</title>
    <item>
      <title>Mind raises $72M to stop data leaking into AI</title>
      <link>https://siliconangle.com/2026/09/17/mind-raises-72m/</link>
      <pubDate>Wed, 17 Sep 2026 18:04:11 +0000</pubDate>
      <description>Data loss prevention startup Mind Security Inc. &lt;b&gt;today&lt;/b&gt;
      announced it has raised $72 million.</description>
    </item>
    <item>
      <title>An item with no link at all</title>
      <pubDate>Wed, 17 Sep 2026 10:00:00 +0000</pubDate>
    </item>
  </channel>
</rss>
"""

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Example</title>
  <entry>
    <title>Figure unveils Helix 2.5</title>
    <link rel="edit" href="https://example.com/edit/1"/>
    <link rel="alternate" href="https://theaiinsider.tech/2026/09/17/helix/"/>
    <published>2026-09-17T09:30:00Z</published>
    <summary>Figure has introduced Helix 2.5.</summary>
  </entry>
</feed>
"""

RDF = """<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns="http://purl.org/rss/1.0/"
         xmlns:dc="http://purl.org/dc/elements/1.1/">
  <item rdf:about="https://example.org/story-1">
    <title>An RDF item</title>
    <link>https://example.org/story-1</link>
    <dc:date>2026-09-16T00:00:00Z</dc:date>
    <description>Old school.</description>
  </item>
</rdf:RDF>
"""


def test_rss2_item_becomes_a_dated_lead():
    documents = parse_feed(RSS2, "siliconangle.com")
    assert len(documents) == 1, "the item with no link must be dropped, not guessed at"
    document = documents[0]
    assert document.url == "https://siliconangle.com/2026/09/17/mind-raises-72m/"
    assert document.published_at.isoformat() == "2026-09-17"
    assert "<b>" not in document.text
    assert "raised $72 million" in document.text
    assert document.source_name == "siliconangle.com"


def test_atom_prefers_the_alternate_link_over_the_first_one():
    """Taking the first <link> would store the edit endpoint as the article."""
    documents = parse_feed(ATOM, "theaiinsider.tech")
    assert len(documents) == 1
    assert documents[0].url == "https://theaiinsider.tech/2026/09/17/helix/"
    assert documents[0].published_at.isoformat() == "2026-09-17"


def test_rdf_parses_and_reads_a_dc_date():
    documents = parse_feed(RDF, "example.org")
    assert len(documents) == 1
    assert documents[0].url == "https://example.org/story-1"
    assert documents[0].published_at.isoformat() == "2026-09-16"


@pytest.mark.parametrize(
    "body",
    ["", "not xml at all", "<rss><channel>", "<html><body>403</body></html>"],
)
def test_broken_input_yields_nothing_instead_of_raising(body):
    """One broken publisher must not end a poll over eighty-six of them."""
    assert parse_feed(body, "example.com") == []


def test_an_oversized_feed_is_refused_before_parsing():
    assert parse_feed("<rss>" + "x" * 6_000_000 + "</rss>", "example.com") == []


def test_items_are_leads_and_declarative():
    for body, host in ((RSS2, "siliconangle.com"), (ATOM, "theaiinsider.tech")):
        for document in parse_feed(body, host):
            assert document.retrieved is False
            assert document.adapter == "feeds"
            assert document.may_create_technology is True


def test_a_block_page_is_not_a_feed_even_on_a_200():
    """nature.com serves the container exactly this while serving Windows RSS.

    Without the check, being blocked and publishing nothing are the same
    observation, and every absence claim built on a poll inherits the confusion.
    """
    blocked = '<!DOCTYPE html>\n<html lang="en"><head><meta http-equiv="CSP">'
    assert looks_like_feed(blocked) is False
    assert parse_feed(blocked, "nature.com") == []
    for good in (RSS2, ATOM, RDF):
        assert looks_like_feed(good) is True


def test_register_is_well_formed_and_every_pollable_host_has_a_feed():
    entries = load_register()
    assert len(entries) > 100
    for entry in entries:
        assert entry.host == entry.host.lower().removeprefix("www.")
        assert entry.source in ("dataset", "manual")
        if entry.poll:
            assert entry.feed, f"{entry.host} is set to poll with no feed url"
    assert all(e.feed for e in pollable_hosts())


def test_a_host_listed_twice_is_an_error_not_a_silent_merge(tmp_path):
    path = tmp_path / "hosts.json"
    path.write_text(
        json.dumps({"hosts": [{"host": "a.com"}, {"host": "www.a.com"}]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="twice"):
        load_register(path)


def test_adding_a_host_by_hand_needs_only_two_fields(tmp_path):
    """The register is meant to be edited, so omitting `poll` must do the
    obvious thing rather than silently not polling."""
    path = tmp_path / "hosts.json"
    path.write_text(
        json.dumps({"hosts": [{"host": "new.example", "feed": "https://new/rss"}]}),
        encoding="utf-8",
    )
    entry = load_register(path)[0]
    assert entry.pollable is True
    assert entry.source == "manual"
