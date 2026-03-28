import asyncio
import json
from pathlib import Path

import brotli

from src.tasks import story_summary as module
from src.tasks.story_summary import ChapterContent, EpisodeMeta, EventMeta, LLMConfig, StorySnippet


def _write_story_asset(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    path.write_bytes(brotli.compress(content, quality=11))


async def _fake_character2d_map() -> dict[int, int]:
    return {}


async def _fake_generate_summary_rows(*args, **kwargs):  # noqa: ANN002, ANN003
    return (
        "测试活动",
        "伙伴们为了演出而齐心协力。",
        "为了迎接演出，伙伴们在准备过程中互相鼓励，最终确认了今后也要并肩前行。",
        [
            {
                "chapter_no": 1,
                "title_jp": "はじまり",
                "title_cn": "开始",
                "summary_cn": "大家为了演出开始行动。",
                "character_ids": [1],
                "image_url": "https://example.com/1.webp",
            },
            {
                "chapter_no": 2,
                "title_jp": "おわり",
                "title_cn": "结束",
                "summary_cn": "大家约定今后也要继续努力。",
                "character_ids": [2],
                "image_url": "https://example.com/2.webp",
            },
        ],
    )


def test_extract_story_snippets_and_character_ids() -> None:
    payload = {
        "Snippets": [
            {"Index": 0, "Action": 1, "ReferenceIndex": 0},
            {"Index": 1, "Action": 6, "ReferenceIndex": 0},
            {"Index": 2, "Action": 6, "ReferenceIndex": 1},
        ],
        "SpecialEffectData": [{"EffectType": 8, "StringVal": "屋顶", "StringValSub": "", "Duration": 0.0, "IntVal": 0}],
        "TalkData": [
            {
                "WindowDisplayName": "奏",
                "Body": " ……新曲、どうしよう。 ",
                "TalkCharacters": [{"Character2dId": 294}],
            },
            {
                "WindowDisplayName": "瑞希・真冬",
                "Body": "一緒に考えようよ。",
                "TalkCharacters": [{"Character2dId": 297}],
            },
        ],
        "AppearCharacters": [{"Character2dId": 297}, {"Character2dId": 294}, {"Character2dId": 297}],
    }

    snippets = module._extract_story_snippets(payload)
    assert snippets == (
        StorySnippet(names=None, text="屋顶"),
        StorySnippet(names=("奏",), text="……新曲、どうしよう。"),
        StorySnippet(names=("瑞希", "真冬"), text="一緒に考えようよ。"),
    )
    assert module._build_prompt_story_text(1, "はじまり", snippets) == (
        "【EP1: はじまり】\n"
        "---\n(屋顶)\n"
        "---\n奏:\n……新曲、どうしよう。\n"
        "---\n瑞希 & 真冬:\n一緒に考えようよ。\n"
    )

    character_ids = module._extract_character_ids(payload, {294: 17, 297: 20})
    assert character_ids == (20, 17)


def test_fetch_event_meta_prefers_latest_event_story(monkeypatch) -> None:
    async def fake_fetch_master_json(file_name: str, *, lang: str = "jp", srcs=None):  # noqa: ANN001
        if file_name == "events":
            return [
                {"id": 199, "name": "Amid the Wavering Light"},
                {"id": 200, "name": "Future Event"},
            ]
        if file_name == "eventStories":
            return [
                {
                    "eventId": 199,
                    "outline": "outline jp",
                    "assetbundleName": "event_wavering_2026",
                    "eventStoryEpisodes": [
                        {"episodeNo": 1, "title": "chapter 1", "scenarioId": "event_199_01"},
                    ],
                }
            ]
        raise AssertionError(file_name)

    monkeypatch.setattr(module, "_fetch_master_json", fake_fetch_master_json)

    event_meta = asyncio.run(module._fetch_event_meta())

    assert event_meta.event_id == 199
    assert event_meta.title_jp == "Amid the Wavering Light"
    assert event_meta.assetbundle_name == "event_wavering_2026"
    assert len(event_meta.episodes) == 1
    assert event_meta.episodes[0].image_url.endswith("/event_wavering_2026/event_wavering_2026_01.webp")


def test_update_story_summary_writes_expected_schema(tmp_path, monkeypatch) -> None:
    asset_dir = tmp_path / "story_assets"
    output_dir = tmp_path / "story" / "detail"

    _write_story_asset(
        asset_dir / "pjsk-jp-assets" / "event_story" / "event_test_2026" / "scenario" / "event_002_01.asset.br",
        {
            "Snippets": [
                {"Index": 0, "Action": 1, "ReferenceIndex": 0},
                {"Index": 1, "Action": 6, "ReferenceIndex": 0},
                {"Index": 2, "Action": 6, "ReferenceIndex": 1},
            ],
            "SpecialEffectData": [{"EffectType": 8, "StringVal": "Live House", "StringValSub": "", "Duration": 0.0, "IntVal": 0}],
            "TalkData": [
                {
                    "WindowDisplayName": "一歌",
                    "Body": "行こう、みんな。",
                    "TalkCharacters": [{"Character2dId": 101}],
                },
                {
                    "WindowDisplayName": "咲希",
                    "Body": "うん、楽しもう！",
                    "TalkCharacters": [{"Character2dId": 102}],
                },
            ],
            "AppearCharacters": [{"Character2dId": 101}, {"Character2dId": 102}],
        },
    )
    _write_story_asset(
        asset_dir / "pjsk-jp-assets" / "event_story" / "event_test_2026" / "scenario" / "event_002_02.asset.br",
        {
            "Snippets": [
                {"Index": 0, "Action": 6, "ReferenceIndex": 0},
                {"Index": 1, "Action": 1, "ReferenceIndex": 0},
            ],
            "SpecialEffectData": [{"EffectType": 8, "StringVal": "屋上", "StringValSub": "", "Duration": 0.0, "IntVal": 0}],
            "TalkData": [
                {
                    "WindowDisplayName": "咲希",
                    "Body": "また次も頑張ろうね。",
                    "TalkCharacters": [{"Character2dId": 102}],
                }
            ],
            "AppearCharacters": [{"Character2dId": 102}, {"Character2dId": 101}],
        },
    )

    async def fake_fetch_master_json(file_name: str, *, lang: str = "jp", srcs=None):  # noqa: ANN001
        if file_name == "events":
            return [{"id": 2, "name": "Test Event"}]
        if file_name == "eventStories":
            return [
                {
                    "eventId": 2,
                    "outline": "仲间们为了准备演出而努力。",
                    "assetbundleName": "event_test_2026",
                    "eventStoryEpisodes": [
                        {"episodeNo": 1, "title": "はじまり", "scenarioId": "event_002_01"},
                        {"episodeNo": 2, "title": "おわり", "scenarioId": "event_002_02"},
                    ],
                }
            ]
        if file_name == "character2ds":
            return [
                {"id": 101, "characterId": 1},
                {"id": 102, "characterId": 2},
            ]
        raise AssertionError(file_name)

    responses = iter(
        [
            {
                "title": "测试活动",
                "outline": "伙伴们为了演出而齐心协力。",
                "ep_1_title": "开始",
                "ep_1_summary": "大家为了演出开始行动，并在对话中确认了彼此的心意。",
            },
            {
                "ep_2_title": "结束",
                "ep_2_summary": "演出准备告一段落，成员们在收尾时约定今后也要继续努力。",
            },
            {
                "summary": (
                    "为了迎接演出，伙伴们在准备过程中互相鼓励，逐步确认了共同前进的决心。"
                    "随着最后的收尾完成，众人也约定今后继续并肩努力，让这次经历成为迈向下一步的起点。"
                )
            },
        ]
    )

    async def fake_chat_completion_json(llm_config, *, system_prompt: str, user_prompt: str, max_response_length: int = 1000):  # noqa: ANN001
        return next(responses)

    monkeypatch.setattr(module, "_fetch_master_json", fake_fetch_master_json)
    monkeypatch.setattr(module, "_chat_completion_json", fake_chat_completion_json)
    monkeypatch.setattr(module, "_load_translation_name_map", lambda: {})

    stats = asyncio.run(
        module.update_story_summary(
            event_id=2,
            asset_dir=asset_dir,
            output_dir=output_dir,
            llm_config=LLMConfig(api_key="test-key"),
        )
    )

    output_path = output_dir / "event_002.json"
    payload = json.loads(output_path.read_text(encoding="utf-8"))

    assert stats["event_id"] == 2
    assert stats["generated_files"] == 1
    assert stats["chapters_total"] == 2
    assert payload["title_jp"] == "Test Event"
    assert payload["title_cn"] == "测试活动"
    assert payload["outline_cn"] == "伙伴们为了演出而齐心协力。"
    assert payload["summary_cn"].startswith("为了迎接演出")
    assert "cover_image_url" not in payload
    assert len(payload["chapters"]) == 2
    assert payload["chapters"][0]["character_ids"] == [1, 2]
    assert payload["chapters"][1]["character_ids"] == [2, 1]
    assert payload["chapters"][0]["image_url"].endswith("/event_test_2026/event_test_2026_01.webp")
    assert payload["chapters"][1]["image_url"].endswith("/event_test_2026/event_test_2026_02.webp")


def test_update_story_summary_skips_existing_output_when_chapter_count_matches(tmp_path, monkeypatch) -> None:
    output_dir = tmp_path / "story" / "detail"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "event_002.json").write_text(
        json.dumps({"chapters": [{"chapter_no": 1}, {"chapter_no": 2}]}, ensure_ascii=False),
        encoding="utf-8",
    )

    event_meta = EventMeta(
        event_id=2,
        title_jp="Test Event",
        outline_jp="outline",
        assetbundle_name="event_test_2026",
        episodes=(
            EpisodeMeta(1, "はじまり", "event_002_01", "https://example.com/1.webp"),
            EpisodeMeta(2, "おわり", "event_002_02", "https://example.com/2.webp"),
        ),
    )

    def fail_resolve_llm_config(llm_config):  # noqa: ANN001
        raise AssertionError("should not resolve llm config when output already exists and chapters match")

    monkeypatch.setattr(module, "_fetch_event_metas", lambda event_id=None: asyncio.sleep(0, result=(event_meta,)))
    monkeypatch.setattr(module, "_resolve_llm_config", fail_resolve_llm_config)

    stats = asyncio.run(module.update_story_summary(event_id=2, output_dir=output_dir))

    assert stats == {
        "event_id": 2,
        "chapters_total": 2,
        "dialogue_lines_total": 0,
        "generated_files": 0,
        "skipped_existing": 1,
    }


def test_update_story_summary_regenerates_when_existing_output_is_outdated(tmp_path, monkeypatch) -> None:
    output_dir = tmp_path / "story" / "detail"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "event_002.json").write_text(
        json.dumps({"chapters": [{"chapter_no": 1}]}, ensure_ascii=False),
        encoding="utf-8",
    )

    event_meta = EventMeta(
        event_id=2,
        title_jp="Test Event",
        outline_jp="outline",
        assetbundle_name="event_test_2026",
        episodes=(
            EpisodeMeta(1, "はじまり", "event_002_01", "https://example.com/1.webp"),
            EpisodeMeta(2, "おわり", "event_002_02", "https://example.com/2.webp"),
        ),
    )

    monkeypatch.setattr(module, "_fetch_event_metas", lambda event_id=None: asyncio.sleep(0, result=(event_meta,)))
    monkeypatch.setattr(module, "_resolve_llm_config", lambda llm_config: LLMConfig(api_key="test-key"))
    monkeypatch.setattr(module, "_fetch_character2d_map", _fake_character2d_map)
    monkeypatch.setattr(
        module,
        "_build_chapter_contents",
        lambda *args, **kwargs: (
            ChapterContent(
                meta=EpisodeMeta(1, "はじまり", "event_002_01", "https://example.com/1.webp"),
                prompt_text="---\n奏:\n开始吧\n",
                character_ids=(1,),
                snippet_count=1,
                implemented=True,
            ),
            ChapterContent(
                meta=EpisodeMeta(2, "おわり", "event_002_02", "https://example.com/2.webp"),
                prompt_text="---\n瑞希:\n继续努力\n",
                character_ids=(2,),
                snippet_count=1,
                implemented=True,
            ),
        ),
    )
    monkeypatch.setattr(module, "_load_translation_name_map", lambda: {})
    monkeypatch.setattr(module, "_generate_summary_rows", _fake_generate_summary_rows)

    stats = asyncio.run(module.update_story_summary(event_id=2, output_dir=output_dir))
    payload = json.loads((output_dir / "event_002.json").read_text(encoding="utf-8"))

    assert stats["generated_files"] == 1
    assert stats["skipped_existing"] == 0
    assert len(payload["chapters"]) == 2
    assert payload["chapters"][1]["title_cn"] == "结束"


def test_update_story_summary_scans_all_history_and_fills_missing(tmp_path, monkeypatch) -> None:
    output_dir = tmp_path / "story" / "detail"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "event_001.json").write_text(
        json.dumps({"chapters": [{"chapter_no": 1}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    (output_dir / "event_003.json").write_text(
        json.dumps({"chapters": [{"chapter_no": 1}]}, ensure_ascii=False),
        encoding="utf-8",
    )

    event_metas = (
        EventMeta(
            event_id=1,
            title_jp="Event 1",
            outline_jp="outline 1",
            assetbundle_name="event_1",
            episodes=(EpisodeMeta(1, "ep1", "event_001_01", "https://example.com/1.webp"),),
        ),
        EventMeta(
            event_id=2,
            title_jp="Event 2",
            outline_jp="outline 2",
            assetbundle_name="event_2",
            episodes=(EpisodeMeta(1, "ep1", "event_002_01", "https://example.com/2.webp"),),
        ),
        EventMeta(
            event_id=3,
            title_jp="Event 3",
            outline_jp="outline 3",
            assetbundle_name="event_3",
            episodes=(EpisodeMeta(1, "ep1", "event_003_01", "https://example.com/3.webp"),),
        ),
    )

    async def fake_generate_summary_rows(llm_config, event_meta, chapter_contents, translation_name_map):  # noqa: ANN001
        return (
            f"活动{event_meta.event_id}",
            f"概要{event_meta.event_id}",
            f"总结{event_meta.event_id}",
            [
                {
                    "chapter_no": 1,
                    "title_jp": "ep1",
                    "title_cn": f"章节{event_meta.event_id}",
                    "summary_cn": f"剧情{event_meta.event_id}",
                    "character_ids": [event_meta.event_id],
                    "image_url": f"https://example.com/{event_meta.event_id}.webp",
                }
            ],
        )

    monkeypatch.setattr(module, "_fetch_event_metas", lambda event_id=None: asyncio.sleep(0, result=event_metas))
    monkeypatch.setattr(module, "_resolve_llm_config", lambda llm_config: LLMConfig(api_key="test-key"))
    monkeypatch.setattr(module, "_fetch_character2d_map", _fake_character2d_map)
    monkeypatch.setattr(
        module,
        "_build_chapter_contents",
        lambda event_meta, *args, **kwargs: (
            ChapterContent(
                meta=event_meta.episodes[0],
                prompt_text="---\n奏:\n开始吧\n",
                character_ids=(event_meta.event_id,),
                snippet_count=1,
                implemented=True,
            ),
        ),
    )
    monkeypatch.setattr(module, "_load_translation_name_map", lambda: {})
    monkeypatch.setattr(module, "_generate_summary_rows", fake_generate_summary_rows)

    stats = asyncio.run(module.update_story_summary(output_dir=output_dir))
    payload = json.loads((output_dir / "event_002.json").read_text(encoding="utf-8"))

    assert stats == {
        "events_total": 3,
        "generated_events": 1,
        "chapters_total": 1,
        "dialogue_lines_total": 1,
        "generated_files": 1,
        "failed_events": 0,
        "skipped_existing": 2,
    }
    assert payload["title_cn"] == "活动2"
    assert payload["chapters"][0]["title_cn"] == "章节2"


def test_update_story_summary_skips_failed_events_and_continues_scan(tmp_path, monkeypatch) -> None:
    output_dir = tmp_path / "story" / "detail"
    output_dir.mkdir(parents=True, exist_ok=True)

    event_metas = (
        EventMeta(
            event_id=1,
            title_jp="Event 1",
            outline_jp="outline 1",
            assetbundle_name="event_1",
            episodes=(EpisodeMeta(1, "ep1", "event_001_01", "https://example.com/1.webp"),),
        ),
        EventMeta(
            event_id=2,
            title_jp="Event 2",
            outline_jp="outline 2",
            assetbundle_name="event_2",
            episodes=(EpisodeMeta(1, "ep1", "event_002_01", "https://example.com/2.webp"),),
        ),
    )

    attempts: dict[int, int] = {}

    async def fake_generate_event_summary_file(event_meta, **kwargs):  # noqa: ANN001
        attempts[event_meta.event_id] = attempts.get(event_meta.event_id, 0) + 1
        if event_meta.event_id == 1:
            raise module.StorySummaryError("permanent failure")
        return (1, 2)

    monkeypatch.setattr(module, "_fetch_event_metas", lambda event_id=None: asyncio.sleep(0, result=event_metas))
    monkeypatch.setattr(module, "_resolve_llm_config", lambda llm_config: LLMConfig(api_key="test-key"))
    monkeypatch.setattr(module, "_fetch_character2d_map", _fake_character2d_map)
    monkeypatch.setattr(module, "_load_translation_name_map", lambda: {})
    monkeypatch.setattr(module, "_generate_event_summary_file", fake_generate_event_summary_file)

    stats = asyncio.run(module.update_story_summary(output_dir=output_dir))

    assert stats == {
        "events_total": 2,
        "generated_events": 1,
        "chapters_total": 1,
        "dialogue_lines_total": 2,
        "generated_files": 1,
        "failed_events": 1,
        "skipped_existing": 0,
    }
    assert attempts == {1: 1, 2: 1}
