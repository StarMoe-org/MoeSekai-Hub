from __future__ import annotations

import asyncio
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

from parsel import Selector

from src.common.http import create_async_client, get_json, get_text, request_with_retry
from src.common.io import utc_now_iso, write_json

MOEGIRL_EVENTS_URL = (
    "https://mzh.moegirl.org.cn/"
    "%E4%B8%96%E7%95%8C%E8%AE%A1%E5%88%92_%E5%BD%A9%E8%89%B2%E8%88%9E%E5%8F%B0_"
    "feat._%E5%88%9D%E9%9F%B3%E6%9C%AA%E6%9D%A5/%E5%8E%86%E5%8F%B2%E6%B4%BB%E5%8A%A8"
)
EVENTS_API_URL = "https://database.pjsekai.moe/events.json"

TITLE_PATTERN = re.compile(
    r"^/?\s*\d+\s*[、\.．]\s*(?P<translate>.+?)\s*/\s*(?P<original>.+?)\s*$"
)
TITLE_FALLBACK_PATTERN = re.compile(r"^(?P<translate>.+?)\s*/\s*(?P<original>.+)$")
PARENTHESIS_CONTENT_PATTERN = re.compile(r"[（(][^）)]*[）)]")
BV_PATTERN = re.compile(r"\b(BV[0-9A-Za-z]{10})\b")

SYMBOL_TRANSLATION_TABLE = str.maketrans(
    {
        "’": "'",
        "‘": "'",
        "“": '"',
        "”": '"',
        "～": "~",
        "—": "-",
        "–": "-",
        "：": ":",
        "！": "!",
        "？": "?",
        "（": "(",
        "）": ")",
        "【": "[",
        "】": "]",
        "「": "[",
        "」": "]",
        "『": "[",
        "』": "]",
        "・": "",
        "·": "",
        "。": "",
        "、": "",
        ",": "",
        ".": "",
        "!": "",
        "?": "",
        "'": "",
        '"': "",
        "[": "",
        "]": "",
        "(": "",
        ")": "",
        "-": "",
        ":": "",
        ";": "",
        "…": "",
        "　": "",
    }
)


@dataclass
class WikiEventEntry:
    translate: str
    original: str
    bilibili_url: str | None
    bvid: str | None


def normalize_event_name(name: str) -> str:
    normalized = unicodedata.normalize("NFKC", name).lower().strip()
    normalized = re.sub(r"\s+", "", normalized)
    return normalized.translate(SYMBOL_TRANSLATION_TABLE)


def extract_bvid(url: str) -> str | None:
    if not url:
        return None

    direct_match = BV_PATTERN.search(url)
    if direct_match:
        return direct_match.group(1)

    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    for key in ("bvid", "BVID"):
        values = query.get(key, [])
        for value in values:
            nested_match = BV_PATTERN.search(value)
            if nested_match:
                return nested_match.group(1)
    return None


def parse_event_title(raw_title: str) -> tuple[str, str] | None:
    compact = " ".join(raw_title.split())
    for pattern in (TITLE_PATTERN, TITLE_FALLBACK_PATTERN):
        match = pattern.match(compact)
        if match is None:
            continue
        translate = match.group("translate").strip()
        original = match.group("original").strip()
        original = PARENTHESIS_CONTENT_PATTERN.sub("", original).strip()
        if translate and original:
            return translate, original
    return None


def parse_wiki_entries(page_html: str) -> list[WikiEventEntry]:
    selector = Selector(text=page_html)
    rows = selector.xpath('//tr[th[@colspan="4" and b]]')
    entries: list[WikiEventEntry] = []

    for row in rows:
        raw_title = row.xpath('string(./th[@colspan="4"]/b)').get("") or ""
        parsed_title = parse_event_title(raw_title)
        if parsed_title is None:
            continue

        translate, original = parsed_title
        href = (row.xpath("./following-sibling::tr[1]/td[3]//a/@href").get("") or "").strip()
        bilibili_url = urljoin(MOEGIRL_EVENTS_URL, href) if href else None
        bvid = extract_bvid(bilibili_url or "")

        entries.append(
            WikiEventEntry(
                translate=translate,
                original=original,
                bilibili_url=bilibili_url,
                bvid=bvid,
            )
        )

    return entries


def _prefer_entry(current: WikiEventEntry, candidate: WikiEventEntry) -> WikiEventEntry:
    if current.bilibili_url is None and candidate.bilibili_url is not None:
        return candidate
    if current.bvid is None and candidate.bvid is not None:
        return candidate
    return current


def build_entry_maps(entries: list[WikiEventEntry]) -> tuple[dict[str, WikiEventEntry], dict[str, WikiEventEntry]]:
    exact_map: dict[str, WikiEventEntry] = {}
    normalized_map: dict[str, WikiEventEntry] = {}

    for entry in entries:
        if entry.original in exact_map:
            exact_map[entry.original] = _prefer_entry(exact_map[entry.original], entry)
        else:
            exact_map[entry.original] = entry

        normalized_key = normalize_event_name(entry.original)
        if not normalized_key:
            continue
        if normalized_key in normalized_map:
            normalized_map[normalized_key] = _prefer_entry(normalized_map[normalized_key], entry)
        else:
            normalized_map[normalized_key] = entry

    return exact_map, normalized_map


def match_event_name(
    event_name: str,
    exact_map: dict[str, WikiEventEntry],
    normalized_map: dict[str, WikiEventEntry],
) -> tuple[WikiEventEntry | None, str]:
    exact = exact_map.get(event_name)
    if exact is not None:
        return exact, "exact"

    normalized = normalized_map.get(normalize_event_name(event_name))
    if normalized is not None:
        return normalized, "normalized"

    return None, "unmatched"


async def _resolve_short_link(entry: WikiEventEntry) -> WikiEventEntry:
    if not entry.bilibili_url or entry.bvid:
        return entry
    host = urlparse(entry.bilibili_url).netloc.lower()
    if host not in {"b23.tv", "www.b23.tv", "bili2233.cn", "www.bili2233.cn"}:
        return entry

    async with create_async_client() as client:
        response = await request_with_retry(client, "GET", entry.bilibili_url)
    final_url = str(response.url)
    return WikiEventEntry(
        translate=entry.translate,
        original=entry.original,
        bilibili_url=final_url,
        bvid=extract_bvid(final_url),
    )


async def resolve_short_links(entries: list[WikiEventEntry], concurrency: int = 8) -> list[WikiEventEntry]:
    semaphore = asyncio.Semaphore(concurrency)
    resolved: list[WikiEventEntry] = [entry for entry in entries]

    async def resolve_one(index: int, item: WikiEventEntry) -> None:
        async with semaphore:
            try:
                resolved[index] = await _resolve_short_link(item)
            except Exception:
                resolved[index] = item

    await asyncio.gather(*(resolve_one(index, item) for index, item in enumerate(entries)))
    return resolved


def build_event_payloads(
    events: list[dict[str, Any]],
    exact_map: dict[str, WikiEventEntry],
    normalized_map: dict[str, WikiEventEntry],
) -> tuple[dict[str, Any], dict[str, Any]]:
    merged_events: list[dict[str, Any]] = []
    unmatched_events: list[dict[str, Any]] = []

    for event in sorted(events, key=lambda row: int(row.get("id", 0))):
        event_id_raw = event.get("id")
        event_name_raw = event.get("name")
        if not isinstance(event_id_raw, int) or not isinstance(event_name_raw, str):
            continue

        matched_entry, status = match_event_name(event_name_raw, exact_map, normalized_map)
        bilibili_url = matched_entry.bilibili_url if matched_entry else None
        bvid = matched_entry.bvid if matched_entry else None

        merged_events.append(
            {
                "event_id": event_id_raw,
                "event_name": event_name_raw,
                "bilibili_url": bilibili_url,
                "bvid": bvid,
                "match_status": status,
            }
        )
        if status == "unmatched":
            unmatched_events.append(
                {
                    "event_id": event_id_raw,
                    "event_name": event_name_raw,
                }
            )

    generated_at = utc_now_iso()
    source = {
        "wiki_page": MOEGIRL_EVENTS_URL,
        "events_api": EVENTS_API_URL,
    }

    main_payload = {
        "generated_at": generated_at,
        "source": source,
        "events": merged_events,
    }
    unmatched_payload = {
        "generated_at": generated_at,
        "source": source,
        "unmatched_events": unmatched_events,
    }
    return main_payload, unmatched_payload


async def update_event_bvid(
    output_path: Path = Path("data/event_bvid/events_bilibili.json"),
    unmatched_path: Path = Path("data/event_bvid/unmatched_events.json"),
) -> dict[str, int]:
    wiki_headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": "https://mzh.moegirl.org.cn/",
    }
    async with create_async_client(headers=wiki_headers) as client:
        wiki_html = await get_text(client, MOEGIRL_EVENTS_URL)
        events_data = await get_json(client, EVENTS_API_URL)

    entries = parse_wiki_entries(wiki_html)
    entries = await resolve_short_links(entries)
    exact_map, normalized_map = build_entry_maps(entries)

    if not isinstance(events_data, list):
        raise ValueError("Invalid events API payload: expected a list")
    main_payload, unmatched_payload = build_event_payloads(events_data, exact_map, normalized_map)

    write_json(output_path, main_payload)
    write_json(unmatched_path, unmatched_payload)
    return {
        "wiki_entries": len(entries),
        "events_total": len(main_payload["events"]),
        "events_unmatched": len(unmatched_payload["unmatched_events"]),
    }

