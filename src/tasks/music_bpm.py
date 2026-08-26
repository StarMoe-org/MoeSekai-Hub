from __future__ import annotations

import asyncio
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from src.common.http import RetryConfig, create_async_client, get_json, get_text
from src.common.io import read_json, write_json

METADATA_URL_TEMPLATE = "https://metadata.exmeaning.com/{lang}/master/musics.json"
INFO_URL_TEMPLATE = (
    "https://storage.exmeaning.com/{prefix}-assets/music/music_score/{music_id:04d}_01/info.txt"
)
SCORE_URL_TEMPLATE = (
    "https://storage.exmeaning.com/{prefix}-assets/music/music_score/{music_id:04d}_01/{difficulty}.txt"
)

# 服务器解析优先级：同一 music_id 只解析优先级最高的服的 BPM
SERVER_PRIORITY: list[tuple[str, str]] = [
    ("jp", "sekai-jp"),
    ("cn", "sekai-cn"),
    ("tw", "sekai-tw"),
    ("en", "sekai-en"),
    ("kr", "sekai-kr"),
]

# 谱面文件解析顺序（master 优先），info.txt 作为兜底
SCORE_FILES = ("master", "append", "hard", "normal", "easy")

CONCURRENCY = 20
FETCH_RETRY = RetryConfig(attempts=4)

_BPM_DEF_RE = re.compile(r"^#BPM_?(\d+):\s*([0-9]+(?:\.[0-9]+)?)\s*$", re.IGNORECASE)
# SUS 事件行：#bbbkk: data（bbb=3 位十进制小节号，kk=2 位类型码）
_EVENT_RE = re.compile(r"^#(\d{3})(\d{2}):\s*(.*)$")


def parse_bpm_segments(info_text: str) -> list[dict[str, Any]] | None:
    """从 Ched/SUS 谱面文本提取按时间顺序的 BPM 段列表。

    - #BPMxx: value 定义 BPM 值（索引 36 进制）
    - #bbb08: pairs 是小节 bbb 处的 BPM 引用（小节内按 beat 均分）
    - #bbb02: n 定义每小节拍数（默认 4）
    - 谱面结束小节 = 所有事件行的最大小节 + 1
    无任何 BPM 定义时返回 None。
    """
    bpm_defs: dict[int, float] = {}
    refs: list[tuple[float, float]] = []
    beats_per_bar = 4
    end_bar = 1

    for raw_line in info_text.splitlines():
        line = raw_line.strip()
        match = _BPM_DEF_RE.match(line)
        if match is not None:
            bpm_defs[int(match.group(1))] = float(match.group(2))
            continue
        match = _EVENT_RE.match(line)
        if match is None:
            continue
        bar, kind, data = int(match.group(1)), match.group(2), match.group(3)
        end_bar = max(end_bar, bar + 1)
        if kind == "02":
            try:
                beats_per_bar = max(1, int(data))
            except ValueError:
                pass
        elif kind == "08":
            cleaned = data.rstrip()
            pairs = [cleaned[index : index + 2] for index in range(0, len(cleaned), 2)]
            count = len(pairs) or 1
            for beat, pair in enumerate(pairs):
                bpm = bpm_defs.get(int(pair, 36))
                if bpm is not None:
                    refs.append((bar + beat / count, bpm))

    if not bpm_defs:
        return None

    refs.sort(key=lambda item: item[0])
    if not refs:
        # 仅定义了 BPM 但无引用：把最小索引的定义当作全曲唯一段
        refs = [(0.0, bpm_defs[min(bpm_defs)])]

    segments: list[dict[str, Any]] = []
    for index, (start_bar, bpm) in enumerate(refs):
        end = refs[index + 1][0] if index + 1 < len(refs) else float(end_bar)
        duration_sec = (end - start_bar) * beats_per_bar * 60.0 / bpm
        segments.append(
            {
                "bpm": bpm,
                "start_bar": start_bar,
                "end_bar": end,
                "duration_sec": round(duration_sec, 2),
            }
        )
    return segments


def build_bpm_record(segments: list[dict[str, Any]]) -> dict[str, Any] | None:
    """汇总 BPM 段：主 bpm = 持续最长的段，bpms = 按时间顺序的完整变化值列表（不去重）。"""
    if not segments:
        return None
    main_bpm = max(segments, key=lambda segment: segment["duration_sec"])["bpm"]
    return {
        "bpm": main_bpm,
        "bpms": [segment["bpm"] for segment in segments],
        "bpm_count": len(segments),
        "bpm_segments": segments,
    }


async def fetch_music_list(client: httpx.AsyncClient, lang: str) -> list[dict[str, Any]]:
    payload = await get_json(client, METADATA_URL_TEMPLATE.format(lang=lang), retry_config=FETCH_RETRY)
    if not isinstance(payload, list):
        raise ValueError(f"Invalid musics payload for {lang}: expected a list")
    return [item for item in payload if isinstance(item, dict)]


async def get_text_or_none(client: httpx.AsyncClient, url: str) -> str | None:
    """抓取文本；404 返回 None，其他错误向上抛出。"""
    try:
        return await get_text(client, url, retry_config=FETCH_RETRY)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None
        raise


async def fetch_score_text(client: httpx.AsyncClient, prefix: str, music_id: int) -> str | None:
    """按优先级依次尝试各难度谱面文件，最后回退 info.txt；全部缺失时返回 None。"""
    for difficulty in SCORE_FILES:
        url = SCORE_URL_TEMPLATE.format(prefix=prefix, music_id=music_id, difficulty=difficulty)
        text = await get_text_or_none(client, url)
        if text is not None:
            return text
    url = INFO_URL_TEMPLATE.format(prefix=prefix, music_id=music_id)
    return await get_text_or_none(client, url)


def _load_cached_songs(output_dir: Path) -> dict[int, dict[str, Any]]:
    """读取上次产物中已成功解析 BPM 段（bpm_segments）的记录，供增量更新复用。"""
    payload = read_json(output_dir / "music_bpms.json", default=None)
    if not isinstance(payload, dict):
        return {}
    songs = payload.get("songs")
    if not isinstance(songs, list):
        return {}
    cached: dict[int, dict[str, Any]] = {}
    for record in songs:
        if not isinstance(record, dict) or "bpm_segments" not in record:
            continue
        music_id = record.get("music_id")
        if isinstance(music_id, int):
            cached[music_id] = record
    return cached


async def update_music_bpm(
    output_dir: Path = Path("data/music_bpm"),
    ids: list[int] | None = None,
    force: bool = False,
) -> dict[str, int]:
    """抓取各服 BPM 并汇总。

    - 默认：增量更新所有歌曲，已成功解析的曲目复用上次产物，只抓新增/缺失的
    - force=True：忽略缓存，全量重新抓取
    - ids 指定时：强制重新抓取这些 id/范围，其余歌曲复用缓存，输出保持全量清单
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    async with create_async_client() as client:
        # 1. 按优先级拉取各服曲目清单，相同 music_id 只保留最高优先级来源
        songs: dict[int, dict[str, Any]] = {}
        source_counts: dict[str, int] = {}
        for lang, prefix in SERVER_PRIORITY:
            try:
                musics = await fetch_music_list(client, lang)
            except Exception as exc:
                print(f"[music-bpm] failed to fetch music list for {lang}: {exc}", file=sys.stderr)
                continue
            added = 0
            for item in musics:
                music_id = item.get("id")
                if not isinstance(music_id, int) or music_id in songs:
                    continue
                title = item.get("title")
                songs[music_id] = {
                    "music_id": music_id,
                    "title": title if isinstance(title, str) else "",
                    "source": lang,
                    "prefix": prefix,
                }
                added += 1
            source_counts[lang] = added

        if not songs:
            raise ValueError("No music ids collected from any server")

        entries = list(songs.values())
        requested = None if ids is None else set(ids)
        if requested is not None:
            matched = [entry for entry in entries if entry["music_id"] in requested]
            if not matched:
                raise ValueError("None of the requested music ids exist in the merged server lists")

        # 2. 读取缓存（增量复用；--ids 模式下其余歌曲也以缓存兜底，force 全量时忽略）
        cached_by_id = {} if (force and ids is None) else _load_cached_songs(output_dir)

        # 3. 组装输出：清单全量歌曲，逐首决定「复用缓存 / 占位保留 / 抓取」
        records_by_id: dict[int, dict[str, Any]] = {}
        to_fetch: list[dict[str, Any]] = []
        stats: dict[str, int] = {"reused": 0, "fetched": 0, "missing": 0, "failed": 0}

        def reuse_or_placeholder(entry: dict[str, Any]) -> None:
            music_id = int(entry["music_id"])
            cached = cached_by_id.get(music_id)
            if cached is not None:
                record = {key: value for key, value in cached.items() if key != "prefix"}
                record["title"] = str(entry["title"])  # 清单标题总是最新
                records_by_id[music_id] = record
                stats["reused"] += 1
            else:
                placeholder = dict(entry)
                placeholder.pop("prefix", None)
                records_by_id[music_id] = placeholder

        for entry in entries:
            music_id = int(entry["music_id"])
            if requested is not None:
                # --ids 模式：子集强制重抓，其余复用缓存（无缓存则占位保留）
                if music_id in requested:
                    to_fetch.append(entry)
                else:
                    reuse_or_placeholder(entry)
            elif force:
                # --force 全量：全部重抓
                to_fetch.append(entry)
            elif music_id in cached_by_id:
                # 默认增量：缓存命中直接复用
                reuse_or_placeholder(entry)
            else:
                # 默认增量：缓存未命中需要抓取
                to_fetch.append(entry)

        # 4. 并发抓取谱面文本并解析 BPM 段
        semaphore = asyncio.Semaphore(CONCURRENCY)

        async def resolve_one(entry: dict[str, Any]) -> tuple[dict[str, Any], str]:
            async with semaphore:
                try:
                    text = await fetch_score_text(client, str(entry["prefix"]), int(entry["music_id"]))
                except httpx.HTTPStatusError:
                    return entry, "missing"
                except Exception:
                    return entry, "failed"
                if text is None:
                    return entry, "missing"
                segments = parse_bpm_segments(text)
                if segments is None:
                    return entry, "missing"
                record = build_bpm_record(segments)
                if record is None:
                    return entry, "missing"
                result = dict(entry)
                result.update(record)
                result.pop("prefix", None)
                return result, "fetched"

        outcomes = await asyncio.gather(*(resolve_one(entry) for entry in to_fetch))
        for record, status in outcomes:
            stats[status] += 1
            records_by_id[int(record["music_id"])] = record

    records = [records_by_id[music_id] for music_id in sorted(records_by_id)]

    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "songs": records,
    }
    write_json(output_dir / "music_bpms.json", payload)

    stats["songs_total"] = len(records)
    stats["songs_with_bpm"] = stats["reused"] + stats["fetched"]
    stats["output_files"] = 1
    for lang, count in source_counts.items():
        stats[f"source_{lang}"] = count
    return stats
