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
from src.tasks.music_bpm import update_music_bpm
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


async def _run_story_summary(
    *,
    event_id: int | None = None,
    event_ids: list[int] | None = None,
    output_dir: str | None = None,
    force: bool = False,
    story_dir: str | None = None,
) -> int:
    resolved_output_dir = Path(output_dir) if output_dir is not None else Path("story/detail")
    resolved_story_dir = Path(story_dir) if story_dir is not None else None
    stats = await update_story_summary(
        event_id=event_id,
        event_ids=tuple(event_ids) if event_ids else None,
        output_dir=resolved_output_dir,
        force=force,
        story_dir=resolved_story_dir,
    )
    _print_stats("update-story-summary", stats)
    return 0


async def _run_music_bpm(ids: list[int] | None, force: bool) -> int:
    stats = await update_music_bpm(ids=ids, force=force)
    _print_stats("update-music-bpm", stats)
    return 0


def _parse_id_expression(text: str, *, label: str) -> list[int]:
    """解析逗号分隔的 id 或范围表达式，如 "1,10-20,35"。"""
    ids: list[int] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "-" in part:
                start_text, end_text = part.split("-", 1)
                start, end = int(start_text), int(end_text)
                if start <= 0 or end < start:
                    raise ValueError
                ids.extend(range(start, end + 1))
            else:
                value = int(part)
                if value <= 0:
                    raise ValueError
                ids.append(value)
        except ValueError:
            raise argparse.ArgumentTypeError(f"invalid {label} expression: {part!r}") from None
    return sorted(set(ids))


def _parse_music_bpm_ids(text: str) -> list[int]:
    """解析 --ids 参数：逗号分隔的 id 或范围，如 "1,10-20,35"。"""
    return _parse_id_expression(text, label="music id")


def _parse_event_ids(text: str) -> list[int]:
    """解析 --event-ids 参数：逗号分隔的活动 id 或范围，如 "1,10-20,35"。"""
    return _parse_id_expression(text, label="event id")


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
    music_bpm_parser = subparsers.add_parser(
        "update-music-bpm",
        help="Fetch BPM for all songs (incremental by default); use --ids/--force to force re-fetch",
    )
    music_bpm_parser.add_argument(
        "--ids",
        type=_parse_music_bpm_ids,
        default=None,
        help="Force update specific music ids or ranges, e.g. 1,10-20,35",
    )
    music_bpm_parser.add_argument(
        "--force",
        action="store_true",
        help="Re-fetch all songs, ignoring cached results",
    )
    subparsers.add_parser("update-bgm-duration")

    summary_parser = subparsers.add_parser("update-story-summary")
    summary_parser.add_argument("--event-id", type=int, default=None, help="Generate summary for a specific event ID")
    summary_parser.add_argument(
        "--event-ids",
        type=_parse_event_ids,
        default=None,
        help="Generate summary for specific event IDs or ranges, e.g. 1,10-20,35",
    )
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
    summary_parser.add_argument(
        "--story-dir",
        type=str,
        default=None,
        help="Path to the Moe-story repository clone (default: ./Moe-story, or $MOE_STORY_DIR)",
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
    if args.command == "update-music-bpm":
        return asyncio.run(_run_music_bpm(ids=args.ids, force=args.force))
    if args.command == "update-bgm-duration":
        return asyncio.run(_run_single("update-bgm-duration", update_bgm_duration))
    if args.command == "update-story-summary":
        return asyncio.run(
            _run_story_summary(
                event_id=args.event_id,
                event_ids=args.event_ids,
                output_dir=args.output_dir,
                force=args.force,
                story_dir=args.story_dir,
            )
        )
    if args.command == "run-all":
        return asyncio.run(_run_all())

    parser.error(f"Unsupported command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
