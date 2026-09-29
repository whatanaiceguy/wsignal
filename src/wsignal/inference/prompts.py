from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

ROLES = ("orchestrator", "assistant", "researcher", "refuter")
SHARED_MARKER = "{{SHARED}}"


def prompts_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "prompts"


def prompt_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@lru_cache(maxsize=1)
def load_prompts() -> dict[str, str]:
    directory = prompts_dir()
    shared = (directory / "shared.md").read_text(encoding="utf-8")
    prompts: dict[str, str] = {}
    for role in ROLES:
        text = (directory / f"{role}.md").read_text(encoding="utf-8")
        prompts[role] = text.replace(SHARED_MARKER, shared)
    return prompts


@lru_cache(maxsize=1)
def load_assistant_lookup_prompt() -> str:
    return (prompts_dir() / "assistant_lookup.md").read_text(encoding="utf-8")


def describe() -> dict[str, dict[str, str | int]]:
    return {
        role: {"sha256": prompt_hash(text), "chars": len(text)}
        for role, text in load_prompts().items()
    }
