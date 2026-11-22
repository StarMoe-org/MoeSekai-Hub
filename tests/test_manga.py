from __future__ import annotations

import asyncio
from typing import Any

import pytest

from src.tasks import manga as manga_module
from src.tasks.manga import (
    CLASS_EMPTY,
    CLASS_JUNK,
    CLASS_MAIN,
    CLASS_SIDE,
    MANGA_PRIMARY_MID,
    MANGA_SUPPLEMENT_MID,
    MangaSource,
    build_metadata_to_persist,
    classify_manga_post,
    merge_metadata,
    merge_with_previous_metadata,
    parse_contributors,
    parse_manga_item,
    parse_title_and_number,
    resolve_number_conflict,
)

PRIMARY_SOURCE = MangaSource(mid=MANGA_PRIMARY_MID, label="情报局")
SUPPLEMENT_SOURCE = MangaSource(mid=MANGA_SUPPLEMENT_MID, label="资讯站")

MAIN_SERIES_TEXT = "描绘《世界计划》日常的四格漫画公开✨\n\n第389话 ——「成为压倒性的存在 」！\n"
SIDE_SERIES_TEXT = "[世界计划表情包_比心]四格漫画第1话「欢迎来到SEKAI！」\n#SEKAI四格漫画#\n"
JUNK_TEXT = "📝一张就能看懂！ \n MORE MORE JUMP ！🍀成员的介绍✨\n"


def _make_item(
    *,
    text: str,
    image_url: str = "http://i0.hdslb.com/bfs/new_dyn/abc.jpg",
    pub_ts: int = 1700000000,
) -> dict[str, Any]:
    return {
        "modules": {
            "module_author": {"pub_ts": pub_ts},
            "module_dynamic": {
                "major": {
                    "opus": {
                        "summary": {"text": text},
                        "pics": [{"url": image_url}],
                        "jump_url": "//www.bilibili.com/opus/123",
                    }
                }
            },
        }
    }


def _record(
    number: int,
    mid: int,
    *,
    date: int = 1700000000,
    image: str | None = None,
) -> dict[str, Any]:
    image_url = image or f"https://example.com/{mid}/{number}.jpg"
    return {
        "id": number,
        "title": f"标题{number}",
        "manga": image_url,
        "date": date,
        "url": f"https://www.bilibili.com/opus/{mid}-{number}",
        "contributors": {},
        "sourceuid": mid,
    }


def _source_metadata(mid: int, *, count: int, start: int = 1, date: int = 1700000000) -> dict[str, dict[str, Any]]:
    return {str(number): _record(number, mid, date=date) for number in range(start, start + count)}


# ---------------------------------------------------------------- 标题解析


def test_parse_title_and_number_handles_supplement_source_dash_format() -> None:
    assert parse_title_and_number(MAIN_SERIES_TEXT) == (389, "成为压倒性的存在")


def test_parse_title_and_number_handles_primary_source_format() -> None:
    content = "[世界计划表情包_比心]第389话「成为压倒性的存在」[世界计划表情包_喜欢]\n"

    assert parse_title_and_number(content) == (389, "成为压倒性的存在")


def test_parse_title_and_number_keeps_hash_prefixed_title() -> None:
    content = "第43话「#生日 #跟朋友一起 」\n"

    assert parse_title_and_number(content) == (43, "#生日 #跟朋友一起")


def test_parse_title_and_number_ignores_special_episode_without_number() -> None:
    content = "描绘《世界计划》日常的四格漫画公开✨ \n\n四格漫画特别篇 ——「六周年感谢祭」\n"

    assert parse_title_and_number(content) is None


# ---------------------------------------------------------------- A/B/J/Z 分类


def test_classify_main_series_with_world_plan_marker() -> None:
    assert classify_manga_post(MAIN_SERIES_TEXT) == CLASS_MAIN


def test_classify_main_series_with_project_sekai_marker() -> None:
    content = "描绘《Project SEKAI》日常的四格漫画公开✨\n第94话「文化祭的前一天」\n"

    assert classify_manga_post(content) == CLASS_MAIN


def test_classify_side_series_without_main_marker() -> None:
    assert classify_manga_post(SIDE_SERIES_TEXT) == CLASS_SIDE


def test_classify_side_series_when_main_marker_has_no_number() -> None:
    content = "描绘《世界计划》日常的四格漫画公开✨\n四格漫画连载开始啦！\n"

    assert classify_manga_post(content) == CLASS_SIDE


def test_classify_junk_post() -> None:
    assert classify_manga_post(JUNK_TEXT) == CLASS_JUNK


def test_classify_empty_post() -> None:
    assert classify_manga_post("  \n\t") == CLASS_EMPTY


# ---------------------------------------------------------------- 分类落地：只有 A 类入库


def test_parse_manga_item_accepts_main_series_and_records_primary_sourceuid() -> None:
    item = _make_item(text=MAIN_SERIES_TEXT)

    parsed = parse_manga_item(item, PRIMARY_SOURCE)

    assert parsed is not None
    assert parsed["id"] == 389
    assert parsed["title"] == "成为压倒性的存在"
    assert parsed["manga"] == "http://i0.hdslb.com/bfs/new_dyn/abc.jpg"
    assert parsed["url"] == "https://www.bilibili.com/opus/123"
    assert parsed["sourceuid"] == MANGA_PRIMARY_MID


def test_parse_manga_item_records_supplement_sourceuid() -> None:
    item = _make_item(text=MAIN_SERIES_TEXT)

    parsed = parse_manga_item(item, SUPPLEMENT_SOURCE)

    assert parsed is not None
    assert parsed["sourceuid"] == MANGA_SUPPLEMENT_MID


def test_parse_manga_item_rejects_side_series_occupying_main_series_number() -> None:
    # KEI 一周年第 1 话与主系列第 1 话同号，这正是历史数据被污染的成因。
    item = _make_item(text=SIDE_SERIES_TEXT)

    assert parse_manga_item(item, PRIMARY_SOURCE) is None


def test_parse_manga_item_rejects_character_intro_post() -> None:
    item = _make_item(text=JUNK_TEXT)

    assert parse_manga_item(item, PRIMARY_SOURCE) is None


def test_parse_contributors_drops_url_and_bvid_noise_keys() -> None:
    content = (
        "翻译：@雪莹ちゃん \n"
        "嵌字：@岓诚 \n"
        "本期活动剧情：BV1GPWm6JEES \n"
        "生日贺图：https://www.bilibili.com/opus/1269467332028137475\n"
        "转载：https://example.com\n"
    )

    contributors = parse_contributors(content)

    assert contributors == {"翻译": "@雪莹ちゃん", "嵌字": "@岓诚"}


def test_parse_contributors_keeps_combined_roles() -> None:
    content = "校对&嵌字：某某@某组 \n翻译&嵌字：另一个人@某组\n"

    contributors = parse_contributors(content)

    assert contributors == {"校对&嵌字": "某某@某组", "翻译&嵌字": "另一个人@某组"}


# ---------------------------------------------------------------- 编号冲突裁决


def test_resolve_number_conflict_prefers_primary_source_even_when_supplement_is_newer() -> None:
    official = _record(375, MANGA_PRIMARY_MID, date=100)
    guide = _record(375, MANGA_SUPPLEMENT_MID, date=999)

    assert resolve_number_conflict(official, guide) is official
    assert resolve_number_conflict(guide, official) is official


def test_resolve_number_conflict_prefers_newer_post_within_same_source() -> None:
    older = _record(375, MANGA_PRIMARY_MID, date=100, image="https://example.com/old.jpg")
    newer = _record(375, MANGA_PRIMARY_MID, date=200, image="https://example.com/new.jpg")

    assert resolve_number_conflict(older, newer) is newer
    assert resolve_number_conflict(newer, older) is newer


def test_merge_metadata_keeps_primary_urls_on_overlap() -> None:
    primary = _source_metadata(MANGA_PRIMARY_MID, count=2, start=1)
    supplement = _source_metadata(MANGA_SUPPLEMENT_MID, count=2, start=1, date=1800000000)

    merged = merge_metadata(primary, supplement)

    assert merged["1"]["sourceuid"] == MANGA_PRIMARY_MID
    assert merged["1"]["manga"] == primary["1"]["manga"]


# ---------------------------------------------------------------- 双源合并


def test_fetch_manga_metadata_prefers_primary_source(monkeypatch: pytest.MonkeyPatch) -> None:
    primary = _source_metadata(MANGA_PRIMARY_MID, count=3, start=375)
    supplement = _source_metadata(MANGA_SUPPLEMENT_MID, count=5, start=375, date=1800000000)

    async def fake_fetch(source: MangaSource, cookie: str = "") -> dict[str, dict[str, Any]]:
        return primary if source.mid == MANGA_PRIMARY_MID else supplement

    monkeypatch.setattr(manga_module, "fetch_source_metadata", fake_fetch)

    merged = asyncio.run(manga_module.fetch_manga_metadata())

    assert set(merged) == {"375", "376", "377", "378", "379"}
    assert merged["375"]["sourceuid"] == MANGA_PRIMARY_MID
    assert merged["375"]["url"] == primary["375"]["url"]
    assert merged["378"]["sourceuid"] == MANGA_SUPPLEMENT_MID


def test_fetch_manga_metadata_uses_supplement_for_old_episodes(monkeypatch: pytest.MonkeyPatch) -> None:
    primary = _source_metadata(MANGA_PRIMARY_MID, count=2, start=388)
    supplement = _source_metadata(MANGA_SUPPLEMENT_MID, count=4, start=1)

    async def fake_fetch(source: MangaSource, cookie: str = "") -> dict[str, dict[str, Any]]:
        return primary if source.mid == MANGA_PRIMARY_MID else supplement

    monkeypatch.setattr(manga_module, "fetch_source_metadata", fake_fetch)

    merged = asyncio.run(manga_module.fetch_manga_metadata())

    assert set(merged) == {"1", "2", "3", "4", "388", "389"}
    assert merged["1"]["sourceuid"] == MANGA_SUPPLEMENT_MID


def test_fetch_manga_metadata_skips_supplement_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    primary = _source_metadata(MANGA_PRIMARY_MID, count=2, start=388)
    calls: list[int] = []

    async def fake_fetch(source: MangaSource, cookie: str = "") -> dict[str, dict[str, Any]]:
        calls.append(source.mid)
        return primary

    monkeypatch.setattr(manga_module, "fetch_source_metadata", fake_fetch)
    monkeypatch.setattr(manga_module, "MANGA_ENABLE_SUPPLEMENT", False)

    merged = asyncio.run(manga_module.fetch_manga_metadata())

    assert calls == [MANGA_PRIMARY_MID]
    assert set(merged) == {"388", "389"}


def test_fetch_manga_metadata_raises_when_no_source_returns_data(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetch(source: MangaSource, cookie: str = "") -> dict[str, dict[str, Any]]:
        return {}

    monkeypatch.setattr(manga_module, "fetch_source_metadata", fake_fetch)

    with pytest.raises(RuntimeError):
        asyncio.run(manga_module.fetch_manga_metadata())


def test_fetch_source_metadata_raises_on_repeated_page(monkeypatch: pytest.MonkeyPatch) -> None:
    # 限流时接口会重复吐同一页，静默截断会写出残缺数据，必须直接报错。
    payload = {
        "code": 0,
        "data": {
            "items": [{"id": "dyn-1", **_make_item(text=MAIN_SERIES_TEXT)}],
            "has_more": True,
        },
    }

    async def fake_get_json(client: Any, url: str, **kwargs: Any) -> dict[str, Any]:
        return payload

    class _DummyClient:
        async def __aenter__(self) -> _DummyClient:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr(manga_module, "get_json", fake_get_json)
    monkeypatch.setattr(manga_module, "create_async_client", lambda **_kwargs: _DummyClient())

    with pytest.raises(RuntimeError, match="repeated page"):
        asyncio.run(manga_module.fetch_source_metadata(PRIMARY_SOURCE))


# ---------------------------------------------------------------- 与历史数据合并


def test_merge_with_previous_metadata_keeps_history_entries() -> None:
    previous = {
        "1": {"id": 1, "title": "旧标题", "manga": "https://example.com/1.jpg", "sourceuid": MANGA_PRIMARY_MID},
    }
    latest = {"389": _record(389, MANGA_PRIMARY_MID)}

    merged = merge_with_previous_metadata(latest, previous)

    assert set(merged) == {"1", "389"}
    assert merged["1"]["manga"] == "https://example.com/1.jpg"


def test_merge_with_previous_metadata_lets_primary_source_refresh_entry() -> None:
    previous = {
        "389": {
            "id": 389,
            "title": "旧标题",
            "manga": "https://example.com/old.jpg",
            "url": "https://www.bilibili.com/opus/old",
            "sourceuid": MANGA_SUPPLEMENT_MID,
        },
    }
    latest = {"389": _record(389, MANGA_PRIMARY_MID, image="https://example.com/new.jpg")}

    merged = merge_with_previous_metadata(latest, previous)

    assert merged["389"]["manga"] == "https://example.com/new.jpg"
    assert merged["389"]["sourceuid"] == MANGA_PRIMARY_MID


def test_merge_with_previous_metadata_blocks_supplement_from_overwriting_urls() -> None:
    previous = {
        "389": {
            "id": 389,
            "title": "主源标题",
            "manga": "https://example.com/primary.jpg",
            "url": "https://www.bilibili.com/opus/primary",
            "sourceuid": MANGA_PRIMARY_MID,
        },
    }
    latest = {"389": _record(389, MANGA_SUPPLEMENT_MID, image="https://example.com/supplement.jpg")}

    merged = merge_with_previous_metadata(latest, previous)

    assert merged["389"]["url"] == "https://www.bilibili.com/opus/primary"
    assert merged["389"]["manga"] == "https://example.com/primary.jpg"
    assert merged["389"]["sourceuid"] == MANGA_PRIMARY_MID


def test_merge_with_previous_metadata_drops_legacy_entries_without_sourceuid() -> None:
    # 迁移前入库的第 1 话实际是 KEI 一周年条目，占用了主系列话号。
    previous = {
        "1": {"id": 1, "title": "欢迎来到SEKAI！", "manga": "https://example.com/kei/1.jpg", "url": "https://example.com/opus/kei"},
        "389": {"id": 389, "title": "旧标题", "manga": "https://example.com/old.jpg", "sourceuid": MANGA_PRIMARY_MID},
    }
    latest = {"6": _record(6, MANGA_PRIMARY_MID), "389": _record(389, MANGA_PRIMARY_MID)}

    merged = merge_with_previous_metadata(latest, previous)

    assert set(merged) == {"6", "389"}
    assert all("sourceuid" in item for item in merged.values())


def test_merge_with_previous_metadata_lets_supplement_fill_legacy_slot() -> None:
    # 遗留条目不能挡住补充源：旧话 374 原本无 sourceuid，必须由补充源重新填上。
    previous = {"374": {"id": 374, "title": "旧标题", "manga": "https://example.com/legacy/374.jpg"}}
    latest = {"374": _record(374, MANGA_SUPPLEMENT_MID)}

    merged = merge_with_previous_metadata(latest, previous)

    assert merged["374"]["sourceuid"] == MANGA_SUPPLEMENT_MID
    assert merged["374"]["manga"] == latest["374"]["manga"]


# ---------------------------------------------------------------- 持久化


def test_build_metadata_to_persist_keeps_old_url_when_changed_url_download_failed() -> None:
    previous_metadata = {
        "10": {
            "id": 10,
            "title": "旧标题",
            "manga": "https://example.com/old.png",
            "date": 1,
            "url": "https://example.com/opus/10",
            "contributors": {},
            "sourceuid": MANGA_PRIMARY_MID,
        }
    }
    latest_metadata = {
        "10": {
            "id": 10,
            "title": "新标题",
            "manga": "https://example.com/new.png",
            "date": 2,
            "url": "https://example.com/opus/10-new",
            "contributors": {"翻译": "A"},
            "sourceuid": MANGA_SUPPLEMENT_MID,
        }
    }

    persisted = build_metadata_to_persist(latest_metadata, previous_metadata, {10})

    assert persisted["10"]["manga"] == "https://example.com/old.png"
    assert persisted["10"]["title"] == "新标题"
    assert persisted["10"]["date"] == 2


def test_build_metadata_to_persist_uses_latest_when_not_failed() -> None:
    previous_metadata = {
        "10": {"id": 10, "manga": "https://example.com/old.png"},
    }
    latest_metadata = {
        "10": {"id": 10, "manga": "https://example.com/new.png"},
    }

    persisted = build_metadata_to_persist(latest_metadata, previous_metadata, set())
    assert persisted["10"]["manga"] == "https://example.com/new.png"
