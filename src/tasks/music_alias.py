from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx

from src.common.http import RetryConfig, create_async_client, get_json
from src.common.io import read_json, utc_now_iso, write_json

MUSICS_SOURCE_URL = (
    "https://raw.githubusercontent.com/Team-Haruki/haruki-sekai-master/refs/heads/main/master/musics.json"
)
ALIAS_API_URL_TEMPLATE = "https://public-api.haruki.seiunx.com/alias/v1/music/{music_id}"
FETCH_CONCURRENCY = 3
RETRY_CONCURRENCY = 1
REQUEST_DELAY_SECONDS = 0.15


def parse_alias_payload(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return []
    aliases_raw = payload.get("aliases")
    if not isinstance(aliases_raw, list):
        return []

    aliases: list[str] = []
    seen: set[str] = set()
    for value in aliases_raw:
        if not isinstance(value, str):
            continue
        alias = value.strip()
        if not alias or alias in seen:
            continue
        seen.add(alias)
        aliases.append(alias)
    return aliases


def _load_previous_alias_map(path: Path) -> dict[int, list[str]]:
    previous_payload = read_json(path, default={})
    if not isinstance(previous_payload, dict):
        return {}
    musics = previous_payload.get("musics")
    if not isinstance(musics, list):
        return {}

    alias_map: dict[int, list[str]] = {}
    for item in musics:
        if not isinstance(item, dict):
            continue
        music_id = item.get("music_id")
        aliases = item.get("aliases")
        if not isinstance(music_id, int) or not isinstance(aliases, list):
            continue
        alias_map[music_id] = [alias for alias in aliases if isinstance(alias, str)]
    return alias_map


async def _fetch_alias_for_music(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    music_id: int,
    retry_attempts: int = 6,
) -> tuple[int, list[str] | None]:
    async with semaphore:
        try:
            payload = await get_json(
                client,
                ALIAS_API_URL_TEMPLATE.format(music_id=music_id),
                retry_config=RetryConfig(attempts=retry_attempts),
            )
            await asyncio.sleep(REQUEST_DELAY_SECONDS)
        except (httpx.HTTPError, ValueError):
            return music_id, None
        return music_id, parse_alias_payload(payload)


def build_output_musics(
    records: list[tuple[int, str]],
    fetched_alias_map: dict[int, list[str] | None],
    previous_alias_map: dict[int, list[str]],
) -> tuple[list[dict[str, Any]], int]:
    failed_count = 0
    output_musics: list[dict[str, Any]] = []

    for music_id, title in records:
        fetched_aliases = fetched_alias_map.get(music_id)
        if fetched_aliases is None:
            failed_count += 1
            aliases = previous_alias_map.get(music_id, [])
        else:
            aliases = fetched_aliases

        output_musics.append(
            {
                "music_id": music_id,
                "title": title,
                "aliases": aliases,
            }
        )
    return output_musics, failed_count


def build_payload(output_musics: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "generated_at": utc_now_iso(),
        "source": {
            "musics": MUSICS_SOURCE_URL,
            "alias_api": ALIAS_API_URL_TEMPLATE,
        },
        "musics": output_musics,
    }


async def update_music_aliases(
    output_path: Path = Path("data/music_alias/music_aliases.json"),
) -> dict[str, int]:
    previous_alias_map = _load_previous_alias_map(output_path)
    async with create_async_client() as client:
        musics_payload = await get_json(client, MUSICS_SOURCE_URL, retry_config=RetryConfig(attempts=6))
        if not isinstance(musics_payload, list):
            raise ValueError("Invalid musics source payload: expected a list")

        records: list[tuple[int, str]] = []
        for item in musics_payload:
            if not isinstance(item, dict):
                continue
            music_id = item.get("id")
            title = item.get("title")
            if not isinstance(music_id, int) or not isinstance(title, str):
                continue
            records.append((music_id, title))

        records.sort(key=lambda row: row[0])
        if not records:
            write_json(
                output_path,
                {
                    "generated_at": utc_now_iso(),
                    "source": {
                        "musics": MUSICS_SOURCE_URL,
                        "alias_api": ALIAS_API_URL_TEMPLATE,
                    },
                    "musics": [],
                },
            )
            return {
                "musics_total": 0,
                "alias_fetch_failed": 0,
                "used_cached_data": 0,
            }

        probe_semaphore = asyncio.Semaphore(1)
        probe_music_id = records[0][0]
        _, probe_aliases = await _fetch_alias_for_music(client, probe_semaphore, probe_music_id, retry_attempts=2)
        if probe_aliases is None:
            if not previous_alias_map:
                raise RuntimeError("Alias API unavailable and no cached data available.")
            fallback_map: dict[int, list[str] | None] = {music_id: None for music_id, _ in records}
            output_musics, failed_count = build_output_musics(records, fallback_map, previous_alias_map)
            write_json(output_path, build_payload(output_musics))
            return {
                "musics_total": len(output_musics),
                "alias_fetch_failed": failed_count,
                "used_cached_data": 1,
            }

        semaphore = asyncio.Semaphore(FETCH_CONCURRENCY)
        tasks = [
            _fetch_alias_for_music(client, semaphore, music_id, retry_attempts=6)
            for music_id, _ in records
        ]
        results = await asyncio.gather(*tasks)
        fetched_alias_map: dict[int, list[str] | None] = dict(results)
        fetched_alias_map[probe_music_id] = probe_aliases

        failed_ids = [music_id for music_id, aliases in fetched_alias_map.items() if aliases is None]
        if failed_ids:
            retry_semaphore = asyncio.Semaphore(RETRY_CONCURRENCY)
            retry_tasks = [
                _fetch_alias_for_music(client, retry_semaphore, music_id, retry_attempts=8)
                for music_id in failed_ids
            ]
            retry_results = await asyncio.gather(*retry_tasks)
            for music_id, aliases in retry_results:
                fetched_alias_map[music_id] = aliases

    output_musics, failed_count = build_output_musics(records, fetched_alias_map, previous_alias_map)
    write_json(output_path, build_payload(output_musics))
    return {
        "musics_total": len(output_musics),
        "alias_fetch_failed": failed_count,
        "used_cached_data": 0,
    }
