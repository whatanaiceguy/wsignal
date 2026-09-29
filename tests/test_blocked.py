from wsignal.parsing.text import html_to_text, looks_blocked

CLOUDFLARE = (
    "<!DOCTYPE html><html><head><title>Just a moment...</title></head>"
    "<body><div id='cf-wrapper'>Enable JavaScript and cookies to continue"
    "</div></body></html>"
)

REAL = (
    "<!DOCTYPE html><html><head><title>Mind raises $72M</title></head><body>"
    "<article><p>Data loss prevention startup Mind Security Inc. today "
    "announced it has raised $72 million in a Series B round led by investors "
    "who have backed the company since its seed stage. The round values the "
    "company at just over half a billion dollars and will fund expansion of "
    "its engineering team through the coming year.</p></article></body></html>"
)


def test_a_challenge_page_is_refused():
    assert looks_blocked(CLOUDFLARE, html_to_text(CLOUDFLARE))


def test_a_real_article_is_not():
    assert looks_blocked(REAL, html_to_text(REAL)) is None


def test_prose_about_cloudflare_is_not_a_challenge_page():
    """The phrases are several words long precisely so an article discussing
    bot protection does not trip the detector."""
    body = REAL.replace(
        "Data loss prevention",
        "Cloudflare and other DDoS vendors compete with this data loss "
        "prevention",
    )
    assert looks_blocked(body, html_to_text(body)) is None


def test_markup_that_renders_to_nothing_is_refused():
    """A client-rendered shell is not a page either, and it does not announce
    itself the way a Cloudflare interstitial does."""
    shell = (
        "<!DOCTYPE html><html><head><title>App</title></head><body>"
        "<div id='root'></div>" + "<div class='x'></div>" * 200 + "</body></html>"
    )
    reason = looks_blocked(shell, html_to_text(shell))
    assert reason and "rendered to" in reason


def test_a_genuinely_short_page_is_not_refused_for_being_short():
    """The length rule needs real markup as well as little text, so a small
    honest page survives it."""
    small = "<html><body><p>Short but real.</p></body></html>"
    assert looks_blocked(small, html_to_text(small)) is None


def test_non_html_is_left_alone():
    """A json or xml body is not markup that failed to render."""
    payload = '{"ok": true}'
    assert looks_blocked(payload, payload) is None
