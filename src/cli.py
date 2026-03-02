from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Awaitable, Callable

from src.tasks.event_bvid import update_event_bvid
from src.tasks.manga import update_manga
from src.tasks.music_alias import update_music_aliases

TaskFunc = Callable[[], Awaitable[dict[str, int]]]


def _print_stats(task_name: str, stats: dict[str, int]) -> None:
    serialized = json.dumps(stats, ensure_ascii=False, sort_keys=True)
    print(f"[{task_name}] {serialized}")


async def _run_single(task_name: str, task: TaskFunc) -> int:
    stats = await task()
    _print_stats(task_name, stats)
    return 0


async def _run_all() -> int:
    pipeline: list[tuple[str, TaskFunc]] = [
        ("update-event-bvid", update_event_bvid),
        ("update-manga", update_manga),
        ("update-music-alias", update_music_aliases),
    ]
    failed: list[str] = []
    for name, task in pipeline:
        try:
            stats = await task()
            _print_stats(name, stats)
        except Exception as exc:
            failed.append(name)
            print(f"[{name}] failed: {type(exc).__name__}: {exc}", file=sys.stderr)

    if failed:
        print(f"[run-all] failed tasks: {', '.join(failed)}", file=sys.stderr)
        return 1

    print("[run-all] all tasks completed successfully")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Unified daily updater for event BVID, manga, and music aliases.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("update-event-bvid")
    subparsers.add_parser("update-manga")
    subparsers.add_parser("update-music-alias")
    subparsers.add_parser("run-all")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "update-event-bvid":
        return asyncio.run(_run_single("update-event-bvid", update_event_bvid))
    if args.command == "update-manga":
        return asyncio.run(_run_single("update-manga", update_manga))
    if args.command == "update-music-alias":
        return asyncio.run(_run_single("update-music-alias", update_music_aliases))
    if args.command == "run-all":
        return asyncio.run(_run_all())

    parser.error(f"Unsupported command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

