from __future__ import annotations

import asyncio
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import brotli
import httpx

from src.common.http import create_async_client, get_json, request_with_retry
from src.common.io import atomic_write_bytes

_URLS_JSON = Path(__file__).with_name("story_asset_urls.json")
_DOWNLOAD_CONCURRENCY = 20

# Status codes that mean "give up on this source immediately, try next one".
# 4xx = resource missing / auth issue on this CDN, no point retrying.
_FAIL_FAST_STATUS: frozenset[int] = frozenset(range(400, 500))


# ---------------------------------------------------------------------------
# Source configuration
# ---------------------------------------------------------------------------

@dataclass
class SourceConfig:
    """Per-source download behaviour. Edit these to tune retry/fallback logic."""
    name: str
    # How many attempts before giving up on this source and moving to the next.
    max_attempts: int = 3
    # Status codes that trigger an immediate switch to the next source (no retry).
    fail_fast_status: frozenset[int] = field(default_factory=lambda: _FAIL_FAST_STATUS)


DEFAULT_SOURCE_CONFIGS: list[SourceConfig] = [
    SourceConfig(name="haruki", max_attempts=3),
    SourceConfig(name="sekai.best", max_attempts=3),
]


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def _load_urls() -> dict[str, Any]:
    with _URLS_JSON.open("r", encoding="utf-8") as f:
        result: dict[str, Any] = json.load(f)
        return result


def _master_url(urls: dict[str, Any], src: str, lang: str, file: str) -> str:
    base: str = urls[src]["master"]
    lang_prefix: str = urls[src]["master_lang"][lang]
    return base.format(lang=lang_prefix, file=file)


def _asset_url(urls: dict[str, Any], src: str, lang: str, asset_type: str) -> str:
    """Return the asset URL template with {lang} already resolved."""
    template: str = urls[src][asset_type]
    lang_prefix: str = urls[src]["asset_lang"][lang]
    return template.format(lang=lang_prefix, assetbundleName="{assetbundleName}", scenarioId="{scenarioId}", group="{group}")


def _extract_asset_path(url: str, pattern: str, group: int) -> str:
    """Extract the content-relative path from a URL using the configured regex.

    The pattern and group index come from ``path_extractor`` /
    ``path_extractor_group`` in story_asset_urls.json, so you can adjust the
    extraction logic per-source without touching Python code.
    """
    m = re.search(pattern, url)
    if m:
        return m.group(group)
    raise ValueError(f"Cannot extract asset path from URL with pattern {pattern!r}: {url}")


def _url_to_local_path(url: str, output_dir: Path, pattern: str, group: int, lang: str) -> Path:
    """Map a source URL to a unified local path regardless of which CDN it came from.

    The final layout is ``output_dir/pjsk-{lang}-assets/{content_path}.br``,
    matching the ``pjsk-{lang}-assets/`` convention in get_story_pjsk.py so that
    jp and cn assets are kept in separate subdirectories, and the directory name
    is consistent regardless of which source CDN was used.

    Uses ``_extract_asset_path`` internally — override that function if you need
    a completely different path-mapping strategy.
    """
    rel = _extract_asset_path(url, pattern, group)
    return output_dir / f"pjsk-{lang}-assets" / (rel + ".br")


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
        if not isinstance(card_id, int):
            continue
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
        sid_common = sid[: sid.rindex("_")]
        urls.append(template.format(scenarioId=sid_common))
        urls.append(template.format(scenarioId=sid))
    return urls


def _collect_special_urls(special_stories: list[dict[str, Any]], template: str) -> list[str]:
    urls: list[str] = []
    for ss in special_stories:
        if ss.get("id") == 2:
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

async def _download_with_fallback(
    clients: dict[str, httpx.AsyncClient],
    semaphore: asyncio.Semaphore,
    url_by_source: list[tuple[str, str]],
    local_path: Path,
    source_configs: list[SourceConfig],
) -> bool:
    """Try each source in order, falling back on failure.

    For each source:
    - If the response status is in ``fail_fast_status``, skip to the next
      source immediately without retrying (resource likely absent on this CDN).
    - Other errors (5xx, network) are retried up to ``max_attempts`` times
      within the same source before moving on.

    Returns True if any source succeeded.
    """
    url_map = dict(url_by_source)

    async with semaphore:
        for cfg in source_configs:
            url = url_map.get(cfg.name)
            if url is None:
                continue

            client = clients.get(cfg.name)
            if client is None:
                continue

            for attempt in range(1, cfg.max_attempts + 1):
                try:
                    response = await client.get(url)
                    if response.status_code in cfg.fail_fast_status:
                        print(f"[story-asset] {cfg.name} {response.status_code} (fail-fast), switching source: {url}")
                        break  # next source
                    response.raise_for_status()
                    data = response.json()
                    compact = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                    compressed = brotli.compress(compact, quality=11)
                    atomic_write_bytes(local_path, compressed)
                    return True
                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code
                    if status in cfg.fail_fast_status:
                        print(f"[story-asset] {cfg.name} {status} (fail-fast), switching source: {url}")
                        break  # next source
                    if attempt >= cfg.max_attempts:
                        print(f"[story-asset] {cfg.name} gave up after {attempt} attempts ({status}): {url}")
                        break  # next source
                    await asyncio.sleep(0.5 * (2 ** (attempt - 1)))
                except Exception as exc:
                    if attempt >= cfg.max_attempts:
                        print(f"[story-asset] {cfg.name} gave up after {attempt} attempts ({type(exc).__name__}): {url}")
                        break  # next source
                    await asyncio.sleep(0.5 * (2 ** (attempt - 1)))

    print(f"[story-asset] all sources failed for: {local_path}")
    return False


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def update_story_asset(
    lang: str = "jp",
    srcs: list[str] | None = None,
    output_dir: Path = Path("story_assets"),
    *,
    full: bool = False,
    source_configs: list[SourceConfig] | None = None,
) -> dict[str, int]:
    """Download story assets from multiple sources with automatic fallback.

    Args:
        lang: Language code (``jp``, ``cn``, ``tw``).
        srcs: Ordered list of source names to try. Defaults to
            ``["haruki", "sekai.best"]``.  Add new sources here as they become
            available — as long as they exist in ``story_asset_urls.json``.
        output_dir: Root directory for saved ``.br`` files.
        full: Re-download even if the local file already exists.
        source_configs: Override per-source retry/fail-fast behaviour.
            Defaults to :data:`DEFAULT_SOURCE_CONFIGS`.
    """
    if srcs is None:
        srcs = ["haruki", "sekai.best"]
    if source_configs is None:
        source_configs = DEFAULT_SOURCE_CONFIGS

    # Only keep configs whose source is actually requested
    active_configs = [c for c in source_configs if c.name in srcs]
    # Preserve the order of srcs for any sources not in source_configs
    cfg_names = {c.name for c in active_configs}
    for name in srcs:
        if name not in cfg_names:
            active_configs.append(SourceConfig(name=name))

    urls_config = _load_urls()

    # Use the first source's path_extractor for local path mapping
    primary_src = srcs[0]
    path_pattern: str = urls_config[primary_src]["path_extractor"]
    path_group: int = urls_config[primary_src]["path_extractor_group"]

    # Fetch master data — try sources in order until one succeeds
    master_files = [
        "eventStories",
        "unitStories",
        "cards",
        "cardEpisodes",
        "actionSets",
        "characterProfiles",
        "specialStories",
    ]

    async def _fetch_master(name: str) -> Any:
        for src in srcs:
            try:
                async with create_async_client() as client:
                    return await get_json(client, _master_url(urls_config, src, lang, name))
            except Exception as exc:
                print(f"[story-asset] master {name} failed on {src}: {exc}")
        print(f"[story-asset] master {name}: all sources failed, using empty list")
        return []

    master_data: dict[str, Any] = {}
    master_results = await asyncio.gather(*[_fetch_master(n) for n in master_files])
    for name, result in zip(master_files, master_results):
        master_data[name] = result

    # Build cards lookup
    cards_lookup: dict[int, dict[str, Any]] = {}
    for card in master_data.get("cards", []):
        cid = card.get("id")
        if cid is not None:
            cards_lookup[cid] = card

    # Collect asset URLs per source, then zip into (local_path -> {src: url}) mapping
    # Each source produces the same logical set of files, just at different URLs.
    path_to_urls: dict[Path, list[tuple[str, str]]] = {}

    for src in srcs:
        event_tpl = _asset_url(urls_config, src, lang, "event_asset")
        unit_tpl = _asset_url(urls_config, src, lang, "unit_asset")
        card_tpl = _asset_url(urls_config, src, lang, "card_asset")
        talk_tpl = _asset_url(urls_config, src, lang, "talk_asset")
        self_tpl = _asset_url(urls_config, src, lang, "self_asset")
        special_tpl = _asset_url(urls_config, src, lang, "special_asset")

        src_urls: list[str] = []
        src_urls.extend(_collect_event_urls(master_data.get("eventStories", []), event_tpl))
        src_urls.extend(_collect_unit_urls(master_data.get("unitStories", []), unit_tpl))
        src_urls.extend(_collect_card_urls(master_data.get("cardEpisodes", []), cards_lookup, card_tpl))
        src_urls.extend(_collect_talk_urls(master_data.get("actionSets", []), talk_tpl))
        src_urls.extend(_collect_self_urls(master_data.get("characterProfiles", []), self_tpl))
        src_urls.extend(_collect_special_urls(master_data.get("specialStories", []), special_tpl))
        src_urls = list(dict.fromkeys(src_urls))

        src_pattern: str = urls_config[src]["path_extractor"]
        src_group: int = urls_config[src]["path_extractor_group"]

        for url in src_urls:
            try:
                local_path = _url_to_local_path(url, output_dir, src_pattern, src_group, lang)
            except ValueError as exc:
                print(f"[story-asset] skipping unrecognised URL: {exc}")
                continue
            path_to_urls.setdefault(local_path, []).append((src, url))

    # Filter: skip existing files unless full re-download requested
    to_download = [
        (local_path, url_pairs)
        for local_path, url_pairs in path_to_urls.items()
        if full or not local_path.exists()
    ]

    total = len(path_to_urls)
    skipped = total - len(to_download)
    mode = "full" if full else "incremental"
    print(f"[story-asset] {lang} srcs={srcs} ({mode}): total={total} skipped={skipped} to_download={len(to_download)}")

    # Download with per-source clients
    success = 0
    failed = 0
    if to_download:
        semaphore = asyncio.Semaphore(_DOWNLOAD_CONCURRENCY)
        clients = {src: create_async_client() for src in srcs}
        try:
            # Enter all clients
            entered: dict[str, httpx.AsyncClient] = {}
            for src, client in clients.items():
                entered[src] = await client.__aenter__()

            tasks = [
                _download_with_fallback(entered, semaphore, url_pairs, local_path, active_configs)
                for local_path, url_pairs in to_download
            ]
            results = await asyncio.gather(*tasks)
        finally:
            for client in clients.values():
                await client.__aexit__(None, None, None)

        for ok in results:
            if ok:
                success += 1
            else:
                failed += 1

    return {
        "total_urls": total,
        "skipped_existing": skipped,
        "download_success": success,
        "download_failed": failed,
    }
