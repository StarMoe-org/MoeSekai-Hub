"""四格漫画元数据抓取。

数据源优先级：MoeSekai 情报局（5441521）是主源，新话比资讯站早约 10 小时发布，
话号、标题、URL 均以它为准；资讯站官方号（13148307）是补充源，只用来补齐
情报局没有保留的历史连载（第 6~374 话等），且不得覆盖已有条目的 URL 数据。

主系列严格分类：只有 A 类（精确标记 + 「第N话「标题」」）写入 mangas.json；
B 类旁支/联动/特别篇（KEI 一周年、皓乐齿联动、公测连载、MIKU 特别篇等）与
J 类干扰动态、Z 类空正文全部丢弃，避免它们占用主系列话号造成编号冲突。
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from PIL import Image

from src.common.http import RetryConfig, create_async_client, get_bytes, get_json
from src.common.io import atomic_write_bytes, read_json, write_json

MANGA_API_TEMPLATE = (
    "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space/search"
    "?host_mid={mid}&page={page}&offset={page}&keyword=%E6%BC%AB%E7%94%BB"
    "&features=itemOpusStyle,opusBigCover,forwardListHidden"
)

# 主源：MoeSekai 情报局。新话比资讯站早约 10 小时发布，但只保留最近若干话。
MANGA_PRIMARY_MID = 5441521
# 补充源：资讯站官方号。第 6~374 话等历史连载只在它这里，用来补旧。
MANGA_SUPPLEMENT_MID = 13148307
# 常量开关：是否抓取补充源。关闭后只用情报局；已入库的历史条目靠上一轮数据保留。
MANGA_ENABLE_SUPPLEMENT = True

# 官方号打 #SEKAI四格漫画#，情报局打 #四格漫画#。
MANGA_TAGS = frozenset({"#SEKAI四格漫画#", "#SEKAI四格漫畫#", "#四格漫画#", "#四格漫畫#"})
FOUR_PANEL_HINTS = ("四格漫画", "四格漫畫")

# 主系列（A 类）的精确标记。旁支/联动/特别篇正文里没有这两个前缀。
MAIN_SERIES_MARKERS = (
    "描绘《世界计划》日常的四格漫画公开",
    "描绘《Project SEKAI》日常的四格漫画公开",
)

# 分类标签（见 classify_manga_post）：
# A 主系列：精确标记 + 第N话「标题」，唯一允许入库的类别
# B 旁支：带四格漫画标签但没有主系列标记，或主系列标记缺话号（联动/周年/特别篇/连载预告）
# J 干扰：角色介绍、活动剧情、生日贺图等非连载动态
# Z 空帖：正文为空
CLASS_MAIN = "A"
CLASS_SIDE = "B"
CLASS_JUNK = "J"
CLASS_EMPTY = "Z"

# 「第N话」之后允许出现一小段限定语（如「联动特别篇」），再跟 「标题」。
# 情报局的写法是「第389话 ——「成为压倒性的存在 」」，破折号与空格由限定语吃掉。
# 标题本体不按 '#' 截断——部分标题本身以 '#' 开头（如「第43话「#生日 #跟朋友一起」」）。
TITLE_PATTERN = re.compile(
    r"第\s*(?P<number>\d+)\s*话(?P<qualifier>[^「」『』\n]{0,12}?)"
    r"[「『](?P<title>[^」』\n]+)[」』]"
)
CONTRIBUTOR_PATTERN = re.compile(r"(?P<role>[^\s：:#]{1,12})\s*[：:]\s*(?P<name>[^：:\n#]+)")
# 贡献者角色名只允许由这些字组成，用来挡掉正文里的 URL / BV 号等噪声键
# （情报局正文含「本期活动剧情：BV...」「生日贺图：https://...」等）。
CONTRIBUTOR_ROLE_CHARS = frozenset("翻译校对嵌字&、＆和及/／,，")
DOWNLOAD_CONCURRENCY = 16
PAGE_UPPER_BOUND = 500
NO_PROGRESS_PAGE_LIMIT = 8


@dataclass(frozen=True)
class MangaSource:
    """一个四格漫画数据源。"""

    mid: int
    label: str


MANGA_PRIMARY_SOURCE = MangaSource(mid=MANGA_PRIMARY_MID, label="情报局")
MANGA_SUPPLEMENT_SOURCE = MangaSource(mid=MANGA_SUPPLEMENT_MID, label="资讯站")
# 同话号冲突时的源优先级：数字越小越权威。主源永远压过补充源。
SOURCE_PRIORITY: dict[int, int] = {MANGA_PRIMARY_MID: 0, MANGA_SUPPLEMENT_MID: 1}

MANGA_IMAGE_FORMAT = "webp"
WEBP_QUALITY = 90
WEBP_METHOD = 6


def encode_image_as_webp(image_bytes: bytes) -> bytes:
    """Decode raw image bytes and re-encode as WebP.

    Raises on any decode/encode error so callers can treat it as a failed download.
    """
    with Image.open(BytesIO(image_bytes)) as image:
        rgb = image.convert("RGB")
        buffer = BytesIO()
        rgb.save(buffer, format="WEBP", quality=WEBP_QUALITY, method=WEBP_METHOD)
        return buffer.getvalue()


def parse_title_and_number(content: str) -> tuple[int, str] | None:
    match = TITLE_PATTERN.search(content)
    if match is None:
        return None
    number = int(match.group("number"))
    title = match.group("title").strip().strip("「」『』\"'[]")
    if not title:
        return None
    return number, title


def classify_manga_post(content: str) -> str:
    """给一条动态打上 A/B/J/Z 分类标签。

    A 主系列：含精确标记「描绘《世界计划》/《Project SEKAI》日常的四格漫画公开」，
        且能解析出「第N话「标题」」。只有这一类允许写入 mangas.json。
    B 旁支：主题仍是四格漫画（带四格漫画标签/字样，或有主系列标记但缺话号），
        包括 KEI 一周年、皓乐齿联动、公测连载、MIKU 特别篇、连载预告等。
        它们自带 1..6 这类小话号，会和主系列话号正面冲突，必须整类丢弃。
    J 干扰：其余动态（角色介绍、活动剧情、生日贺图等）。
    Z 空帖：正文为空。
    """
    if not content or not content.strip():
        return CLASS_EMPTY
    if any(marker in content for marker in MAIN_SERIES_MARKERS):
        return CLASS_MAIN if parse_title_and_number(content) is not None else CLASS_SIDE
    if any(tag in content for tag in MANGA_TAGS) or any(hint in content for hint in FOUR_PANEL_HINTS):
        return CLASS_SIDE
    return CLASS_JUNK


def _is_contributor_role(role: str) -> bool:
    """贡献者角色名只可能是翻译/校对/嵌字及其组合。

    正文里还有「本期活动剧情：BV...」「生日贺图：https://...」这类结构，
    冒号前的内容会被正则当成角色名，需要挡掉。
    """
    if not role or role.startswith("第") or "话" in role:
        return False
    return all(char in CONTRIBUTOR_ROLE_CHARS for char in role)


def parse_contributors(content: str) -> dict[str, str]:
    contributors: dict[str, str] = {}
    for match in CONTRIBUTOR_PATTERN.finditer(content):
        role = match.group("role").strip()
        name = match.group("name").strip()
        if not _is_contributor_role(role) or not name:
            continue
        contributors[role] = name
    return contributors


def normalize_bilibili_url(url: str) -> str:
    if not url:
        return url
    if url.startswith("//"):
        return "https:" + url
    return url


def parse_manga_item(item: dict[str, Any], source: MangaSource) -> dict[str, Any] | None:
    """解析单条动态；只接受主系列（A 类）。

    旁支/联动/特别篇（B 类）自带 1..6 这类小话号，会和主系列话号冲突，
    因此在这里整类挡掉，编号冲突从源头上不会发生。
    """
    try:
        opus = item["modules"]["module_dynamic"]["major"]["opus"]
        content = opus["summary"]["text"]
        if not isinstance(content, str) or classify_manga_post(content) != CLASS_MAIN:
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
        "sourceuid": source.mid,
    }


def _conflict_rank(item: dict[str, Any]) -> tuple[int, int]:
    """同话号冲突的排序键，越小越优先。

    先比数据源（主源情报局 0 < 补充源资讯站 1），再比发布时间（新的在前）。
    这样资讯站的同话数据永远压不过情报局，也就不会覆盖主源 URL。
    """
    mid = item.get("sourceuid")
    source_rank = SOURCE_PRIORITY.get(mid, len(SOURCE_PRIORITY)) if isinstance(mid, int) else len(SOURCE_PRIORITY)
    publish_ts = item.get("date")
    date_rank = -publish_ts if isinstance(publish_ts, int) else 0
    return source_rank, date_rank


def resolve_number_conflict(existing: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    """同话号冲突裁决：返回两者中应保留的一条。"""
    return candidate if _conflict_rank(candidate) < _conflict_rank(existing) else existing


def merge_metadata(*sources: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """按话号合并多个数据源，同话号交给 resolve_number_conflict 裁决。"""
    merged: dict[str, dict[str, Any]] = {}
    for source in sources:
        for key, item in source.items():
            existing = merged.get(key)
            merged[key] = item if existing is None else resolve_number_conflict(existing, item)
    return merged


async def fetch_source_metadata(
    source: MangaSource,
    cookie: str = "",
) -> dict[str, dict[str, Any]]:
    """抓取单个数据源的四格漫画元数据，按话数索引。"""
    headers: dict[str, str] = {}
    if cookie:
        headers["cookie"] = cookie

    mangas: dict[str, dict[str, Any]] = {}
    no_progress_pages = 0
    previous_page_ids: tuple[str, ...] = ()

    async with create_async_client(headers=headers) as client:
        for page in range(1, PAGE_UPPER_BOUND + 1):
            api_url = MANGA_API_TEMPLATE.format(mid=source.mid, page=page)
            payload = await get_json(client, api_url, retry_config=RetryConfig(attempts=6))
            # B 站风控时返回 code != 0 且 data 为 null；get_json 不校验业务码。
            if not isinstance(payload, dict) or payload.get("code") != 0:
                error_code = payload.get("code") if isinstance(payload, dict) else "invalid payload"
                raise RuntimeError(f"Bilibili API returned an error for {source.label}: {error_code}")
            data = payload.get("data")
            items = data.get("items") if isinstance(data, dict) else None
            if not items:
                break

            # 风控/限流时接口会重复吐同一页；这种重复会被 no-progress 逻辑静默截断，
            # 导致只抓到前几页就写库，必须显式报错。
            page_ids = tuple(str(item.get("id", "")) for item in items if isinstance(item, dict))
            if page_ids and page_ids == previous_page_ids:
                raise RuntimeError(f"Bilibili API returned a repeated page for {source.label}: page {page}")
            previous_page_ids = page_ids

            new_count = 0
            for item in items:
                if not isinstance(item, dict):
                    continue
                manga = parse_manga_item(item, source)
                if manga is None:
                    continue
                manga_id = str(manga["id"])
                existing = mangas.get(manga_id)
                if existing is None:
                    mangas[manga_id] = manga
                    new_count += 1
                    continue
                # 同一话可能出现在多条动态里，按话号冲突裁决挑一条。
                if resolve_number_conflict(existing, manga) is manga:
                    mangas[manga_id] = manga
                    new_count += 1

            if new_count == 0:
                no_progress_pages += 1
            else:
                no_progress_pages = 0
            if no_progress_pages >= NO_PROGRESS_PAGE_LIMIT:
                break

            if isinstance(data, dict) and data.get("has_more") is False:
                break

    return mangas


async def fetch_manga_metadata(cookie: str = "") -> dict[str, dict[str, Any]]:
    """按话数合并主源与补充源。

    MoeSekai 情报局（主源）是权威：同一话两边都有时保留情报局的 URL 与标题。
    资讯站官方号（补充源）只补情报局没保留的历史旧话（第 6~374 话等）。
    补充源可由 MANGA_ENABLE_SUPPLEMENT 常量关闭。
    """
    primary = await fetch_source_metadata(MANGA_PRIMARY_SOURCE, cookie=cookie)

    supplement: dict[str, dict[str, Any]] = {}
    if MANGA_ENABLE_SUPPLEMENT:
        supplement = await fetch_source_metadata(MANGA_SUPPLEMENT_SOURCE, cookie=cookie)

    merged = merge_metadata(primary, supplement)

    if not merged:
        raise RuntimeError("No manga metadata collected from any source; refusing to overwrite existing data")

    return dict(sorted(merged.items(), key=lambda item: item[1]["id"], reverse=True))


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
        webp_bytes = encode_image_as_webp(image_bytes)
        atomic_write_bytes(target_path, webp_bytes)
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

        target_path = output_dir / f"{manga_id}.{MANGA_IMAGE_FORMAT}"
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


def _has_sourceuid(item: Any) -> bool:
    """条目是否带可信的来源标记（迁移前的旧数据没有该字段）。"""
    return isinstance(item, dict) and isinstance(item.get("sourceuid"), int)


def merge_with_previous_metadata(
    metadata: dict[str, dict[str, Any]],
    previous_metadata: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    """把上一轮已入库的条目并回来，并保证补充源只补缺、不覆盖。

    - 主源（情报局）抓到的话直接覆盖上一轮：它是权威，URL 变了要触发重新下载。
    - 补充源（资讯站）遇到上一轮已存在的话号时整条跳过，既有 url/manga/sourceuid
      全部保持原样，资讯站的历史 opus 链接顶不掉情报局的新话数据。
    - 上一轮有、这一轮没抓到的历史条目原样保留，不会因搜索接口翻页上限而消失。
    - 旧的 mangas.json 没有 sourceuid，无法证明是主系列，按一次性迁移丢弃。
    """
    merged = dict(previous_metadata)
    for key, item in metadata.items():
        previous_item = merged.get(key)
        # 只有「带 sourceuid 的可信历史条目」才享有不被补充源覆盖的保护；
        # 无 sourceuid 的遗留条目随后会被丢弃，不能挡住补充源补齐的旧话。
        if _has_sourceuid(previous_item) and item.get("sourceuid") != MANGA_PRIMARY_MID:
            continue
        merged[key] = item

    # 一次性迁移：旧的 mangas.json 没有 sourceuid，无法证明是主系列，其中第 1~6 话
    # 被 KEI 一周年/皓乐齿联动等 B 类同号条目占着。两个源本轮合起来已重抓全部话号，
    # 因此丢弃这些无来源标记的历史条目；之后每条都带 sourceuid，这条规则自动失效。
    for key in [key for key, item in merged.items() if not _has_sourceuid(item)]:
        del merged[key]
    return merged


async def update_manga(
    metadata_path: Path = Path("mangas/mangas.json"),
    output_dir: Path = Path("mangas"),
    cookie: str | None = None,
) -> dict[str, int]:
    resolved_cookie = cookie if cookie is not None else os.getenv("BILIBILI_COOKIE", "")
    previous_metadata_raw = read_json(metadata_path, default={})
    previous_metadata = previous_metadata_raw if isinstance(previous_metadata_raw, dict) else {}

    metadata = await fetch_manga_metadata(cookie=resolved_cookie)
    # 补历史条目 + 挡住补充源对既有 URL 的覆盖。
    metadata = merge_with_previous_metadata(metadata, previous_metadata)
    download_stats, failed_changed_url_ids = await download_manga_images(metadata, previous_metadata, output_dir)
    metadata_to_persist = build_metadata_to_persist(metadata, previous_metadata, failed_changed_url_ids)
    write_json(metadata_path, metadata_to_persist)
    return {
        "metadata_total": len(metadata_to_persist),
        **download_stats,
    }
