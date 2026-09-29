import pytest

from wsignal.parsing.composite import RRF_K, fuse, normalise_url
from wsignal.parsing.ddg import looks_challenged
from wsignal.parsing.registry import COMPOSITES, SOURCES, adapter_names


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://WWW.Example.com/a/", "https://example.com/a"),
        ("https://example.com/a?utm_source=x&id=7", "https://example.com/a?id=7"),
        ("https://example.com/a#frag", "https://example.com/a"),
        ("https://example.com/", "https://example.com/"),
    ],
)
def test_urls_normalise_to_one_key(raw, expected):
    assert normalise_url(raw) == expected


def test_a_selecting_query_parameter_is_kept():
    one = normalise_url("https://example.com/view?article=1")
    two = normalise_url("https://example.com/view?article=2")
    assert one != two


def test_the_same_page_from_two_sources_becomes_one_record():
    per_source = {
        "ddg": [{"url": "https://example.com/a?utm_source=news", "title": "A"}],
        "store": [{"url": "https://www.example.com/a/", "title": "A"}],
    }
    fused = fuse(per_source, 10)
    assert len(fused) == 1
    assert fused[0]["agreement"] == 2
    assert sorted(fused[0]["found_by"]) == ["ddg", "store"]


def test_agreement_raises_the_score_without_being_the_sort_key_itself():
    both = fuse(
        {
            "a": [{"url": "https://x.test/1"}],
            "b": [{"url": "https://x.test/1"}],
        },
        10,
    )
    assert both[0]["score"] == round(2 / (RRF_K + 1), 5)


def test_a_date_from_any_source_fills_one_that_has_none():
    fused = fuse(
        {
            "ddg": [{"url": "https://x.test/1", "published_at": None}],
            "store": [
                {
                    "url": "https://x.test/1",
                    "published_at": "2026-09-01",
                    "date_type": "announced",
                }
            ],
        },
        10,
    )
    assert fused[0]["published_at"] == "2026-09-01"
    assert fused[0]["date_type"] == "announced"


def test_fusion_is_not_order_dependent():
    a = fuse({"a": [{"url": "https://x.test/1", "title": "from a"}],
              "b": [{"url": "https://x.test/1", "title": "from b"}]}, 5)
    b = fuse({"b": [{"url": "https://x.test/1", "title": "from b"}],
              "a": [{"url": "https://x.test/1", "title": "from a"}]}, 5)
    assert a[0]["title"] == b[0]["title"]


def test_a_record_with_no_url_cannot_enter_the_blend():
    assert fuse({"a": [{"title": "no url"}]}, 5) == []


def test_duckduckgo_challenge_is_not_an_empty_result():
    challenge = (
        "<html><body>Unfortunately, bots use DuckDuckGo too. Please complete "
        "the following challenge</body></html>"
    )
    assert looks_challenged(challenge) is True
    assert looks_challenged('<html><form><input name="q"></form></html>') is False


@pytest.mark.asyncio
async def test_list_sources_describes_every_registered_adapter():
    from wsignal.inference.tools import Toolbox

    listed = await Toolbox(None, None)._list_sources({}, None)
    described = {row["adapter"] for row in listed["sources"]}
    assert described == set(adapter_names())
    for row in listed["sources"]:
        assert row["date_means"] != "unknown", row["adapter"]


def test_composites_have_registered_members_and_are_available_as_adapters():
    known = set(SOURCES) | {"store"}
    available = set(adapter_names())
    for name, members in COMPOSITES.items():
        assert members, f"{name} fans across nothing"
        assert set(members) <= known, name
        assert name in available
