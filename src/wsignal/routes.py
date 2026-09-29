import logging
from dataclasses import dataclass
from typing import Literal

from wsignal.config import Settings, get_settings

log = logging.getLogger("uvicorn.error")

Route = Literal["direct", "relay", "off"]

SERVICES: tuple[str, ...] = ("llm", "corpus", "brave", "epo", "rospatent", "x")
RELAYED: frozenset[str] = frozenset({"llm", "corpus", "brave"})
RELAY_LLM_KEY = "relay"


def has_key(service: str, settings: Settings) -> bool:
    if service == "llm":
        return bool(settings.llm_api_key)
    if service == "corpus":
        return bool(settings.corpus_url)
    if service == "brave":
        return bool(settings.brave_api_key)
    if service == "epo":
        return bool(settings.epo_consumer_key and settings.epo_consumer_secret_key)
    if service == "rospatent":
        return bool(settings.rospatent_api_key)
    if service == "x":
        return bool(settings.x_bearer_token)
    raise ValueError(f"unknown service {service!r}")


def resolve(service: str, settings: Settings | None = None) -> Route:
    settings = settings or get_settings()
    keyed = has_key(service, settings)
    relayable = service in RELAYED and bool(settings.relay_url)
    forced = (getattr(settings, f"route_{service}") or "").strip().lower()
    if forced == "direct":
        return "direct" if keyed else "off"
    if forced == "relay":
        return "relay" if relayable else "off"
    if settings.mode == "local":
        return "direct" if keyed else "off"
    if settings.mode == "relay" and relayable:
        return "relay"
    if keyed:
        return "direct"
    return "relay" if relayable else "off"


def routes(settings: Settings | None = None) -> dict[str, Route]:
    settings = settings or get_settings()
    return {service: resolve(service, settings) for service in SERVICES}


@dataclass(frozen=True)
class Endpoint:
    route: Route
    base_url: str
    key: str


def relay_base(settings: Settings, path: str) -> str:
    return f"{settings.relay_url.rstrip('/')}/v1/{path}"


def llm_endpoint(settings: Settings | None = None) -> Endpoint:
    settings = settings or get_settings()
    route = resolve("llm", settings)
    if route == "relay":
        return Endpoint(route, relay_base(settings, "llm"), RELAY_LLM_KEY)
    if route == "direct":
        return Endpoint(route, settings.llm_base_url.rstrip("/"), settings.llm_api_key)
    return Endpoint(route, settings.llm_base_url.rstrip("/"), "")


def corpus_endpoint(settings: Settings | None = None) -> Endpoint:
    settings = settings or get_settings()
    route = resolve("corpus", settings)
    if route == "relay":
        return Endpoint(route, relay_base(settings, "corpus"), "")
    if route == "direct":
        return Endpoint(route, f"{settings.corpus_url.rstrip('/')}/v1/corpus", "")
    return Endpoint(route, "", "")


def brave_endpoint(direct_url: str, settings: Settings | None = None) -> Endpoint:
    settings = settings or get_settings()
    route = resolve("brave", settings)
    if route == "relay":
        return Endpoint(route, f"{relay_base(settings, 'src/brave')}/web/search", "")
    if route == "direct":
        return Endpoint(route, direct_url, settings.brave_api_key)
    return Endpoint(route, direct_url, "")


def log_routes(settings: Settings | None = None) -> dict[str, Route]:
    settings = settings or get_settings()
    table = routes(settings)
    for service, route in table.items():
        where = settings.relay_url if route == "relay" else (
            "own key" if route == "direct" else "no key, disabled"
        )
        log.info("route %-9s %-6s %s", service, route, where)
    return table
