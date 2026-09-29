#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ACTIONS = (
    "up",
    "down",
    "logs",
    "test",
    "psql",
    "reset",
    "migrate",
    "ask",
    "cli",
    "params",
    "feeds",
    "report",
    "retier",
    "probe",
)


ACTION_HELP = {
    "up": "build and start the development stack",
    "down": "stop the stack; pass -v to also delete the database volume",
    "logs": "follow logs, optionally for one service: logs api",
    "test": "run the complete test and lint suite inside the image",
    "psql": "open PostgreSQL, or pass psql -c 'select 1'",
    "reset": "DESTRUCTIVE: delete the database volume and rebuild",
    "migrate": "run Alembic; defaults to upgrade head",
    "ask": "start a query or resume a stored run; ask --help for details",
    "cli": "alias of ask",
    "params": "extract parameter readings over dataset or entry populations",
    "feeds": "poll the configured feed register",
    "report": "render a stored run; report --help for details",
    "retier": "recompute stored document source tiers",
    "probe": "run a script from tmp/ inside the API environment",
}


HELP = """usage: python dev.py <command> [args...]

Run development commands through WSL and Docker Compose. Arguments after the
command are forwarded unchanged to that command.

commands:
{commands}

common workflows:
  python dev.py up
  python dev.py logs api
  python dev.py test
  python dev.py ask --help
  python dev.py ask --resume 9
  python dev.py report 9 --full
  python dev.py down

help:
  python dev.py --help          show this command catalog
  python dev.py ask --help      query and resume arguments
  python dev.py report --help   report arguments

`reset` and `down -v` delete the development database volume.
""".format(
    commands="\n".join(f"  {name:<9} {ACTION_HELP[name]}" for name in ACTIONS)
)


def usage(*, error: bool = False) -> int:
    print(HELP)
    return 1 if error else 0


def wsl_repo_path(root: Path) -> str | None:
    try:
        done = subprocess.run(
            ["wsl.exe", "wslpath", "-a", root.as_posix()], capture_output=True
        )
    except OSError:
        return None
    if done.returncode != 0:
        return None
    raw = done.stdout.replace(b"\x00", b"")
    return raw.decode("utf-8", "replace").strip() or None


def main(argv: list[str]) -> int:
    if not argv:
        return usage(error=True)

    if argv in (["-h"], ["--help"], ["help"]):
        return usage()

    action, args = argv[0], argv[1:]
    if action not in ACTIONS:
        print(f"unknown action: {action}")
        return usage(error=True)

    root = Path(__file__).resolve().parent
    repo = wsl_repo_path(root)
    if repo is None:
        print("could not resolve this folder inside WSL. is WSL installed and running?")
        return 1

    try:
        return subprocess.run(
            ["wsl.exe", "--", "bash", f"{repo}/scripts/{action}.sh", *args]
        ).returncode
    except KeyboardInterrupt:
        return 130
    except OSError as exc:
        print(f"could not start wsl.exe: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
