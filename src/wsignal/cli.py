from __future__ import annotations

import argparse
import asyncio
import json
import queue
import signal
import sys
import threading
import time
import traceback
from pathlib import Path

from wsignal.config import get_settings
from wsignal.db import get_sessionmaker
from wsignal.inference.accounting import context_sources, per_source
from wsignal.inference.pipeline import RunOutcome, run_query
from wsignal.interface.report import (
    assemble,
    field_counts,
    render_meta,
    save,
    unbacked_patterns,
)

SEED_METADATA_KEYS = ("companies", "why", "stage", "trend", "sources")

ROLE_TAG = {
    "orchestrator": "ОРК",
    "assistant": "АСС",
    "researcher": "ИСС",
    "refuter": "КРИТ",
}


class Live:
    _STOP = object()

    def __init__(self, max_lines: int = 2000, drain_timeout: float = 3.0) -> None:
        self.started = time.monotonic()
        self.spent = 0.0
        self._queue: queue.Queue[str | object] = queue.Queue(maxsize=max_lines)
        self._drain_timeout = drain_timeout
        self._dropped = 0
        self._drop_lock = threading.Lock()
        self._closed = False
        self._writer = threading.Thread(target=self._write_loop, daemon=True)
        self._writer.start()

    def _t(self) -> str:
        return f"{time.monotonic() - self.started:6.1f}s"

    def _write(self, line: str) -> None:
        print(line, flush=True)

    def _write_loop(self) -> None:
        while True:
            line = self._queue.get()
            try:
                if line is self._STOP:
                    return
                self._write(line)
                if self._queue.empty():
                    with self._drop_lock:
                        dropped = self._dropped
                        self._dropped = 0
                    if dropped:
                        self._write(f"·· пропущено строк вывода: {dropped}")
            finally:
                self._queue.task_done()

    def __call__(self, event: dict) -> None:
        kind = event.get("kind")
        tag = ROLE_TAG.get(event.get("role", ""), "   ")
        agent = event.get("agent_id")
        who = f"{tag}#{agent}" if agent else tag
        lines: list[str] = []

        if kind == "run_started":
            lines.append(f"{self._t()}  запуск {event['run_id']}: {event['query']}")
        elif kind == "agent_created":
            field = f"  — {event['field']}" if event.get("field") else ""
            lines.append(
                f"{self._t()}  + {ROLE_TAG.get(event['role'], event['role'])}"
                f"#{event['agent_id']} {event['model']}{field}"
            )
        elif kind == "model_call_started":
            lines.append(
                f"{self._t()}  {who:<10} шаг {event['step']:<2} …думает "
                f"({event['messages']} сообщений)"
            )
        elif kind == "heartbeat":
            lines.append(f"{self._t()}  ·· живы: {event['inflight']} агентов в работе")
        elif kind == "model_call":
            self.spent += event.get("cost_usd") or 0.0
            cached = event.get("cached_tokens") or 0
            prompt = event.get("prompt_tokens") or 0
            pct = f"{100.0 * cached / prompt:.0f}%" if prompt else "-"
            wants = ",".join(event.get("tool_calls") or []) or "ответ"
            lines.append(
                f"{self._t()}  {who:<10} шаг {event['step']:<2} "
                f"{event['duration_s']:>5}s  кэш {pct:>4}  "
                f"${self.spent:.4f}  → {wants}"
            )
        elif kind == "tool_done":
            arg = (
                event.get("arguments", {}).get("query")
                or event.get("arguments", {}).get("url")
                or event.get("arguments", {}).get("name")
                or ""
            )
            lines.append(
                f"{self._t()}  {who:<10}   {event['name']}({str(arg)[:44]}) "
                f"{event['duration_s']}s  {event.get('summary', '')}"
            )
        elif kind == "fields_written":
            lines.append(f"{self._t()}  полей записано: {event['count']}")
            lines.extend(
                f"            · {f.get('focus', '')[:70]}" for f in event.get("fields", [])
            )
        elif kind == "researcher_returned":
            lines.append(
                f"{self._t()}  ← ИСС#{event['agent_id']} {event['stopped']}, "
                f"{event['searches']} запросов по {event['sources']} площадкам, "
                f"{event['chars']} символов"
            )
        elif kind == "refuter_returned":
            lines.append(
                f"{self._t()}  ← КРИТ#{event['agent_id']} против ИСС#{event['attacked']} "
                f"{event['stopped']}, {event['searches']} запросов, "
                f"{event['chars']} символов"
            )
        elif kind == "rebuttal_returned":
            lines.append(
                f"{self._t()}  ← ИСС#{event['agent_id']} ответил критику "
                f"{event['stopped']}, {event['chars']} символов"
            )
        elif kind == "entry_written":
            lines.append(
                f"{self._t()}  ЗАПИСЬ [{event['score']:.2f}] [{event['state']}] "
                f"{event['name'][:52]}  цитат {event['citations']}"
            )
        elif kind == "agent_failed":
            attempts = event.get("attempts")
            spent = event.get("elapsed_s")
            detail = "".join(
                [
                    f" попыток {attempts}" if attempts else "",
                    f" за {spent}s" if spent else "",
                ]
            )
            lines.append(
                f"{self._t()}  xx {who:<10} сорвался [{event.get('reason')}]{detail}  "
                f"{event.get('error', '')[:60]}"
            )
        elif kind == "supervisor_restart":
            lines.append(f"{self._t()}  !! рестарт {event['attempt']}: {event['error']}")
        elif kind == "supervisor_gave_up":
            lines.append(
                f"{self._t()}  !! супервизор сдался после {event['attempts']}: {event['error']}"
            )
        elif kind == "split_reparse":
            lines.append(f"{self._t()}  ~~ разбор не удался, прошу переделать: {event['error']}")
        elif kind == "split_failed":
            lines.append(f"{self._t()}  xx разбиение на поля не состоялось: {event['error']}")
        elif kind == "tail_failed":
            lines.append(f"{self._t()}  ~~ не собрался отчёт [{event['stage']}]: {event['error']}")
        elif kind == "verifying":
            lines.append(f"{self._t()}  проверка цитат...")

        for line in lines:
            try:
                self._queue.put_nowait(line)
            except queue.Full:
                with self._drop_lock:
                    self._dropped += 1

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        deadline = time.monotonic() + self._drain_timeout
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)
        try:
            self._queue.put_nowait(self._STOP)
        except queue.Full:
            return
        self._writer.join(max(0.0, deadline - time.monotonic()))

    def __enter__(self) -> Live:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()



async def main(
    query: str,
    top: int | None,
    max_research: int | None,
    resume_run_id: int | None = None,
    shard_size: int | None = None,
    seed: list[dict] | None = None,
    seed_filename: str | None = None,
    max_cost_usd: float | None = None,
) -> int:
    live = Live()
    try:
        outcome = await run_query(
            query,
            top=top,
            max_research=max_research,
            shard_size=shard_size,
            max_cost_usd=max_cost_usd,
            sink=live,
            resume_run_id=resume_run_id,
            **({"seed": seed, "seed_filename": seed_filename} if seed is not None else {}),
        )
    finally:
        live.close()

    print(f"\nrun       {outcome.run_id}  ({outcome.state})")
    reason = getattr(outcome, "reason", None)
    if reason:
        print(f"reason    {reason}")
    print(
        f"задача    до {outcome.max_research} технологий в исследование, "
        f"показать лучшие {outcome.top_n}"
    )

    if outcome.summary:
        print(f"\n{'=' * 78}\n{outcome.summary}")

    try:
        await _present(outcome, outcome.top_n)
    except Exception as exc:
        print(f"\n(отчёт не собрался: {type(exc).__name__}: {exc})")
        print(f"запуск цел, попробуйте:  dev report {outcome.run_id} --full")
        if get_settings().debug:
            traceback.print_exc()

    return 0 if outcome.state in ("finished", "exhausted") else 1


async def _present(outcome: RunOutcome, top: int) -> None:
    async with get_sessionmaker()() as session:
        response, text = await assemble(session, outcome.run_id, top=top)
        meta = render_meta(
            accounting=outcome.accounting or {},
            agents=outcome.agents,
            context=await context_sources(session, outcome.run_id),
            sources=await per_source(session, outcome.run_id),
            run_state=outcome.state,
            restarts=outcome.restarts,
            prompts=outcome.prompts,
            http_requests=outcome.http_requests,
            fields=await field_counts(session, outcome.run_id),
            unbacked=await unbacked_patterns(session, outcome.run_id),
        )

    print(text)
    print(meta)

    try:
        written = save(outcome.run_id, text, response)
        written += save(outcome.run_id, text + meta, suffix="-full")
    except OSError as exc:
        print(f"\n(отчёт не записался на диск: {exc})")
        return
    for path in written:
        print(f"записано  {path}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ask",
        description=(
            "Найти слабые технологические сигналы или продолжить сохранённый запуск."
        ),
        epilog="""examples:
  ask
      Спросить направление интерактивно и запустить с настройками по умолчанию.

  ask --max 20 --top 15 --shard-size 4
      Исследовать не более 20 технологий, показать лучшие 15
      и использовать шарды по 4 поля.

  ask --resume 9
      Продолжить запуск 9. Направление, --max и --top берутся из базы.
      Размер шарда берётся из базы, либо из настроек для прежних запусков.

  ask --resume 9 --top 10 --max 25 --shard-size 4
      Продолжить запуск 9 с размером шарда 4 и показать 10 результатов.
      Общий предел исследования — 25.

resume semantics:
  --resume не принимает QUERY и не начинает исследование заново. Уже исследованные
  технологии засчитываются в --max; без --shard-size используется сохранённый
  размер шарда; незабранные возвраты агентов восстанавливаются
  из сохранённых turns. Продолжение может делать новые платные вызовы модели.

Windows wrappers:
  .\\ask.cmd --resume 9
  python dev.py ask --resume 9
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "query",
        nargs="?",
        metavar="QUERY",
        help="Свободная формулировка направления, по-русски. Без неё спросим.",
    )
    parser.add_argument(
        "--max",
        dest="max_research",
        metavar="N",
        type=int,
        default=None,
        help=(
            "Сколько технологий взять в исследование. Жёсткий предел запуска. "
            "По умолчанию WSIGNAL_MAX_RESEARCH."
        ),
    )
    parser.add_argument(
        "--shard-size",
        metavar="N",
        type=int,
        default=None,
        help=(
            "Максимум полей на оркестраторский шард; позволяет принудительно "
            "делить небольшие запуски. По умолчанию WSIGNAL_SHARD_SIZE."
        ),
    )
    parser.add_argument(
        "--top",
        metavar="N",
        type=int,
        default=None,
        help=(
            "Сколько показать в голове списка. Только отображение: ниже черты "
            "всё равно всё записано. По умолчанию WSIGNAL_TOP_N."
        ),
    )
    parser.add_argument(
        "--max-cost",
        dest="max_cost_usd",
        metavar="USD",
        type=float,
        default=None,
        help="Stop model calls when run spend reaches this USD amount; unlimited by default.",
    )
    parser.add_argument(
        "--seed",
        metavar="PATH",
        default=None,
        help="JSON list of technologies to assess; starts one researcher per item.",
    )
    parser.add_argument(
        "--resume",
        dest="resume_run_id",
        metavar="RUN_ID",
        type=int,
        default=None,
        help=(
            "Продолжить прерванный запуск по его id. Направление берётся из "
            "самого запуска; возвраты исследователей, которые никто не забрал, "
            "восстанавливаются из базы и ничего не исследуется заново."
        ),
    )
    return parser


def _run_with_sigterm(coro):
    previous = signal.getsignal(signal.SIGTERM)

    def request_stop(_signum, _frame):
        signal.raise_signal(signal.SIGINT)

    signal.signal(signal.SIGTERM, request_stop)
    try:
        return asyncio.run(coro)
    finally:
        signal.signal(signal.SIGTERM, previous)


def run() -> int:
    if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = _parser()
    args = parser.parse_args()

    if args.resume_run_id is not None and args.query is not None:
        parser.error("QUERY нельзя передавать вместе с --resume: направление хранится в запуске")
    if args.resume_run_id is not None and args.seed is not None:
        parser.error("--seed нельзя передавать вместе с --resume")
    if args.seed is not None and args.query is not None:
        parser.error("QUERY вместе с --seed не принимается; задача берётся из списка технологий")

    if args.resume_run_id is not None:
        if (
            (args.top is not None and args.top < 1)
            or (args.max_research is not None and args.max_research < 1)
            or (args.shard_size is not None and args.shard_size < 1)
            or (args.max_cost_usd is not None and args.max_cost_usd < 0)
        ):
            parser.error("--top, --max и --shard-size должны быть не меньше единицы")
        try:
            return _run_with_sigterm(
                main(
                    "",
                    args.top,
                    args.max_research,
                    resume_run_id=args.resume_run_id,
                    shard_size=args.shard_size,
                    max_cost_usd=args.max_cost_usd,
                )
            )
        except KeyboardInterrupt:
            print("\nпрервано")
            return 130
        except Exception as exc:
            print(f"\nпродолжить не вышло: {type(exc).__name__}: {exc}")
            if get_settings().debug:
                traceback.print_exc()
            return 1

    seeds = None
    seed_filename = None
    if args.seed is not None:
        seed_path = Path(args.seed)
        try:
            payload = json.loads(seed_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            parser.error(f"не удалось прочитать JSON seed-файл: {exc}")
        if not isinstance(payload, list) or not payload:
            parser.error("seed-файл должен содержать непустой JSON-список")
        seeds = []
        for index, item in enumerate(payload, start=1):
            if not isinstance(item, dict):
                parser.error(f"seed item {index} должен быть объектом")
            label = item.get("n")
            technology = item.get("tech")
            domain = item.get("domain")
            if (
                isinstance(label, bool)
                or not isinstance(label, (str, int, float))
                or not isinstance(technology, str)
                or not technology.strip()
                or not isinstance(domain, str)
                or not domain.strip()
            ):
                parser.error(f"seed item {index} требует n (строка/число), tech и domain")
            metadata = {}
            for key in SEED_METADATA_KEYS:
                if key in item:
                    if not isinstance(item[key], str):
                        parser.error(f"seed item {index} {key} must be a string")
                    metadata[key] = item[key]
            seeds.append(
                {
                    "n": label,
                    "tech": technology.strip(),
                    "domain": domain.strip(),
                    **metadata,
                }
            )
        seed_filename = seed_path.name

    query = args.query
    if not query and seeds is None:
        print("Направление (свободная формулировка, по-русски).")
        print("Например: слабые сигналы в области кибербезопасности\n")
        try:
            query = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nотменено")
            return 1
    if not query and seeds is None:
        print("пустой запрос")
        return 1

    try:
        query = query or ""
        query.encode("utf-8")
    except UnicodeEncodeError:
        print("Консоль прислала не UTF-8 — русский текст не дошёл целым.")
        print(f"получено: {query!r}")
        print("Запускайте через dev.py; cmd.exe ломает кириллицу в аргументах.")
        return 1

    settings = get_settings()
    top = args.top if args.top is not None else settings.top_n
    max_research = (
        args.max_research
        if args.max_research is not None
        else len(seeds)
        if seeds is not None
        else settings.max_research
    )
    shard_size = (
        args.shard_size if args.shard_size is not None else settings.shard_size
    )
    if top < 1 or max_research < 1 or shard_size < 1:
        print("--top, --max и --shard-size должны быть не меньше единицы")
        return 1
    if args.max_cost_usd is not None and args.max_cost_usd < 0:
        print("--max-cost не может быть отрицательным")
        return 1
    if seeds is not None and max_research < len(seeds):
        print("--max не может быть меньше количества технологий в seed-файле")
        return 1

    try:
        return _run_with_sigterm(
            main(
                query,
                top,
                max_research,
                shard_size=shard_size,
                max_cost_usd=args.max_cost_usd,
                seed=seeds,
                seed_filename=seed_filename,
            )
        )
    except KeyboardInterrupt:
        print("\nпрервано")
        return 130
    except Exception as exc:
        print(f"\nзапуск не состоялся: {type(exc).__name__}: {exc}")
        if settings.debug:
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(run())
