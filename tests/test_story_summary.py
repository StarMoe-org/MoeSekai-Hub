import asyncio
import json
from pathlib import Path

import pytest

from src.tasks import story_summary as module
from src.tasks.story_summary import ChapterContent, EpisodeMeta, EventMeta, LLMConfig


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


def test_load_story_txt_and_count_dialogue_lines(tmp_path) -> None:
    event_dir = tmp_path / "Moe-story" / "story" / "event" / "2"
    event_dir.mkdir(parents=True)
    (event_dir / "1.txt").write_text(
        "仲间们为了准备演出而努力。\n"
        "\n"
        "1-1 开始\n"
        "\n"
        "（登场角色：星乃一歌、天马咲希、初音未来）\n"
        "\n"
        "（黑屏转场）\n"
        "一歌：走吧，大家。\n"
        "咲希：嗯，开心点！\n",
        encoding="utf-8",
    )
    (event_dir / "2.txt").write_text(
        "2-1 结束\n"
        "\n"
        "(Character: 宵崎奏, 朝比奈真冬)\n"
        "\n"
        "奏: 新曲、どうしよう。\n",
        encoding="utf-8",
    )
    (event_dir / "3.txt").write_text(
        "日文简介。\n"
        "\n"
        "3-1 始まりの時\n"
        "\n"
        "(Character: 宵崎奏)\n"
        "\n"
        "奏: 新曲、どうしよう。\n",
        encoding="utf-8",
    )

    story_dir = event_dir.parents[2]
    # 第1话：正文从登场角色行之后开始，简介与章节标题取自 txt 原文
    story_1 = module._load_story_txt(story_dir, 2, 1)
    assert story_1.body == "（黑屏转场）\n一歌：走吧，大家。\n咲希：嗯，开心点！"
    assert story_1.outline == "仲间们为了准备演出而努力。"
    assert story_1.chapter_title == "开始"
    assert module._count_dialogue_lines(story_1.body) == 2

    # 第2话起：没有活动简介，仅章节标题
    story_2 = module._load_story_txt(story_dir, 2, 2)
    assert story_2.body == "奏: 新曲、どうしよう。"
    assert story_2.outline is None
    assert story_2.chapter_title == "结束"

    # 日文 txt：同样提取（得到的是日文原文，与 master 一致，由 LLM 翻译）
    story_3 = module._load_story_txt(story_dir, 2, 3)
    assert story_3.body.startswith("奏:")
    assert story_3.outline == "日文简介。"
    assert story_3.chapter_title == "始まりの時"


def test_parse_story_text_falls_back_to_full_text_when_no_marker() -> None:
    text = "活动的剧情简介。\n\n1-1 开始\n\n（没有角色标记行）\n一歌：走吧。\n"
    story = module._parse_story_text(text)
    assert story.body == text.strip()
    assert story.outline is None
    assert story.chapter_title is None


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
    output_dir = tmp_path / "story" / "detail"
    story_dir = tmp_path / "Moe-story"
    event_dir = story_dir / "story" / "event" / "2"
    event_dir.mkdir(parents=True)
    (event_dir / "1.txt").write_text(
        "仲间们为了准备演出而努力。\n"
        "\n"
        "1-1 はじまり\n"
        "\n"
        "（登场角色：星乃一歌、天马咲希）\n"
        "\n"
        "Live House\n"
        "一歌：行こう、みんな。\n"
        "咲希：うん、楽しもう！\n",
        encoding="utf-8",
    )
    (event_dir / "2.txt").write_text(
        "仲间们为了准备演出而努力。\n"
        "\n"
        "2-1 おわり\n"
        "\n"
        "（登场角色：天马咲希）\n"
        "\n"
        "咲希：また次も頑張ろうね。\n",
        encoding="utf-8",
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
        raise AssertionError(file_name)

    responses = iter(
        [
            {
                "title": "测试活动",
                "outline": "伙伴们为了演出而齐心协力。",
                "ep_1_title": "开始",
                "ep_1_summary": "大家为了演出开始行动,并在对话中确认了彼此的心意。",
            },
            {
                "ep_2_title": "结束",
                "ep_2_summary": "演出准备告一段落,成员们在收尾时约定今后也要继续努力。",
            },
            {
                "summary": (
                    "为了迎接演出,伙伴们在准备过程中互相鼓励,逐步确认了共同前进的决心。"
                    "随着最后的收尾完成,众人也约定今后继续并肩努力,让这次经历成为迈向下一步的起点。"
                )
            },
        ]
    )

    async def fake_chat_completion_json(llm_config, *, system_prompt: str, user_prompt: str, max_response_length: int = 1000):  # noqa: ANN001
        return next(responses)

    monkeypatch.setattr(module, "_fetch_master_json", fake_fetch_master_json)
    monkeypatch.setattr(module, "_chat_completion_json", fake_chat_completion_json)

    stats = asyncio.run(
        module.update_story_summary(
            event_id=2,
            output_dir=output_dir,
            story_dir=story_dir,
            llm_config=LLMConfig(api_key="test-key"),
        )
    )

    output_path = output_dir / "event_002.json"
    payload = json.loads(output_path.read_text(encoding="utf-8"))

    assert stats["event_id"] == 2
    assert stats["generated_files"] == 1
    assert stats["chapters_total"] == 2
    assert stats["dialogue_lines_total"] == 3
    assert payload["title_jp"] == "Test Event"
    assert payload["title_cn"] == "测试活动"
    assert payload["outline_cn"] == "伙伴们为了演出而齐心协力。"
    assert payload["summary_cn"].startswith("为了迎接演出")
    assert "cover_image_url" not in payload
    assert len(payload["chapters"]) == 2
    assert payload["chapters"][0]["character_ids"] == []
    assert payload["chapters"][1]["character_ids"] == []
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

    def fake_build_chapter_contents(story_dir, event_meta):  # noqa: ANN001
        return (
            ChapterContent(
                meta=EpisodeMeta(1, "はじまり", "event_002_01", "https://example.com/1.webp"),
                prompt_text="---\n奏:\n开始吧\n",
                character_ids=(1,),
                dialogue_line_count=1,
                implemented=True,
            ),
            ChapterContent(
                meta=EpisodeMeta(2, "おわり", "event_002_02", "https://example.com/2.webp"),
                prompt_text="---\n瑞希:\n继续努力\n",
                character_ids=(2,),
                dialogue_line_count=1,
                implemented=True,
            ),
        )

    monkeypatch.setattr(module, "_fetch_event_metas", lambda event_id=None: asyncio.sleep(0, result=(event_meta,)))
    monkeypatch.setattr(module, "_resolve_llm_config", lambda llm_config: LLMConfig(api_key="test-key"))
    monkeypatch.setattr(module, "_build_chapter_contents", fake_build_chapter_contents)
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

    async def fake_generate_summary_rows(llm_config, event_meta, chapter_contents):  # noqa: ANN001
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

    def fake_build_chapter_contents(story_dir, event_meta):  # noqa: ANN001
        return (
            ChapterContent(
                meta=event_meta.episodes[0],
                prompt_text="---\n奏:\n开始吧\n",
                character_ids=(event_meta.event_id,),
                dialogue_line_count=1,
                implemented=True,
            ),
        )

    monkeypatch.setattr(module, "_fetch_event_metas", lambda event_id=None: asyncio.sleep(0, result=event_metas))
    monkeypatch.setattr(module, "_resolve_llm_config", lambda llm_config: LLMConfig(api_key="test-key"))
    monkeypatch.setattr(module, "_build_chapter_contents", fake_build_chapter_contents)
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
        "skipped_missing": 0,
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
        "skipped_missing": 0,
    }
    assert attempts == {1: 1, 2: 1}


def test_update_story_summary_skips_when_story_txt_missing(tmp_path, monkeypatch, capsys) -> None:
    output_dir = tmp_path / "story" / "detail"
    story_dir = tmp_path / "empty-story"
    story_dir.mkdir()

    event_meta = EventMeta(
        event_id=2,
        title_jp="Test Event",
        outline_jp="outline",
        assetbundle_name="event_test_2026",
        episodes=(EpisodeMeta(1, "はじまり", "event_002_01", "https://example.com/1.webp"),),
    )

    monkeypatch.setattr(module, "_fetch_event_metas", lambda event_id=None: asyncio.sleep(0, result=(event_meta,)))
    monkeypatch.setattr(module, "_resolve_llm_config", lambda llm_config: LLMConfig(api_key="test-key"))

    stats = asyncio.run(
        module.update_story_summary(
            event_id=2,
            output_dir=output_dir,
            story_dir=story_dir,
            llm_config=LLMConfig(api_key="test-key"),
        )
    )

    assert stats == {
        "event_id": 2,
        "chapters_total": 0,
        "dialogue_lines_total": 0,
        "generated_files": 0,
        "skipped_existing": 0,
    }
    output = capsys.readouterr().out
    assert "Missing story txt" in output
    assert not (output_dir / "event_002.json").exists()


def test_update_story_summary_specific_event_ids(tmp_path, monkeypatch) -> None:
    output_dir = tmp_path / "story" / "detail"

    event_meta_2 = EventMeta(
        event_id=2,
        title_jp="Test Event",
        outline_jp="outline",
        assetbundle_name="event_test_2026",
        episodes=(EpisodeMeta(1, "ep1", "event_002_01", "https://example.com/2.webp"),),
    )

    async def fake_fetch_event_metas(event_id=None):  # noqa: ANN001
        assert event_id is None  # 多 id 分支只拉一次全量 master
        return (event_meta_2,)

    attempts: list[int] = []

    async def fake_generate_event_summary_file(event_meta, **kwargs):  # noqa: ANN001
        attempts.append(event_meta.event_id)
        return (1, 2)

    monkeypatch.setattr(module, "_fetch_event_metas", fake_fetch_event_metas)
    monkeypatch.setattr(module, "_resolve_llm_config", lambda llm_config: LLMConfig(api_key="test-key"))
    monkeypatch.setattr(module, "_generate_event_summary_file", fake_generate_event_summary_file)

    stats = asyncio.run(
        module.update_story_summary(
            event_ids=(1, 2),
            output_dir=output_dir,
            llm_config=LLMConfig(api_key="test-key"),
        )
    )

    assert stats == {
        "events_total": 2,
        "generated_events": 1,
        "chapters_total": 1,
        "dialogue_lines_total": 2,
        "generated_files": 1,
        "failed_events": 1,
        "skipped_existing": 0,
        "skipped_missing": 0,
    }
    assert attempts == [2]


def test_update_story_summary_counts_missing_story_txts_separately(tmp_path, monkeypatch) -> None:
    output_dir = tmp_path / "story" / "detail"

    event_meta_2 = EventMeta(
        event_id=2,
        title_jp="Test Event",
        outline_jp="outline",
        assetbundle_name="event_test_2026",
        episodes=(EpisodeMeta(1, "ep1", "event_002_01", "https://example.com/2.webp"),),
    )
    event_meta_3 = EventMeta(
        event_id=3,
        title_jp="Test Event 3",
        outline_jp="outline 3",
        assetbundle_name="event_test_2026",
        episodes=(EpisodeMeta(1, "ep1", "event_003_01", "https://example.com/3.webp"),),
    )

    async def fake_fetch_event_metas(event_id=None):  # noqa: ANN001
        return (event_meta_2, event_meta_3)

    async def fake_generate_event_summary_file(event_meta, **kwargs):  # noqa: ANN001
        raise module.StoryTextNotFoundError("Missing story txt (event not crawled in Moe-story repo yet)")

    monkeypatch.setattr(module, "_fetch_event_metas", fake_fetch_event_metas)
    monkeypatch.setattr(module, "_resolve_llm_config", lambda llm_config: LLMConfig(api_key="test-key"))
    monkeypatch.setattr(module, "_generate_event_summary_file", fake_generate_event_summary_file)

    stats = asyncio.run(
        module.update_story_summary(
            event_ids=(2, 3),
            output_dir=output_dir,
            llm_config=LLMConfig(api_key="test-key"),
        )
    )

    assert stats == {
        "events_total": 2,
        "generated_events": 0,
        "chapters_total": 0,
        "dialogue_lines_total": 0,
        "generated_files": 0,
        "failed_events": 0,
        "skipped_existing": 0,
        "skipped_missing": 2,
    }
    assert not (output_dir / "event_002.json").exists()


def test_run_event_batch_runs_concurrently(monkeypatch) -> None:
    """验证多个事件并发执行（峰值并发数 > 1）。"""
    peak_concurrency = 0
    current_concurrency = 0

    async def fake_generate_event_summary_file(event_meta, **kwargs):  # noqa: ANN001
        nonlocal peak_concurrency, current_concurrency
        current_concurrency += 1
        peak_concurrency = max(peak_concurrency, current_concurrency)
        await asyncio.sleep(0.05)
        current_concurrency -= 1
        return (1, 1)

    monkeypatch.setattr(module, "_generate_event_summary_file", fake_generate_event_summary_file)

    metas = [
        EventMeta(
            event_id=event_id,
            title_jp=f"E{event_id}",
            outline_jp="outline",
            assetbundle_name="event_test",
            episodes=(EpisodeMeta(1, "ep1", "ev_001", "https://example.com/x.webp"),),
        )
        for event_id in range(1, 7)
    ]

    stats = asyncio.run(
        module._run_event_batch(
            metas,
            output_dir=Path("not-used"),
            story_dir=Path("not-used"),
            llm_config=LLMConfig(api_key="test-key"),
            force=True,
        )
    )

    assert peak_concurrency > 1
    assert stats == (6, 6, 6, 0, 0, 0)


def test_check_llm_available(monkeypatch) -> None:
    async def fake_request(client, method, url, **kwargs):  # noqa: ANN001
        class FakeResponse:
            def json(self):  # noqa: ANN201
                return {"choices": [{"message": {"content": "pong"}}]}

        return FakeResponse()

    monkeypatch.setattr(module, "request_with_retry", fake_request)
    message = asyncio.run(module.check_llm_available(LLMConfig(api_key="test-key")))
    assert message.startswith("OK")


def test_check_llm_available_fails_on_timeout(monkeypatch) -> None:
    async def fake_request(client, method, url, **kwargs):  # noqa: ANN001
        raise TimeoutError("read timeout")

    monkeypatch.setattr(module, "request_with_retry", fake_request)
    with pytest.raises(module.StorySummaryError, match="LLM unavailable"):
        asyncio.run(module.check_llm_available(LLMConfig(api_key="test-key")))
