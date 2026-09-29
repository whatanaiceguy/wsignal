from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter

from sqlalchemy import func, select, update

from wsignal.db import get_sessionmaker
from wsignal.models import Document
from wsignal.parsing.base import tier_name
from wsignal.parsing.tiering import host_tier

HOST_DERIVED_TYPES = ("page", "news")


def _scope():
    return Document.source_type.in_(HOST_DERIVED_TYPES)


async def main(dry_run: bool) -> int:
    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(
                select(Document.id, Document.url, Document.source_tier).where(_scope())
            )
        ).all()

        if not rows:
            print("нечего пересчитывать")
            return 0

        moves: dict[int, list[int]] = {}
        before: Counter[int] = Counter()
        after: Counter[int] = Counter()
        for row in rows:
            want = host_tier(row.url)
            before[row.source_tier] += 1
            after[want] += 1
            if want != row.source_tier:
                moves.setdefault(want, []).append(row.id)

        changed = sum(len(ids) for ids in moves.values())
        print(
            f"в области пересчёта: {len(rows):,} документов "
            f"(типы {', '.join(HOST_DERIVED_TYPES)}; статьи и hn не трогаем — "
            "там источник и есть адаптер)"
        )
        print(f"{'тир':<16} {'было':>9} {'станет':>9}")
        for tier in sorted(set(before) | set(after)):
            print(f"{tier_name(tier):<16} {before[tier]:>9,} {after[tier]:>9,}")
        print(f"\nизменится: {changed:,}")

        if dry_run:
            print("--dry-run: ничего не записано")
            return 0
        if not changed:
            return 0

        for tier, ids in moves.items():
            for start in range(0, len(ids), 5000):
                chunk = ids[start : start + 5000]
                await session.execute(
                    update(Document).where(Document.id.in_(chunk)).values(source_tier=tier)
                )
        await session.commit()

        check = dict(
            (
                await session.execute(
                    select(Document.source_tier, func.count())
                    .where(_scope())
                    .group_by(Document.source_tier)
                )
            ).all()
        )
        print("\nзаписано. сейчас в базе:")
        for tier, count in sorted(check.items()):
            print(f"  {tier_name(tier):<16} {count:>9,}")
    return 0


def run() -> int:
    parser = argparse.ArgumentParser(description="Recompute source_tier from the host.")
    parser.add_argument("--dry-run", action="store_true", help="показать и не писать")
    args = parser.parse_args()
    try:
        return asyncio.run(main(args.dry_run))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(run())
