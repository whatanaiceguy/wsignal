import pytest

from wsignal.config import Settings
from wsignal.routes import (
    RELAY_LLM_KEY,
    SERVICES,
    corpus_endpoint,
    llm_endpoint,
    resolve,
    routes,
)

KEYS = {
    "llm_api_key": "sk-own",
    "corpus_url": "https://corpus.own",
    "brave_api_key": "brave-own",
    "epo_consumer_key": "epo",
    "epo_consumer_secret_key": "secret",
    "rospatent_api_key": "rp",
    "x_bearer_token": "x",
}
EMPTY = {name: "" for name in KEYS}


def settings(**values) -> Settings:
    return Settings(_env_file=None, **{"relay_url": "https://relay.test", **EMPTY, **values})


def test_an_empty_env_relays_what_the_relay_serves_and_turns_off_the_rest():
    assert routes(settings()) == {
        "llm": "relay", "corpus": "relay", "brave": "relay",
        "epo": "off", "rospatent": "off", "x": "off",
    }


def test_own_keys_go_direct_in_auto_mode():
    assert set(routes(settings(**KEYS)).values()) == {"direct"}


def test_local_mode_never_touches_the_relay():
    table = routes(settings(mode="local", llm_api_key="sk-own"))
    assert table["llm"] == "direct"
    assert {table[s] for s in SERVICES if s != "llm"} == {"off"}


def test_relay_mode_relays_even_with_a_key_and_keeps_unrelayed_keys_direct():
    table = routes(settings(mode="relay", **KEYS))
    assert table["llm"] == table["corpus"] == table["brave"] == "relay"
    assert table["epo"] == table["rospatent"] == table["x"] == "direct"


@pytest.mark.parametrize(
    "route,mode,keyed,expected",
    [
        ("direct", "auto", True, "direct"),
        ("direct", "relay", True, "direct"),
        ("direct", "auto", False, "off"),
        ("relay", "local", True, "relay"),
        ("relay", "auto", True, "relay"),
        ("", "auto", True, "direct"),
        ("", "auto", False, "relay"),
    ],
)
def test_a_per_service_route_wins_over_mode_and_key(route, mode, keyed, expected):
    values = {"route_llm": route, "mode": mode}
    if keyed:
        values["llm_api_key"] = "sk-own"
    assert resolve("llm", settings(**values)) == expected


def test_empty_environment_values_keep_the_defaults(monkeypatch):
    monkeypatch.setenv("WSIGNAL_LLM_MODEL_ORCHESTRATOR", "")
    monkeypatch.setenv("WSIGNAL_MODE", "")
    monkeypatch.setenv("WSIGNAL_RELAY_URL", "")
    loaded = Settings(_env_file=None)
    assert loaded.llm_model_orchestrator == Settings.model_fields["llm_model_orchestrator"].default
    assert loaded.mode == "auto"
    assert loaded.relay_url == "https://api.1f608.com"


def test_an_empty_relay_url_disables_relaying():
    assert resolve("llm", settings(relay_url="")) == "off"


def test_route_names_are_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("WSIGNAL_MODE", "local")
    monkeypatch.setenv("WSIGNAL_ROUTE_CORPUS", "relay")
    monkeypatch.setenv("WSIGNAL_RELAY_URL", "https://other.relay")
    loaded = Settings(_env_file=None)
    assert loaded.mode == "local"
    assert resolve("corpus", loaded) == "relay"
    assert corpus_endpoint(loaded).base_url == "https://other.relay/v1/corpus"


def test_llm_endpoint_by_route():
    relayed = llm_endpoint(settings())
    assert (relayed.base_url, relayed.key) == ("https://relay.test/v1/llm", RELAY_LLM_KEY)
    own = llm_endpoint(settings(llm_api_key="sk-own"))
    assert (own.base_url, own.key) == ("https://openrouter.ai/api/v1", "sk-own")
    off = llm_endpoint(settings(mode="local"))
    assert off.route == "off" and off.key == ""


def test_corpus_endpoint_by_route():
    assert corpus_endpoint(settings()).base_url == "https://relay.test/v1/corpus"
    assert corpus_endpoint(settings(corpus_url="https://corpus.own/")).base_url == (
        "https://corpus.own/v1/corpus"
    )
    assert corpus_endpoint(settings(mode="local")).base_url == ""


def test_epo_needs_both_halves_of_its_key():
    assert resolve("epo", settings(epo_consumer_key="only-one")) == "off"
