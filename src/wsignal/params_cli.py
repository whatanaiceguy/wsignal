from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import delete, select

from wsignal.db import get_sessionmaker
from wsignal.inference import paramcode
from wsignal.inference.llm import LlmClient
from wsignal.inference.params import (
    CODE_PARAMS,
    PARAMS,
    Extraction,
    ParamReadingOut,
    dataset_subject_text,
    extract,
    extractor_sha,
)
from wsignal.models import Entry, ParamExtra, ParamReading

DATASET_HINT = (
    "pass --dataset PATH to the organisers' signals100.json; it is not in the "
    "repository. Through scripts/params.sh, put it under docs/reference/ and "
    "pass --dataset docs/reference/signals100.json"
)


@dataclass(slots=True)
class Subject:
    ref: str
    text: str
    effort: int | None
    code: dict[str, ParamReadingOut] = field(default_factory=dict)


def load_dataset(path: Path, limit: int | None) -> list[dict]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    return rows[:limit] if limit else rows


def dataset_code_params(row: dict) -> dict[str, ParamReadingOut]:
    out: dict[str, ParamReadingOut] = {}
    value, _n, note = paramcode.actor_count(row.get("companies", ""))
    out["actor_count"] = ParamReadingOut(
        param="actor_count", value=value, evidence=note
    )
    value, _n, note = paramcode.venue_without_recognition(row.get("sources", ""))
    out["venue_without_recognition"] = ParamReadingOut(
        param="venue_without_recognition", value=value, evidence=note
    )
    return out


def entry_code_params() -> dict[str, ParamReadingOut]:
    return {
        name: ParamReadingOut(param=name, value=None, evidence="")
        for name in CODE_PARAMS
    }


def entry_subject_text(entry: Entry) -> str:
    parts = [
        f"Technology: {entry.name_ru}",
        f"Transition: {entry.transition_ru}",
        "",
        "Why this is a weak signal:",
        entry.why_ru,
    ]
    for label, value in (
        ("Current state:", entry.current_state_ru),
        ("Dynamics:", entry.dynamics_ru),
        ("What would refute it:", entry.what_would_refute_ru),
    ):
        if value:
            parts += ["", label, value]
    return "\n".join(parts)


async def collect_subjects(
    kind: str, limit: int | None, dataset: Path | None = None
) -> list[Subject]:
    if kind == "dataset":
        if dataset is None:
            raise ValueError(f"--kind dataset needs a dataset file: {DATASET_HINT}")
        return [
            Subject(
                ref=str(r["n"]),
                text=dataset_subject_text(r),
                effort=None,
                code=dataset_code_params(r),
            )
            for r in load_dataset(dataset, limit)
        ]

    async with get_sessionmaker()() as session:
        stmt = select(Entry).order_by(Entry.id)
        if limit:
            stmt = stmt.limit(limit)
        entries = (await session.scalars(stmt)).all()
    return [
        Subject(
            ref=str(e.id),
            text=entry_subject_text(e),
            effort=(e.searches_run or 0) + (e.sources_checked or 0),
            code=entry_code_params(),
        )
        for e in entries
    ]


async def write_subject(
    kind: str,
    subject: Subject,
    label: str,
    readings: dict[str, ParamReadingOut],
    extraction: Extraction,
    model: str,
    sha: str,
) -> None:
    async with get_sessionmaker()() as session:
        for table in (ParamReading, ParamExtra):
            await session.execute(
                delete(table).where(
                    table.subject_kind == kind,
                    table.subject_ref == subject.ref,
                    table.pass_label == label,
                )
            )
        session.add_all(
            [
                ParamReading(
                    subject_kind=kind,
                    subject_ref=subject.ref,
                    pass_label=label,
                    param=name,
                    value=readings[name].value,
                    effort=subject.effort,
                    evidence=readings[name].evidence or None,
                    model=model,
                    prompt_sha=sha,
                )
                for name in PARAMS
            ]
        )
        session.add(
            ParamExtra(
                subject_kind=kind,
                subject_ref=subject.ref,
                pass_label=label,
                payload={
                    "quantities": [q.model_dump() for q in extraction.quantities],
                    "named_neighbour": extraction.named_neighbour,
                    "named_baseline": extraction.named_baseline,
                },
                model=model,
                prompt_sha=sha,
            )
        )
        await session.commit()


async def main(
    kind: str,
    label: str,
    limit: int | None,
    concurrency: int,
    dataset: Path | None = None,
) -> int:
    subjects = await collect_subjects(kind, limit, dataset)
    if not subjects:
        print(f"no {kind} subjects found")
        return 1

    client = LlmClient()
    sha = extractor_sha()
    model = client.model_for("extractor")
    print(f"{len(subjects)} {kind} subjects · {model} · prompt {sha} · pass {label}")

    gate = asyncio.Semaphore(concurrency)
    done = 0
    failed: list[tuple[str, str]] = []
    non_null: list[int] = []
    started = time.monotonic()

    async def one(subject: Subject) -> None:
        nonlocal done
        async with gate:
            note = ""
            try:
                model_readings, extraction = await extract(client, subject.text)
                readings = {**model_readings, **subject.code}
                await write_subject(
                    kind, subject, label, readings, extraction, model, sha
                )
                filled = sum(1 for r in readings.values() if r.value is not None)
                non_null.append(filled)
                note = f"ok · {filled}/{len(PARAMS)} non-null"
            except Exception as exc:
                failed.append((subject.ref, f"{type(exc).__name__}: {exc}"))
                note = f"FAILED {type(exc).__name__}"
            done += 1
            print(f"  [{done}/{len(subjects)}] {subject.ref} {note}", flush=True)

    await asyncio.gather(*(one(s) for s in subjects))
    await client.aclose()

    elapsed = time.monotonic() - started
    written = len(subjects) - len(failed)
    print(
        f"\n{written}/{len(subjects)} written in {elapsed:.0f}s · "
        f"{client.log.total_calls} calls · "
        f"{client.log.prompt_tokens} prompt + {client.log.completion_tokens} completion tokens"
    )
    if non_null:
        print(
            f"non-null per subject: min {min(non_null)} · "
            f"mean {sum(non_null) / len(non_null):.1f} · max {max(non_null)}"
        )
    for ref, error in failed:
        print(f"  FAILED {ref}: {error}")
    return 0 if not failed else 2


def run() -> int:
    parser = argparse.ArgumentParser(description="Extract params over a population.")
    parser.add_argument("--kind", choices=("dataset", "entry"), default="entry")
    parser.add_argument(
        "--dataset",
        type=Path,
        default=None,
        metavar="PATH",
        help="The organisers' signals100.json, required by --kind dataset. "
        "It is not in the repository.",
    )
    parser.add_argument(
        "--pass-label",
        default="p1",
        help="Names this pass. Two passes over identical input measure the "
        "extractor's own resolution, which nothing downstream can assume.",
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--concurrency", type=int, default=6)
    args = parser.parse_args()
    if args.kind == "dataset":
        if args.dataset is None:
            parser.error(f"--kind dataset needs a dataset file: {DATASET_HINT}")
        if not args.dataset.is_file():
            parser.error(f"dataset file not found: {args.dataset}; {DATASET_HINT}")
    elif args.dataset is not None:
        parser.error("--dataset applies only to --kind dataset")

    try:
        return asyncio.run(
            main(
                args.kind,
                args.pass_label,
                args.limit,
                args.concurrency,
                args.dataset,
            )
        )
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(run())
