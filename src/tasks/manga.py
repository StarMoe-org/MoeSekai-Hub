from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from src.common.http import RetryConfig, create_async_client, get_bytes, get_json
from src.common.io import atomic_write_bytes, read_json, write_json

MANGA_API_TEMPLATE = (
    "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space/search"
    "?host_mid=13148307&page={page}&offset={page}&keyword=%E6%BC%AB%E7%94%BB"
    "&features=itemOpusStyle,opusBigCover,forwardListHidden"
)

MANGA_TAGS = {"#SEKAI四格漫画#", "#SEKAI四格漫畫#"}
TITLE_PATTERN = re.compile(r"第\s*(?P<number>\d+)\s*话(?:[：:\s「『\"]+)?(?P<title>[^」』\"\n#]+)")
CONTRIBUTOR_PATTERN = re.compile(r"(?P<role>[^\s：:#]{1,12})\s*[：:]\s*(?P<name>[^：:\n#]+)")
DOWNLOAD_CONCURRENCY = 16
PAGE_UPPER_BOUND = 500
NO_PROGRESS_PAGE_LIMIT = 8


def _is_manga_post(content: str) -> bool:
    return any(tag in content for tag in MANGA_TAGS)


def parse_title_and_number(content: str) -> tuple[int, str] | None:
    match = TITLE_PATTERN.search(content)
    if match is None:
        return None
    number = int(match.group("number"))
    title = match.group("title").strip().strip("「」『』\"'[]")
    if not title:
        return None
    return number, title


def parse_contributors(content: str) -> dict[str, str]:
    contributors: dict[str, str] = {}
    for match in CONTRIBUTOR_PATTERN.finditer(content):
        role = match.group("role").strip()
        name = match.group("name").strip()
        if not role or not name:
            continue
        if role.startswith("第") or "话" in role:
            continue
        contributors[role] = name
    return contributors


def normalize_bilibili_url(url: str) -> str:
    if not url:
        return url
    if url.startswith("//"):
        return "https:" + url
    return url


def parse_manga_item(item: dict[str, Any]) -> dict[str, Any] | None:
    try:
        opus = item["modules"]["module_dynamic"]["major"]["opus"]
        content = opus["summary"]["text"]
        if not isinstance(content, str) or not _is_manga_post(content):
            return None

        parsed = parse_title_and_number(content)
        if parsed is None:
            return None
        number, title = parsed

        pictures = opus.get("pics") or []
        if not pictures or not isinstance(pictures[0], dict):
            return None
        image_url = normalize_bilibili_url(str(pictures[0].get("url", "")))
        if not image_url:
            return None

        jump_url = normalize_bilibili_url(str(opus.get("jump_url", "")))
        publish_ts = int(item["modules"]["module_author"]["pub_ts"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None

    return {
        "id": number,
        "title": title,
        "manga": image_url,
        "date": publish_ts,
        "url": jump_url,
        "contributors": parse_contributors(content),
    }


async def fetch_manga_metadata(cookie: str = "") -> dict[str, dict[str, Any]]:
    headers: dict[str, str] = {}
    if cookie:
        headers["cookie"] = cookie

    mangas: dict[str, dict[str, Any]] = {}
    max_seen_number = 0
    no_progress_pages = 0

    async with create_async_client(headers=headers) as client:
        for page in range(1, PAGE_UPPER_BOUND + 1):
            api_url = MANGA_API_TEMPLATE.format(page=page)
            payload = await get_json(client, api_url, retry_config=RetryConfig(attempts=6))
            items = payload.get("data", {}).get("items", [])
            if not items:
                break

            new_count = 0
            for item in items:
                if not isinstance(item, dict):
                    continue
                manga = parse_manga_item(item)
                if manga is None:
                    continue
                manga_id = manga["id"]
                max_seen_number = max(max_seen_number, manga_id)
                mangas[str(manga_id)] = manga
                new_count += 1

            if max_seen_number > 0 and len(mangas) >= max_seen_number:
                break

            if new_count == 0:
                no_progress_pages += 1
            else:
                no_progress_pages = 0
            if no_progress_pages >= NO_PROGRESS_PAGE_LIMIT:
                break

    return dict(sorted(mangas.items(), key=lambda item: item[1]["id"], reverse=True))


def _should_redownload_image(
    manga_id: int,
    target_path: Path,
    metadata: dict[str, dict[str, Any]],
    previous_metadata: dict[str, Any],
) -> bool:
    if not target_path.exists():
        return True
    previous_item = previous_metadata.get(str(manga_id))
    if not isinstance(previous_item, dict):
        return False
    previous_url = previous_item.get("manga")
    current_url = metadata[str(manga_id)].get("manga")
    return isinstance(previous_url, str) and isinstance(current_url, str) and previous_url != current_url


@dataclass(frozen=True)
class DownloadCandidate:
    manga_id: int
    image_url: str
    target_path: Path
    changed_url: bool


async def _download_image(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    manga_id: int,
    image_url: str,
    target_path: Path,
) -> bool:
    async with semaphore:
        image_bytes = await get_bytes(client, image_url, retry_config=RetryConfig(attempts=6))
        atomic_write_bytes(target_path, image_bytes)
        return True


async def download_manga_images(
    metadata: dict[str, dict[str, Any]],
    previous_metadata: dict[str, Any],
    output_dir: Path,
) -> tuple[dict[str, int], set[int]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)
    to_download: list[DownloadCandidate] = []

    for key, item in metadata.items():
        manga_id = int(key)
        image_url = str(item.get("manga", ""))
        if not image_url:
            continue
        parsed_url = urlparse(image_url)
        if not parsed_url.scheme:
            continue

        target_path = output_dir / f"{manga_id}.png"
        changed_url = False
        previous_item = previous_metadata.get(str(manga_id))
        if isinstance(previous_item, dict):
            previous_url = previous_item.get("manga")
            current_url = metadata[str(manga_id)].get("manga")
            changed_url = isinstance(previous_url, str) and isinstance(current_url, str) and previous_url != current_url
        if _should_redownload_image(manga_id, target_path, metadata, previous_metadata):
            to_download.append(
                DownloadCandidate(
                    manga_id=manga_id,
                    image_url=image_url,
                    target_path=target_path,
                    changed_url=changed_url,
                )
            )

    success_count = 0
    failure_count = 0
    failed_changed_url_ids: set[int] = set()

    async with create_async_client() as client:
        tasks = [_download_image(client, semaphore, item.manga_id, item.image_url, item.target_path) for item in to_download]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    for candidate, result in zip(to_download, results, strict=True):
        if isinstance(result, Exception):
            failure_count += 1
            if candidate.changed_url:
                failed_changed_url_ids.add(candidate.manga_id)
        elif result:
            success_count += 1

    return (
        {
            "download_required": len(to_download),
            "download_success": success_count,
            "download_failed": failure_count,
        },
        failed_changed_url_ids,
    )


def build_metadata_to_persist(
    metadata: dict[str, dict[str, Any]],
    previous_metadata: dict[str, Any],
    failed_changed_url_ids: set[int],
) -> dict[str, dict[str, Any]]:
    if not failed_changed_url_ids:
        return metadata

    output: dict[str, dict[str, Any]] = {}
    for key, item in metadata.items():
        manga_id = int(key)
        if manga_id not in failed_changed_url_ids:
            output[key] = item
            continue

        previous_item = previous_metadata.get(key)
        if not isinstance(previous_item, dict):
            output[key] = item
            continue

        previous_url = previous_item.get("manga")
        if not isinstance(previous_url, str) or not previous_url:
            output[key] = item
            continue

        merged_item = dict(item)
        # Keep old image URL when changed-url download failed; this guarantees retry next run.
        merged_item["manga"] = previous_url
        output[key] = merged_item
    return output


async def update_manga(
    metadata_path: Path = Path("mangas/mangas.json"),
    output_dir: Path = Path("mangas"),
    cookie: str | None = None,
) -> dict[str, int]:
    resolved_cookie = cookie if cookie is not None else os.getenv("BILIBILI_COOKIE", "")
    previous_metadata_raw = read_json(metadata_path, default={})
    previous_metadata = previous_metadata_raw if isinstance(previous_metadata_raw, dict) else {}

    metadata = await fetch_manga_metadata(cookie=resolved_cookie)
    download_stats, failed_changed_url_ids = await download_manga_images(metadata, previous_metadata, output_dir)
    metadata_to_persist = build_metadata_to_persist(metadata, previous_metadata, failed_changed_url_ids)
    write_json(metadata_path, metadata_to_persist)
    return {
        "metadata_total": len(metadata_to_persist),
        **download_stats,
    }
