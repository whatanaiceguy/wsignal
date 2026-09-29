from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import select

from wsignal.db import get_sessionmaker
from wsignal.interface.report import assemble, save
from wsignal.models import Run


async def _latest() -> int | None:
    async with get_sessionmaker()() as session:
        return await session.scalar(select(Run.id).order_by(Run.id.desc()).limit(1))


async def _list() -> int:
    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(
                select(Run.id, Run.state, Run.started_at, Run.query)
                .order_by(Run.id.desc())
                .limit(30)
            )
        ).all()
    if not rows:
        print("запусков нет")
        return 1
    for r in rows:
        started = r.started_at.strftime("%Y-%m-%d %H:%M") if r.started_at else "?"
        print(f"{r.id:>5}  {r.state:<9} {started}  {r.query[:52]}")
    return 0


async def main(args: argparse.Namespace) -> int:
    if args.list:
        return await _list()

    run_id = args.run_id
    if run_id is None:
        run_id = await _latest()
        if run_id is None:
            print("запусков нет")
            return 1

    async with get_sessionmaker()() as session:
        try:
            response, text = await assemble(session, run_id, top=args.top, full=args.full)
        except LookupError as exc:
            print(exc)
            return 1

    print(text)

    if args.no_save:
        return 0
    try:
        written = save(
            run_id,
            text,
            response,
            out_dir=args.out,
            suffix="-full" if args.full else "",
        )
    except OSError as exc:
        print(f"\n(не записалось: {exc})")
        return 0
    for path in written:
        print(f"записано  {path}")
    return 0


def run() -> int:
    parser = argparse.ArgumentParser(description="Render a finished run from the store.")
    parser.add_argument("run_id", nargs="?", type=int, help="номер запуска; без него — последний")
    parser.add_argument(
        "--full",
        action="store_true",
        help="добавить служебное: стоимость, агенты, контексты, source_events",
    )
    parser.add_argument("--top", type=int, default=None, help="где провести черту исключения")
    parser.add_argument("--out", default=None, help="куда писать; по умолчанию WSIGNAL_REPORT_DIR")
    parser.add_argument("--no-save", action="store_true", help="только вывод в консоль")
    parser.add_argument("--list", action="store_true", help="показать последние запуски")
    args = parser.parse_args()

    try:
        return asyncio.run(main(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(run())
