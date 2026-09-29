import json

SEED_METADATA_KEYS = ("companies", "why", "stage", "trend", "sources")
_SEED_METADATA_MARKER = "seed_metadata:"


def seed_metadata(item: dict) -> dict[str, str]:
    return {
        key: item[key]
        for key in SEED_METADATA_KEYS
        if isinstance(item.get(key), str)
    }


def seed_rationale(label: object, metadata: dict[str, str]) -> str:
    rationale = f"seed {label}"
    if metadata:
        rationale += f"\n{_SEED_METADATA_MARKER} {json.dumps(metadata, ensure_ascii=False)}"
    return rationale


def metadata_from_rationale(rationale: str | None) -> dict[str, str]:
    if not rationale:
        return {}
    for line in rationale.splitlines():
        if line.startswith(_SEED_METADATA_MARKER):
            try:
                value = json.loads(line[len(_SEED_METADATA_MARKER) :].strip())
            except json.JSONDecodeError:
                return {}
            return seed_metadata(value) if isinstance(value, dict) else {}
    return {}


def supplied_claim(metadata: dict[str, str]) -> str:
    if not metadata:
        return ""
    details = "\n".join(f"- {key}: {value}" for key, value in metadata.items())
    return (
        "The claim as supplied: test it, do not trust it; assess the technology at "
        "the scope named here, do not narrow it and do not substitute a neighbouring "
        "technology.\n"
        f"{details}"
    )
