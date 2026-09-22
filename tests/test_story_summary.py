import asyncio
import json
from pathlib import Path

import pytest

from src.tasks import story_summary as module
from src.tasks.story_summary import ChapterContent, EpisodeMeta, EventMeta, LLMConfig, StorySource


def _write_upstream_txt(
    story_root: Path,
    lang: str,
    event_id: int,
    chapter_no: int,
    text: str,
    *,
    event_name: str = "Test Event",
    chapter_title: str = "chapter",
) -> Path:
    """按上游布局写入单话 txt。

    story_{lang}/event/{id:03d} {event_name}/{id:03d}-{ep:02d} {chapter_title}.txt
    """
    event_dir = story_root / f"story_{lang}" / "event" / f"{event_id:03d} {event_name}"
    event_dir.mkdir(parents=True, exist_ok=True)
    path = event_dir / f"{event_id:03d}-{chapter_no:02d} {chapter_title}.txt"
    path.write_text(text, encoding="utf-8")
    return path


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
    story_root = tmp_path / "ProjectSekai-story"
    _write_upstream_txt(
        story_root,
        "jp",
        2,
        1,
        "仲间们为了准备演出而努力。\n"
        "\n"
        "1-1 开始\n"
        "\n"
        "（登场角色：星乃一歌、天马咲希、初音未来）\n"
        "\n"
        "（黑屏转场）\n"
        "一歌：走吧，大家。\n"
        "咲希：嗯，开心点！\n",
    )
    _write_upstream_txt(
        story_root,
        "jp",
        2,
        2,
        "2-1 结束\n"
        "\n"
        "(Character: 宵崎奏, 朝比奈真冬)\n"
        "\n"
        "奏: 新曲、どうしよう。\n",
    )
    _write_upstream_txt(
        story_root,
        "jp",
        2,
        3,
        "日文简介。\n"
        "\n"
        "3-1 始まりの時\n"
        "\n"
        "(Character: 宵崎奏)\n"
        "\n"
        "奏: 新曲、どうしよう。\n",
    )

    source = StorySource(root=story_root)
    # 第1话：正文从登场角色行之后开始，简介与章节标题取自 txt 原文
    story_1 = module._load_story_txt(source, 2, 1)
    assert story_1.body == "（黑屏转场）\n一歌：走吧，大家。\n咲希：嗯，开心点！"
    assert story_1.outline == "仲间们为了准备演出而努力。"
    assert story_1.chapter_title == "开始"
    assert module._count_dialogue_lines(story_1.body) == 2

    # 第2话起：没有活动简介，仅章节标题
    story_2 = module._load_story_txt(source, 2, 2)
    assert story_2.body == "奏: 新曲、どうしよう。"
    assert story_2.outline is None
    assert story_2.chapter_title == "结束"

    # 日文 txt：同样提取（得到的是日文原文，与 master 一致，由 LLM 翻译）
    story_3 = module._load_story_txt(source, 2, 3)
    assert story_3.body.startswith("奏:")
    assert story_3.outline == "日文简介。"
    assert story_3.chapter_title == "始まりの時"


def _minimal_txt(marker_line: str, dialogue: str) -> str:
    return f"简介。\n\n1-1 标题\n\n{marker_line}\n\n{dialogue}\n"


def test_load_story_txt_prefers_requested_lang_then_falls_back_to_jp(tmp_path) -> None:
    story_root = tmp_path / "ProjectSekai-story"
    # 活动 2：cn 与 jp 都有 → cn 优先
    _write_upstream_txt(story_root, "jp", 2, 1, _minimal_txt("(Character: 宵崎奏)", "奏: 日文台词。"))
    _write_upstream_txt(story_root, "cn", 2, 1, _minimal_txt("（登场角色：宵崎奏）", "奏：中文台词。"))
    # 活动 3：只有 jp（模拟 cn 未收录的新活动）
    _write_upstream_txt(story_root, "jp", 3, 1, _minimal_txt("(Character: 東雲彰人)", "彰人: 只有日文。"))

    cn_source = StorySource(root=story_root, lang="cn")
    assert module._load_story_txt(cn_source, 2, 1).body == "奏：中文台词。"
    # cn 缺失 → 非严格模式回退 jp
    assert module._load_story_txt(cn_source, 3, 1).body == "彰人: 只有日文。"

    # 默认 jp 时不会被 cn 影响
    jp_source = StorySource(root=story_root, lang="jp")
    assert module._load_story_txt(jp_source, 2, 1).body == "奏: 日文台词。"


def test_load_story_txt_strict_mode_does_not_fall_back(tmp_path) -> None:
    story_root = tmp_path / "ProjectSekai-story"
    _write_upstream_txt(story_root, "jp", 3, 1, _minimal_txt("(Character: 東雲彰人)", "彰人: 只有日文。"))

    strict_source = StorySource(root=story_root, lang="cn", strict=True)
    with pytest.raises(module.StoryTextNotFoundError) as excinfo:
        module._load_story_txt(strict_source, 3, 1)
    # 报错信息应只提到 cn，不含回退语言
    assert "lang=cn" in str(excinfo.value)

    # 同一份数据在非严格模式下可以回退
    assert module._load_story_txt(StorySource(root=story_root, lang="cn"), 3, 1).body == "彰人: 只有日文。"


def test_resolve_event_txt_matches_id_prefix_exactly(tmp_path) -> None:
    story_root = tmp_path / "ProjectSekai-story"
    _write_upstream_txt(story_root, "jp", 2, 1, "x", event_name="Small Event")
    _write_upstream_txt(story_root, "jp", 20, 1, "y", event_name="Big Event")

    path_2 = module._resolve_event_txt(story_root, "jp", 2, 1)
    path_20 = module._resolve_event_txt(story_root, "jp", 20, 1)
    assert path_2 is not None and path_2.parent.name == "002 Small Event"
    assert path_20 is not None and path_20.parent.name == "020 Big Event"

    # 未收录的活动/话数返回 None
    assert module._resolve_event_txt(story_root, "jp", 2, 9) is None
    assert module._resolve_event_txt(story_root, "jp", 999, 1) is None
    # 语言目录不存在也返回 None，而非抛错
    assert module._resolve_event_txt(story_root, "cn", 2, 1) is None


def test_resolve_event_txt_handles_two_digit_chapters(tmp_path) -> None:
    story_root = tmp_path / "ProjectSekai-story"
    # 上游话数为两位补零，10 话以上活动需正确匹配
    _write_upstream_txt(story_root, "jp", 179, 1, "ep1", chapter_title="first")
    _write_upstream_txt(story_root, "jp", 179, 10, "ep10", chapter_title="tenth")
    _write_upstream_txt(story_root, "jp", 179, 15, "ep15", chapter_title="last")

    source = StorySource(root=story_root)
    assert module._load_story_txt(source, 179, 1).body == "ep1"
    assert module._load_story_txt(source, 179, 10).body == "ep10"
    assert module._load_story_txt(source, 179, 15).body == "ep15"


def test_parse_event_dir_title() -> None:
    # 标准形态：id + 标题 + banner 括号
    assert module._parse_event_dir_title("005 此时此地再次启程！ (MMJ_桃井爱莉)") == "此时此地再次启程！"
    # 标题自身含括号
    assert module._parse_event_dir_title("100 標題(含括號) (Mix_WL)") == "標題(含括號)"
    # 无 banner 括号
    assert module._parse_event_dir_title("101 NoBanner") == "NoBanner"
    # 标题为空或前缀非法
    assert module._parse_event_dir_title("102 ") is None
    assert module._parse_event_dir_title("abc bad prefix (x)") is None


def test_resolve_prompt_title_uses_dir_title_for_non_jp_only(tmp_path) -> None:
    story_root = tmp_path / "ProjectSekai-story"
    _write_upstream_txt(story_root, "jp", 5, 1, "x", event_name="ここからRE：START！ (MMJ_桃井愛莉)")
    _write_upstream_txt(story_root, "cn", 5, 1, "x", event_name="此时此地再次启程！ (MMJ_桃井爱莉)")

    # jp 一律回退 master（目录名经过非法字符替换且不可逆）
    assert module._resolve_prompt_title(StorySource(root=story_root, lang="jp"), 5) is None
    # 非 jp 取所选语言的目录名标题
    assert module._resolve_prompt_title(StorySource(root=story_root, lang="cn"), 5) == "此时此地再次启程！"
    # 所选语言缺该活动目录时不跨语言回退（回退 jp 只会拿到日文，与 master 等价）
    assert module._resolve_prompt_title(StorySource(root=story_root, lang="cn"), 9) is None
    # 语言目录整体不存在也返回 None，而非抛错
    assert module._resolve_prompt_title(StorySource(root=story_root, lang="tw"), 5) is None


def test_build_start_prompt_prefers_event_title_over_master(tmp_path) -> None:
    event_meta = EventMeta(
        event_id=5,
        title_jp="ここからRE:START！",
        outline_jp="日文简介",
        assetbundle_name="event_test",
        episodes=(EpisodeMeta(1, "わたしもアイドルに！", "event_005_01", "https://example.com/1.webp"),),
    )
    chapter = ChapterContent(
        meta=event_meta.episodes[0],
        prompt_text="愛莉：がんばろう。",
        character_ids=(),
        dialogue_line_count=1,
        implemented=True,
        outline="中文简介",
        chapter_title="我也要成为偶像！",
    )

    # 传入上游本地化标题时优先使用
    prompt_cn = module._build_start_prompt(event_meta, chapter, limit=200, event_title="此时此地再次启程！")
    assert "标题: 此时此地再次启程！" in prompt_cn
    assert "ここからRE:START！" not in prompt_cn

    # 未传入（jp 路径）时回退 master 日文标题
    prompt_jp = module._build_start_prompt(event_meta, chapter, limit=200)
    assert "标题: ここからRE:START！" in prompt_jp


def test_generate_event_summary_file_passes_dir_title_for_non_jp(tmp_path, monkeypatch) -> None:
    """非 jp 语言时，上游目录名标题应一路传到 prompt 构造。"""
    story_root = tmp_path / "ProjectSekai-story"
    text = _minimal_txt("（登场角色：桃井爱莉）", "爱莉：加油。")
    _write_upstream_txt(story_root, "cn", 5, 1, text, event_name="此时此地再次启程！ (MMJ_桃井爱莉)")
    _write_upstream_txt(story_root, "jp", 5, 1, text, event_name="ここからRE：START！ (MMJ_桃井愛莉)")

    event_meta = EventMeta(
        event_id=5,
        title_jp="ここからRE:START！",
        outline_jp="日文简介",
        assetbundle_name="event_test",
        episodes=(EpisodeMeta(1, "わたしもアイドルに！", "event_005_01", "https://example.com/1.webp"),),
    )

    seen: list[str | None] = []

    async def fake_generate_summary_rows(llm_config, meta, contents, *, event_title=None):  # noqa: ANN001
        seen.append(event_title)
        return ("标题", "简介", "总结", [])

    monkeypatch.setattr(module, "_generate_summary_rows", fake_generate_summary_rows)

    for lang in ("cn", "jp"):
        asyncio.run(
            module._generate_event_summary_file(
                event_meta,
                output_dir=tmp_path / "out",
                source=StorySource(root=story_root, lang=lang),
                llm_config=LLMConfig(api_key="test-key"),
            )
        )
    assert seen == ["此时此地再次启程！", None]


def test_resolve_story_source_env_and_validation(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("PJSK_STORY_DIR", raising=False)
    monkeypatch.delenv("MOE_STORY_DIR", raising=False)
    monkeypatch.delenv("PJSK_STORY_LANG", raising=False)

    # 默认值
    default_source = module._resolve_story_source(None)
    assert default_source.root == Path("ProjectSekai-story")
    assert default_source.lang == "jp"
    assert default_source.strict is False
    assert default_source.lang_candidates() == ("jp",)

    # PJSK_STORY_DIR 与 PJSK_STORY_LANG
    monkeypatch.setenv("PJSK_STORY_DIR", str(tmp_path / "from-env"))
    monkeypatch.setenv("PJSK_STORY_LANG", "CN")
    env_source = module._resolve_story_source(None)
    assert env_source.root == tmp_path / "from-env"
    assert env_source.lang == "cn"
    assert env_source.lang_candidates() == ("cn", "jp")

    # 显式参数优先于环境变量
    explicit = module._resolve_story_source(tmp_path / "explicit", story_lang="jp", story_lang_strict=True)
    assert explicit.root == tmp_path / "explicit"
    assert explicit.lang == "jp"
    assert explicit.lang_candidates() == ("jp",)

    # 兼容旧的 MOE_STORY_DIR（优先级低于 PJSK_STORY_DIR）
    monkeypatch.delenv("PJSK_STORY_DIR")
    monkeypatch.setenv("MOE_STORY_DIR", str(tmp_path / "legacy"))
    assert module._resolve_story_source(None).root == tmp_path / "legacy"

    with pytest.raises(module.StorySummaryError):
        module._resolve_story_source(None, story_lang="kr")


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
    story_dir = tmp_path / "ProjectSekai-story"
    _write_upstream_txt(
        story_dir,
        "jp",
        2,
        1,
        "仲间们为了准备演出而努力。\n"
        "\n"
        "1-1 はじまり\n"
        "\n"
        "（登场角色：星乃一歌、天马咲希）\n"
        "\n"
        "Live House\n"
        "一歌：行こう、みんな。\n"
        "咲希：うん、楽しもう！\n",
    )
    _write_upstream_txt(
        story_dir,
        "jp",
        2,
        2,
        "仲间们为了准备演出而努力。\n"
        "\n"
        "2-1 おわり\n"
        "\n"
        "（登场角色：天马咲希）\n"
        "\n"
        "咲希：また次も頑張ろうね。\n",
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

    def fake_build_chapter_contents(source, event_meta):  # noqa: ANN001
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

    async def fake_generate_summary_rows(llm_config, event_meta, chapter_contents, *, event_title=None):  # noqa: ANN001
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

    def fake_build_chapter_contents(source, event_meta):  # noqa: ANN001
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
        raise module.StoryTextNotFoundError("Missing story txt for event_id=2 chapter=1 (tried lang=jp)")

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
            source=StorySource(root=Path("not-used")),
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
