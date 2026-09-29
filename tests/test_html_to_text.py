from wsignal.inference.verify import quote_appears_in
from wsignal.parsing.text import html_to_text, looks_like_html


def test_plain_text_is_untouched():
    assert html_to_text("just a sentence") == "just a sentence"


def test_xml_is_not_mistaken_for_html():
    feed = '<?xml version="1.0"?><feed><entry><title>A paper</title></entry></feed>'
    assert looks_like_html(feed) is False
    assert html_to_text(feed) == feed


def test_script_and_style_bodies_are_dropped():
    raw = (
        "<html><head><style>.a{color:red}</style></head><body>"
        "<script>var x = 1; alert('hi')</script>"
        "<p>The actual sentence.</p></body></html>"
    )
    text = html_to_text(raw)
    assert "The actual sentence." in text
    assert "color:red" not in text
    assert "alert" not in text


def test_block_tags_become_boundaries():
    text = html_to_text("<html><body><ul><li>alpha</li><li>beta</li></ul></body></html>")
    assert "alphabeta" not in text
    assert "alpha" in text and "beta" in text


def test_entities_are_resolved():
    text = html_to_text("<html><body><p>AT&amp;T said &quot;no&quot;</p></body></html>")
    assert 'AT&T said "no"' in text


def test_a_quote_from_the_rendered_page_now_verifies():
    raw = (
        "<html><body><div class='wrap'><p>Google is announcing\n"
        "   fully-managed, <b>remote</b> MCP servers.</p></div></body></html>"
    )
    text = html_to_text(raw)
    quote = "Google is announcing fully-managed, remote MCP servers."
    assert quote_appears_in(quote, text)
    assert not quote_appears_in(quote, raw)


def test_malformed_markup_still_yields_what_it_parsed():
    text = html_to_text("<html><body><p>kept<<<>>> broken")
    assert "kept" in text


def test_it_actually_shrinks_a_page():
    raw = "<html><body>" + "<div class='nav-item-wrapper'><span>x</span></div>" * 500
    raw += "<p>the one real sentence</p></body></html>"
    text = html_to_text(raw)
    assert "the one real sentence" in text
    assert len(text) < len(raw) / 3
