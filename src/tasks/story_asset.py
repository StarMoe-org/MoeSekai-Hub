from __future__ import annotations

import asyncio
import brotli
import json
import math
from pathlib import Path
from typing import Any

import httpx

from src.common.http import create_async_client, get_json
from src.common.io import atomic_write_bytes

_URLS_JSON = Path(__file__).with_name("story_asset_urls.json")
_DOWNLOAD_CONCURRENCY = 20


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def _load_urls() -> dict[str, Any]:
    with _URLS_JSON.open("r", encoding="utf-8") as f:
        return json.load(f)


def _master_url(urls: dict[str, Any], src: str, lang: str, file: str) -> str:
    base: str = urls[src]["master"]
    lang_prefix: str = urls[src]["master_lang"][lang]
    return base.format(lang=lang_prefix, file=file)


def _asset_url(urls: dict[str, Any], src: str, lang: str, asset_type: str) -> str:
    """Return the asset URL template with {lang} already resolved."""
    template: str = urls[src][asset_type]
    lang_prefix: str = urls[src]["asset_lang"][lang]
    return template.format(lang=lang_prefix, assetbundleName="{assetbundleName}", scenarioId="{scenarioId}", group="{group}")


def _url_to_local_path(url: str, output_dir: Path) -> Path:
    """Mirror the original project's url_to_path: strip scheme, use host+path as relative. Append .br suffix."""
    url_path = url[url.index("//") + 2:]
    return output_dir / (url_path + ".br")


# ---------------------------------------------------------------------------
# Collect asset URLs from master data
# ---------------------------------------------------------------------------

def _collect_event_urls(event_stories: list[dict[str, Any]], template: str) -> list[str]:
    urls: list[str] = []
    for es in event_stories:
        abn = es.get("assetbundleName", "")
        for ep in es.get("eventStoryEpisodes", []):
            sid = ep.get("scenarioId", "")
            if abn and sid:
                urls.append(template.format(assetbundleName=abn, scenarioId=sid))
    return urls


def _collect_unit_urls(unit_stories: list[dict[str, Any]], template: str) -> list[str]:
    urls: list[str] = []
    for us in unit_stories:
        for chapter in us.get("chapters", []):
            abn = chapter.get("assetbundleName", "")
            for ep in chapter.get("episodes", []):
                sid = ep.get("scenarioId", "")
                if abn and sid:
                    urls.append(template.format(assetbundleName=abn, scenarioId=sid))
    return urls


def _collect_card_urls(card_episodes: list[dict[str, Any]], cards_lookup: dict[int, dict[str, Any]], template: str) -> list[str]:
    urls: list[str] = []
    for ce in card_episodes:
        card_id = ce.get("cardId")
        card = cards_lookup.get(card_id)
        if card is None:
            continue
        abn = card.get("assetbundleName", "")
        sid = ce.get("scenarioId", "")
        if abn and sid:
            urls.append(template.format(assetbundleName=abn, scenarioId=sid))
    return urls


def _collect_talk_urls(action_sets: list[dict[str, Any]], template: str) -> list[str]:
    urls: list[str] = []
    for action in action_sets:
        sid = action.get("scenarioId")
        if not sid:
            continue
        group = math.floor(action["id"] / 100)
        urls.append(template.format(group=group, scenarioId=sid))
    return urls


def _collect_self_urls(character_profiles: list[dict[str, Any]], template: str) -> list[str]:
    urls: list[str] = []
    for cp in character_profiles:
        sid = cp.get("scenarioId", "")
        if not sid:
            continue
        # grade 1: common prefix (strip last _xxx)
        sid_common = sid[: sid.rindex("_")]
        urls.append(template.format(scenarioId=sid_common))
        # grade 2: full scenarioId
        urls.append(template.format(scenarioId=sid))
    return urls


def _collect_special_urls(special_stories: list[dict[str, Any]], template: str) -> list[str]:
    urls: list[str] = []
    for ss in special_stories:
        if ss.get("id") == 2:  # special case, skip id 2 per original
            continue
        for ep in ss.get("episodes", []):
            abn = ep.get("assetbundleName", "")
            sid = ep.get("scenarioId", "")
            if abn and sid:
                urls.append(template.format(assetbundleName=abn, scenarioId=sid))
    return urls


# ---------------------------------------------------------------------------
# Download logic
# ---------------------------------------------------------------------------

async def _download_one(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    url: str,
    local_path: Path,
) -> bool:
    """Download a single asset, compress with brotli and save. Returns True on success."""
    async with semaphore:
        try:
            data = await get_json(client, url)
        except Exception as exc:
            print(f"[story-asset] failed to fetch {url}: {exc}")
            return False
    compact = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    compressed = brotli.compress(compact, quality=11)
    atomic_write_bytes(local_path, compressed)
    return True


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def update_story_asset(
    lang: str = "jp",
    src: str = "sekai.best",
    output_dir: Path = Path("story_assets"),
    *,
    full: bool = False,
) -> dict[str, int]:
    urls_config = _load_urls()

    # Fetch all master data
    master_files = [
        "eventStories",
        "unitStories",
        "cards",
        "cardEpisodes",
        "actionSets",
        "characterProfiles",
        "specialStories",
    ]
    master_data: dict[str, Any] = {}
    async with create_async_client() as client:
        master_tasks = {
            name: get_json(client, _master_url(urls_config, src, lang, name))
            for name in master_files
        }
        results = await asyncio.gather(*master_tasks.values(), return_exceptions=True)
        for name, result in zip(master_tasks.keys(), results, strict=True):
            if isinstance(result, Exception):
                print(f"[story-asset] failed to fetch master {name}: {result}")
                master_data[name] = []
            else:
                master_data[name] = result

    # Build cards lookup
    cards_lookup: dict[int, dict[str, Any]] = {}
    cards_list = master_data.get("cards", [])
    if isinstance(cards_list, list):
        for card in cards_list:
            cid = card.get("id")
            if cid is not None:
                cards_lookup[cid] = card

    # Resolve asset URL templates
    event_tpl = _asset_url(urls_config, src, lang, "event_asset")
    unit_tpl = _asset_url(urls_config, src, lang, "unit_asset")
    card_tpl = _asset_url(urls_config, src, lang, "card_asset")
    talk_tpl = _asset_url(urls_config, src, lang, "talk_asset")
    self_tpl = _asset_url(urls_config, src, lang, "self_asset")
    special_tpl = _asset_url(urls_config, src, lang, "special_asset")

    # Collect all asset URLs
    all_urls: list[str] = []
    all_urls.extend(_collect_event_urls(master_data.get("eventStories", []), event_tpl))
    all_urls.extend(_collect_unit_urls(master_data.get("unitStories", []), unit_tpl))
    all_urls.extend(_collect_card_urls(master_data.get("cardEpisodes", []), cards_lookup, card_tpl))
    all_urls.extend(_collect_talk_urls(master_data.get("actionSets", []), talk_tpl))
    all_urls.extend(_collect_self_urls(master_data.get("characterProfiles", []), self_tpl))
    all_urls.extend(_collect_special_urls(master_data.get("specialStories", []), special_tpl))

    # Deduplicate
    all_urls = list(dict.fromkeys(all_urls))

    # Filter: incremental skips existing files, full re-downloads everything
    to_download: list[tuple[str, Path]] = []
    for url in all_urls:
        local_path = _url_to_local_path(url, output_dir)
        if full or not local_path.exists():
            to_download.append((url, local_path))

    skipped = len(all_urls) - len(to_download)
    mode = "full" if full else "incremental"
    print(f"[story-asset] {lang}/{src} ({mode}): total={len(all_urls)} skipped={skipped} to_download={len(to_download)}")

    # Download
    success = 0
    failed = 0
    if to_download:
        semaphore = asyncio.Semaphore(_DOWNLOAD_CONCURRENCY)
        async with create_async_client() as client:
            tasks = [
                _download_one(client, semaphore, url, path)
                for url, path in to_download
            ]
            results = await asyncio.gather(*tasks)
        for ok in results:
            if ok:
                success += 1
            else:
                failed += 1

    return {
        "total_urls": len(all_urls),
        "skipped_existing": skipped,
        "download_success": success,
        "download_failed": failed,
    }
