from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

from src.tasks.b30_csv import update_b30_csv
from src.tasks.bgm_duration import update_bgm_duration
from src.tasks.event_bvid import update_event_bvid
from src.tasks.manga import update_manga
from src.tasks.music_alias import update_music_aliases
from src.tasks.music_meta import update_music_meta
from src.tasks.story_summary import update_story_summary

TaskFunc = Callable[[], Awaitable[dict[str, int]]]


def _print_stats(task_name: str, stats: dict[str, int]) -> None:
    serialized = json.dumps(stats, ensure_ascii=False, sort_keys=True)
    print(f"[{task_name}] {serialized}")


async def _run_single(task_name: str, task: TaskFunc) -> int:
    stats = await task()
    _print_stats(task_name, stats)
    return 0


async def _run_story_summary(*, event_id: int | None = None, output_dir: str | None = None, force: bool = False) -> int:
    resolved_output_dir = Path(output_dir) if output_dir is not None else Path("story/detail")
    stats = await update_story_summary(event_id=event_id, output_dir=resolved_output_dir, force=force)
    _print_stats("update-story-summary", stats)
    return 0


async def _run_all() -> int:
    pipeline: list[tuple[str, TaskFunc]] = [
        ("update-event-bvid", update_event_bvid),
        ("update-manga", update_manga),
        ("update-music-alias", update_music_aliases),
        ("update-b30-csv", update_b30_csv),
        ("update-music-meta", update_music_meta),
        ("update-bgm-duration", update_bgm_duration),
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
    parser = argparse.ArgumentParser(
        description="Unified daily updater for event BVID, manga, music aliases, B30 CSV, music meta, and BGM durations."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("update-event-bvid")
    subparsers.add_parser("update-manga")
    subparsers.add_parser("update-music-alias")
    subparsers.add_parser("update-b30-csv")
    subparsers.add_parser("update-music-meta")
    subparsers.add_parser("update-bgm-duration")

    summary_parser = subparsers.add_parser("update-story-summary")
    summary_parser.add_argument("--event-id", type=int, default=None, help="Generate summary for a specific event ID")
    summary_parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="Directory for generated story summary JSON files (default: story/detail)",
    )
    summary_parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Regenerate summary even if the output file already exists",
    )

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
    if args.command == "update-b30-csv":
        return asyncio.run(_run_single("update-b30-csv", update_b30_csv))
    if args.command == "update-music-meta":
        return asyncio.run(_run_single("update-music-meta", update_music_meta))
    if args.command == "update-bgm-duration":
        return asyncio.run(_run_single("update-bgm-duration", update_bgm_duration))
    if args.command == "update-story-summary":
        return asyncio.run(_run_story_summary(event_id=args.event_id, output_dir=args.output_dir, force=args.force))
    if args.command == "run-all":
        return asyncio.run(_run_all())

    parser.error(f"Unsupported command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
