from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from wsignal.inference.llm import LlmClient
from wsignal.inference.prompts import prompt_hash, prompts_dir

CODE_PARAMS: tuple[str, ...] = ("actor_count", "venue_without_recognition")

PARAMS: tuple[str, ...] = (
    "no_procurement_category",
    "venue_without_recognition",
    "mainstream_is_the_neighbour",
    "misfiled_domain",
    "bottleneck_not_market",
    "money_over_attention",
    "money_present",
    "funding_trajectory",
    "strategic_money",
    "incumbent_entry",
    "strategic_acquisition",
    "stealth_exits",
    "first_independent_artifact",
    "regulatory_trigger",
    "supply_demand_asymmetry",
    "delivery_gap",
    "reversal",
    "actor_count",
    "perf_vs_baseline",
    "literature_slope",
    "adopter_population",
    "instrument_mismatch",
    "precondition_absent",
)

NON_MONOTONIC = frozenset(
    {"stealth_exits", "first_independent_artifact", "adopter_population"}
)

MODEL_PARAMS: tuple[str, ...] = tuple(p for p in PARAMS if p not in CODE_PARAMS)


class ParamReadingOut(BaseModel):
    param: str
    value: float | None = None
    evidence: str = ""

    @field_validator("value")
    @classmethod
    def _in_range(cls, v: float | None) -> float | None:
        if v is None:
            return None
        if not -1.0 <= v <= 1.0:
            raise ValueError(f"value {v} outside -1.0..1.0")
        return round(v, 2)


class Quantity(BaseModel):
    amount: str
    measured_against: str | None = None


class Extraction(BaseModel):
    readings: list[ParamReadingOut] = Field(default_factory=list)

    quantities: list[Quantity] = Field(default_factory=list)
    named_neighbour: str | None = None
    named_baseline: str | None = None


class ExtractionIncomplete(ValueError):
    pass


def extractor_prompt() -> str:
    return (prompts_dir() / "extractor.md").read_text(encoding="utf-8")


def extractor_sha() -> str:
    return prompt_hash(extractor_prompt())


def normalise(extraction: Extraction) -> dict[str, ParamReadingOut]:
    seen: dict[str, ParamReadingOut] = {}
    unknown: list[str] = []
    for reading in extraction.readings:
        if reading.param not in MODEL_PARAMS:
            unknown.append(reading.param)
            continue
        seen.setdefault(reading.param, reading)

    missing = [p for p in MODEL_PARAMS if p not in seen]
    if missing or unknown:
        raise ExtractionIncomplete(
            f"missing={missing or '-'} unknown={unknown or '-'}"
        )
    return seen


def blank_evidence_on_null(readings: dict[str, ParamReadingOut]) -> dict[str, ParamReadingOut]:
    for reading in readings.values():
        if reading.value is None:
            reading.evidence = ""
    return readings


async def extract(
    client: LlmClient, subject_text: str
) -> tuple[dict[str, ParamReadingOut], Extraction]:
    extraction = await client.structured(
        role="extractor",
        system=extractor_prompt(),
        user=subject_text,
        schema=Extraction,
    )
    return blank_evidence_on_null(normalise(extraction)), extraction


def dataset_subject_text(row: dict) -> str:
    return "\n".join(
        (
            f"Technology: {row.get('tech', '')}",
            f"Domain: {row.get('domain', '')}",
            f"Organisations named: {row.get('companies', '')}",
            "",
            "Why this is a weak signal:",
            str(row.get("why", "")),
        )
    )
