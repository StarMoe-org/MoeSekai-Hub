from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import brotli
import httpx

from src.common.http import build_headers, get_json, request_with_retry
from src.common.io import read_json, write_json

_URLS_JSON = Path(__file__).with_name("story_asset_urls.json")
_TRANSLATION_EVENTS_JSON = Path("translation/events.json")
_DEFAULT_MASTER_SRCS = ["sekai.best", "haruki"]
_DEFAULT_ASSET_DIR = Path("story_assets")
_DEFAULT_OUTPUT_DIR = Path("story/detail")
_JSON_BLOCK_PATTERN = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)

_TALK_ACTION = 6
_SPECIAL_EFFECT_ACTION = 1
_SPECIAL_EFFECT_TITLE = 8
_EVENT_OUTPUT_LEN_LIMIT = 1000
_EVENT_TARGET_LEN_LONG = 200
_EVENT_TARGET_LEN_SHORT = 200
_EPISODE_IMAGE_URL_TEMPLATE = (
    "https://cdn.jsdelivr.net/gh/Exmeaning/Exmeaning-Image-hosting@main/"
    "event_story/{assetbundle_name}/{assetbundle_name}_{chapter_no:02d}.webp"
)

_SUMMARY_SYSTEM_PROMPT = (
    "你是日文游戏剧情翻译与总结助手。"
    "请严格按照用户要求输出 JSON，不要输出 JSON 以外的任何解释。"
)

_PROMPT_HEAD = """给定日文的游戏剧情标题、简介、各个章节的标题以及对话，请你：
1. 将剧情标题、简介、各个章节的标题翻译到**简体中文**
2. 生成每章节对话的中文总结，需要包含该章节中的所有信息，并确保总结准确反映该章节内容。不需要分点，而是用一段文字流畅地总结

游戏背景设定供参考：
游戏中存在一个现实世界和虚拟世界「世界」，游戏的主人公团体（5个团体，每个团有4个角色）各有一个「世界」，他们能够通过电子设备上神秘出现的「Untitled」歌曲往返现实世界和「世界」。在「世界」中，原本在现实世界的虚拟歌手（比如初音未来）变为了真实的存在，能够与主人公团体互动。「世界」反映的是主人的强烈的心愿，而虚拟歌手们会帮助主人公团体一步步达成他们的心愿。游戏的故事围绕着主人公团体于虚拟歌手在现实与「世界」之间的故事展开。

以下译名表供你使用，为了保持翻译的流畅性，你可以进行适当调整（例如省略姓名间的空格，加上称谓等）
组合: Virtual Singer(虚拟歌手)
- 初音 ミク(はつね みく): 初音 未来
- 鏡音 リン(かがみね りん): 镜音 铃
- 鏡音 レン(かがみね れん): 镜音 连
- 巡音 ルカ(めぐりね るか): 巡音 流歌
- MEIKO(めいこ): MEIKO
- KAITO(かいと): KAITO

组合: Leo/need
该组合的世界为: 教室のセカイ(教室的世界)
- 星乃 一歌(ほしの いちか): 星乃 一歌
- 天馬 咲希(てんま さき): 天马 咲希
- 望月 穂波(もちづき ほなみ): 望月 穗波
- 日野森 志歩(ひのもり しほ): 日野森 志步

组合: MORE MORE JUMP!
该组合的世界为: ステージのセカイ(舞台的世界)
- 花里 みのり(はなざと みのり): 花里 实乃里
- 桐谷 遥(きりたに はるか): 桐谷 遥
- 桃井 愛莉(ももい あいり): 桃井 爱莉
- 日野森 雫(ひのもり しずく): 日野森 雫

组合: Vivid BAD SQUAD
该组合的世界为: ストリートのセカイ(街头的世界)
- 小豆沢 こはね(あずさわ こはね): 小豆泽 心羽
- 白石 杏(しらいし あん): 白石 杏
- 東雲 彰人(しののめ あきと): 东云 彰人
- 青柳 冬弥(あおやぎ とうや): 青柳 冬弥

组合: ワンダーランズ×ショウタイム
该组合的世界为: ワンダーランドのセカイ(奇幻的世界)
- 天馬 司(てんま つかさ): 天马 司
- 鳳 えむ(おおとり えむ): 凤 笑梦
- 草薙 寧々(くさなぎ ねね): 草薙 宁宁
- 神代 類(かみしろ るい): 神代 类

组合: 25時、ナイトコードで。
该组合的世界为: 誰もいないセカイ(无人的世界)
- 宵崎 奏(よいさき かなで): 宵崎 奏
- 朝比奈 まふゆ(あさひな まふゆ): 朝比奈 真冬
- 東雲 絵名(しののめ えな): 东云 绘名
- 暁山 瑞希(あかつきやま みずき): 暁山 瑞希
""".strip()

_PROMPT_START_TEMPLATE = """接下来我将给你活动的总标题、简介，以及第1章的对话，请你进行翻译
记住：在保证信息完整的情况下尽量简洁，字数不超过{limit}字
章节标题请翻译原始日文标题本身，不要自行补成“第1话”“第2章”这类编号标题。
本次输出格式如下（只需要输出花括号及以内的内容）：
{{
    "title": "翻译过的剧情标题",
    "outline": "翻译过的简介",
    "ep_1_title": "翻译过的第1章标题",
    "ep_1_summary": "翻译过的第1章对话总结"
}}

标题: {title}
简介: {outline}
第1章对话:
```
{raw_story}
```
""".strip()

_PROMPT_EP_TEMPLATE = """接下来我将给你第{ep}章的对话，请你结合之前已经翻译过的内容进行翻译
记住：在保证信息完整的情况下尽量简洁，字数不超过{limit}字
章节标题请翻译原始日文标题本身，不要自行补成“第{ep}话”“第{ep}章”这类编号标题。
本次输出格式如下（只需要输出花括号及以内的内容，只需要输出第{ep}章的结果！）：
{{
    "ep_{ep}_title": "翻译过的第{ep}章标题",
    "ep_{ep}_summary": "翻译过的第{ep}章对话总结"
}}

以下是之前已经翻译过的内容，你需要保持翻译的一致性和连贯性：
```
{prev_summary}
```

以下是你需要翻译的第{ep}章对话:
```
{raw_story}
```
""".strip()

_PROMPT_END_TEMPLATE = """请你根据已有的翻译的内容生成一段流畅的总体剧情概要，确保概要反应了整个剧情的核心内容和主要事件
在保证信息完整的情况下尽量简洁，字数不超过{limit}字
以下是之前已经翻译过的内容：
```
{prev_summary}
```

本次输出格式如下（只需要输出花括号及以内的内容）：
{{
    "summary": "翻译过的总体剧情概要"
}}
""".strip()


class StorySummaryError(RuntimeError):
    """Raised when the story summary pipeline cannot produce a valid result."""


@dataclass(frozen=True)
class LLMConfig:
    api_key: str
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4.1-mini"
    timeout_seconds: float = 120.0
    temperature: float = 0.0


@dataclass(frozen=True)
class EpisodeMeta:
    chapter_no: int
    title_jp: str
    scenario_id: str
    image_url: str


@dataclass(frozen=True)
class EventMeta:
    event_id: int
    title_jp: str
    outline_jp: str
    assetbundle_name: str
    episodes: tuple[EpisodeMeta, ...]


@dataclass(frozen=True)
class StorySnippet:
    names: tuple[str, ...] | None
    text: str


@dataclass(frozen=True)
class ChapterContent:
    meta: EpisodeMeta
    prompt_text: str
    character_ids: tuple[int, ...]
    snippet_count: int
    implemented: bool


def _load_urls() -> dict[str, Any]:
    with _URLS_JSON.open("r", encoding="utf-8") as f:
        result: dict[str, Any] = json.load(f)
        return result


def _master_url(urls: dict[str, Any], src: str, lang: str, file_name: str) -> str:
    base: str = urls[src]["master"]
    lang_prefix: str = urls[src]["master_lang"][lang]
    return base.format(lang=lang_prefix, file=file_name)


def _build_episode_image_url(assetbundle_name: str, chapter_no: int) -> str:
    return _EPISODE_IMAGE_URL_TEMPLATE.format(assetbundle_name=assetbundle_name, chapter_no=chapter_no)


def _create_async_client(*, headers: dict[str, str] | None = None, timeout_seconds: float = 30.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers=build_headers(headers),
        timeout=httpx.Timeout(timeout_seconds),
        follow_redirects=True,
        trust_env=False,
    )


async def _fetch_master_json(file_name: str, *, lang: str = "jp", srcs: list[str] | None = None) -> Any:
    if srcs is None:
        srcs = _DEFAULT_MASTER_SRCS
    urls = _load_urls()
    async with _create_async_client(timeout_seconds=30.0) as client:
        for src in srcs:
            try:
                return await get_json(client, _master_url(urls, src, lang, file_name))
            except Exception as exc:
                print(f"[story-summary] master {file_name} failed on {src}: {type(exc).__name__}: {exc}")
    raise StorySummaryError(f"All master sources failed for {file_name}")


def _build_event_meta(event_row: dict[str, Any], event_story_row: dict[str, Any]) -> EventMeta:
    target_event_id = int(event_story_row["eventId"])
    title_jp = str(event_row.get("name") or "").strip()
    outline_jp = str(event_story_row.get("outline") or "").strip()
    assetbundle_name = str(event_story_row.get("assetbundleName") or "").strip()
    episodes_raw = event_story_row.get("eventStoryEpisodes")

    if not title_jp or not outline_jp or not assetbundle_name or not isinstance(episodes_raw, list):
        raise StorySummaryError(f"Incomplete master data for event_id={target_event_id}")

    episodes: list[EpisodeMeta] = []
    for row in sorted(episodes_raw, key=lambda item: int(item.get("episodeNo", 0))):
        if not isinstance(row, dict):
            continue
        chapter_no = row.get("episodeNo")
        chapter_title = row.get("title")
        scenario_id = row.get("scenarioId")
        if not isinstance(chapter_no, int) or not isinstance(chapter_title, str) or not isinstance(scenario_id, str):
            continue
        episodes.append(
            EpisodeMeta(
                chapter_no=chapter_no,
                title_jp=chapter_title.strip(),
                scenario_id=scenario_id.strip(),
                image_url=_build_episode_image_url(assetbundle_name, chapter_no),
            )
        )

    if not episodes:
        raise StorySummaryError(f"No valid episodes found for event_id={target_event_id}")

    return EventMeta(
        event_id=target_event_id,
        title_jp=title_jp,
        outline_jp=outline_jp,
        assetbundle_name=assetbundle_name,
        episodes=tuple(episodes),
    )


async def _fetch_event_metas(event_id: int | None = None) -> tuple[EventMeta, ...]:
    events_raw, event_stories_raw = await asyncio.gather(
        _fetch_master_json("events"),
        _fetch_master_json("eventStories"),
    )

    if not isinstance(events_raw, list) or not isinstance(event_stories_raw, list):
        raise StorySummaryError("Unexpected master payload shape")

    event_map = {
        item.get("id"): item
        for item in events_raw
        if isinstance(item, dict) and isinstance(item.get("id"), int)
    }

    event_story_rows: list[dict[str, Any]] = [
        item
        for item in event_stories_raw
        if isinstance(item, dict) and isinstance(item.get("eventId"), int)
    ]
    if not event_story_rows:
        raise StorySummaryError("No event story rows found in master data")

    if event_id is None:
        target_rows = sorted(event_story_rows, key=lambda item: int(item["eventId"]))
    else:
        target_rows = [item for item in event_story_rows if item["eventId"] == event_id]
        if not target_rows:
            raise StorySummaryError(f"No event story found for event_id={event_id}")

    metas: list[EventMeta] = []
    for target_story in target_rows:
        target_event_id = int(target_story["eventId"])
        event_row = event_map.get(target_event_id)
        if event_row is None:
            if event_id is not None:
                raise StorySummaryError(f"No event master row found for event_id={target_event_id}")
            print(f"[story-summary] skip event_id={target_event_id}: missing event master row")
            continue
        try:
            metas.append(_build_event_meta(event_row, target_story))
        except StorySummaryError:
            if event_id is not None:
                raise
            print(f"[story-summary] skip event_id={target_event_id}: incomplete story master data")

    if not metas:
        raise StorySummaryError("No valid event story metadata found")
    return tuple(metas)


async def _fetch_event_meta(event_id: int | None = None) -> EventMeta:
    metas = await _fetch_event_metas(event_id)
    if event_id is None:
        return metas[-1]
    return metas[0]


async def _fetch_character2d_map() -> dict[int, int]:
    payload = await _fetch_master_json("character2ds")
    if not isinstance(payload, list):
        raise StorySummaryError("Unexpected character2ds payload shape")

    result: dict[int, int] = {}
    for row in payload:
        if not isinstance(row, dict):
            continue
        character_2d_id = row.get("id")
        character_id = row.get("characterId")
        if isinstance(character_2d_id, int) and isinstance(character_id, int):
            result[character_2d_id] = character_id
    return result


def _scenario_asset_path(asset_dir: Path, event_meta: EventMeta, episode: EpisodeMeta) -> Path:
    return asset_dir / "pjsk-jp-assets" / "event_story" / event_meta.assetbundle_name / "scenario" / f"{episode.scenario_id}.asset.br"


def _load_story_payload(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise StorySummaryError(f"Story asset does not exist: {path}")
    data = json.loads(brotli.decompress(path.read_bytes()))
    if not isinstance(data, dict):
        raise StorySummaryError(f"Unexpected story asset payload shape: {path}")
    return data


def _strip_text(value: str) -> str:
    return value.replace("\r\n", "\n").strip()


def _split_display_names(value: str) -> tuple[str, ...] | None:
    names = tuple(part.strip() for part in value.split("・") if part.strip())
    return names or None


def _extract_story_snippets(payload: dict[str, Any]) -> tuple[StorySnippet, ...]:
    snippets = payload.get("Snippets")
    talks = payload.get("TalkData")
    special_effects = payload.get("SpecialEffectData")
    if not isinstance(snippets, list):
        return ()

    ordered_rows = sorted((item for item in snippets if isinstance(item, dict)), key=lambda item: int(item.get("Index", 0)))
    result: list[StorySnippet] = []
    for row in ordered_rows:
        action = row.get("Action")
        ref = row.get("ReferenceIndex")
        if not isinstance(ref, int) or ref < 0:
            continue

        if action == _TALK_ACTION and isinstance(talks, list) and ref < len(talks) and isinstance(talks[ref], dict):
            talk = talks[ref]
            body = talk.get("Body")
            if not isinstance(body, str):
                continue
            body_text = _strip_text(body)
            if not body_text:
                continue
            speaker = talk.get("WindowDisplayName")
            names = _split_display_names(speaker) if isinstance(speaker, str) else None
            result.append(StorySnippet(names=names, text=body_text))
            continue

        if action == _SPECIAL_EFFECT_ACTION and isinstance(special_effects, list) and ref < len(special_effects):
            effect = special_effects[ref]
            if not isinstance(effect, dict) or effect.get("EffectType") != _SPECIAL_EFFECT_TITLE:
                continue
            text = effect.get("StringVal")
            if not isinstance(text, str):
                continue
            effect_text = _strip_text(text)
            if effect_text:
                result.append(StorySnippet(names=None, text=effect_text))

    return tuple(result)


def _extract_character_ids(payload: dict[str, Any], character2d_map: dict[int, int]) -> tuple[int, ...]:
    appear_characters = payload.get("AppearCharacters")
    talks = payload.get("TalkData")

    result: list[int] = []
    seen: set[int] = set()

    def add_character(character_2d_id: Any) -> None:
        if not isinstance(character_2d_id, int):
            return
        character_id = character2d_map.get(character_2d_id)
        if character_id is None or character_id in seen:
            return
        seen.add(character_id)
        result.append(character_id)

    if isinstance(appear_characters, list):
        for row in appear_characters:
            if isinstance(row, dict):
                add_character(row.get("Character2dId"))

    if result:
        return tuple(result)

    if isinstance(talks, list):
        for talk in talks:
            if not isinstance(talk, dict):
                continue
            talk_characters = talk.get("TalkCharacters")
            if not isinstance(talk_characters, list):
                continue
            for row in talk_characters:
                if isinstance(row, dict):
                    add_character(row.get("Character2dId"))

    return tuple(result)


def _build_prompt_story_text(chapter_no: int, chapter_title: str, snippets: tuple[StorySnippet, ...]) -> str:
    parts = [f"【EP{chapter_no}: {chapter_title}】"]
    for snippet in snippets:
        if snippet.names:
            parts.append(f"---\n{' & '.join(snippet.names)}:\n{snippet.text}")
        else:
            parts.append(f"---\n({snippet.text})")
    return "\n".join(parts) + "\n"


def _build_chapter_contents(event_meta: EventMeta, asset_dir: Path, character2d_map: dict[int, int]) -> tuple[ChapterContent, ...]:
    results: list[ChapterContent] = []
    for episode in event_meta.episodes:
        payload = _load_story_payload(_scenario_asset_path(asset_dir, event_meta, episode))
        story_snippets = _extract_story_snippets(payload)
        results.append(
            ChapterContent(
                meta=episode,
                prompt_text=_build_prompt_story_text(episode.chapter_no, episode.title_jp, story_snippets) if story_snippets else "",
                character_ids=_extract_character_ids(payload, character2d_map),
                snippet_count=len(story_snippets),
                implemented=bool(story_snippets),
            )
        )
    return tuple(results)


def _load_translation_name_map() -> dict[str, str]:
    payload = read_json(_TRANSLATION_EVENTS_JSON, default={})
    if not isinstance(payload, dict):
        return {}
    names = payload.get("name")
    if not isinstance(names, dict):
        return {}
    return {
        str(key): str(value)
        for key, value in names.items()
        if isinstance(key, str) and isinstance(value, str) and key.strip() and value.strip()
    }


def _chat_completions_url(base_url: str) -> str:
    cleaned = base_url.rstrip("/")
    if cleaned.endswith("/chat/completions"):
        return cleaned
    return f"{cleaned}/chat/completions"


def _extract_message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)
        ]
        return "".join(texts)
    return ""


def _parse_response_json(text: str) -> dict[str, Any]:
    candidates = [text.strip()]
    fenced = _JSON_BLOCK_PATTERN.search(text)
    if fenced is not None:
        candidates.insert(0, fenced.group(1).strip())

    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        candidates.append(text[first_brace : last_brace + 1].strip())

    for candidate in candidates:
        if not candidate:
            continue
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise StorySummaryError("Model did not return a valid JSON object")


def _truncate_for_prompt(text: str, limit: int = 12000) -> str:
    if len(text) <= limit:
        return text
    head = text[: int(limit * 0.7)].rstrip()
    tail = text[-int(limit * 0.25) :].lstrip()
    return f"{head}\n...（中间内容已截断）...\n{tail}"


async def _chat_completion_json(
    llm_config: LLMConfig,
    *,
    system_prompt: str,
    user_prompt: str,
    max_response_length: int = _EVENT_OUTPUT_LEN_LIMIT,
) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {llm_config.api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": llm_config.model,
        "temperature": llm_config.temperature,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }

    async with _create_async_client(headers=headers, timeout_seconds=llm_config.timeout_seconds) as client:
        response = await request_with_retry(client, "POST", _chat_completions_url(llm_config.base_url), json=payload)
    data = response.json()
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise StorySummaryError("Model response does not contain choices")

    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        raise StorySummaryError("Model response does not contain a valid message")

    text = _extract_message_text(message.get("content"))
    if not text.strip():
        raise StorySummaryError("Model returned empty content")
    if len(text) > max_response_length:
        raise StorySummaryError(f"Model response exceeds length limit ({len(text)}>{max_response_length})")
    return _parse_response_json(text)


def _require_text_field(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise StorySummaryError(f"Model response missing string field: {key}")
    normalized = value.strip()
    if not normalized:
        raise StorySummaryError(f"Model response returned empty string for: {key}")
    return normalized


def _target_length(total_implemented_chapters: int) -> int:
    if total_implemented_chapters >= 10:
        return _EVENT_TARGET_LEN_SHORT
    return _EVENT_TARGET_LEN_LONG


def _build_start_prompt(event_meta: EventMeta, chapter: ChapterContent, *, limit: int) -> str:
    return "\n\n".join(
        [
            _PROMPT_HEAD,
            _PROMPT_START_TEMPLATE.format(
                title=event_meta.title_jp,
                outline=event_meta.outline_jp,
                raw_story=_truncate_for_prompt(chapter.prompt_text),
                limit=limit,
            ),
        ]
    )


def _build_chapter_prompt(chapter_no: int, chapter: ChapterContent, previous_summary: str, *, limit: int) -> str:
    return "\n\n".join(
        [
            _PROMPT_HEAD,
            _PROMPT_EP_TEMPLATE.format(
                ep=chapter_no,
                raw_story=_truncate_for_prompt(chapter.prompt_text),
                limit=limit,
                prev_summary=_truncate_for_prompt(previous_summary),
            ),
        ]
    )


def _build_end_prompt(previous_summary: str, *, limit: int) -> str:
    return "\n\n".join(
        [
            _PROMPT_HEAD,
            _PROMPT_END_TEMPLATE.format(limit=limit, prev_summary=_truncate_for_prompt(previous_summary)),
        ]
    )


async def _generate_summary_rows(
    llm_config: LLMConfig,
    event_meta: EventMeta,
    chapter_contents: tuple[ChapterContent, ...],
    translation_name_map: dict[str, str],
) -> tuple[str, str, str, list[dict[str, Any]]]:
    implemented_chapters = [chapter for chapter in chapter_contents if chapter.implemented]
    if not implemented_chapters:
        raise StorySummaryError(f"No story snippets extracted for event_id={event_meta.event_id}")

    limit = _target_length(len(implemented_chapters))
    chapter_rows_by_no: dict[int, dict[str, Any]] = {}

    start_payload = await _chat_completion_json(
        llm_config,
        system_prompt=_SUMMARY_SYSTEM_PROMPT,
        user_prompt=_build_start_prompt(event_meta, implemented_chapters[0], limit=limit),
    )

    title_cn_generated = _require_text_field(start_payload, "title")
    outline_cn = _require_text_field(start_payload, "outline")
    first_chapter_title_cn = _require_text_field(start_payload, "ep_1_title")
    first_chapter_summary_cn = _require_text_field(start_payload, "ep_1_summary")

    title_cn = translation_name_map.get(event_meta.title_jp) or title_cn_generated

    first_chapter = implemented_chapters[0]
    chapter_rows_by_no[first_chapter.meta.chapter_no] = {
        "chapter_no": first_chapter.meta.chapter_no,
        "title_jp": first_chapter.meta.title_jp,
        "title_cn": first_chapter_title_cn,
        "summary_cn": first_chapter_summary_cn,
        "character_ids": list(first_chapter.character_ids),
        "image_url": first_chapter.meta.image_url,
    }

    previous_summary = f"标题: {title_cn}\n"
    previous_summary += f"简介: {outline_cn}\n\n"
    previous_summary += f"第{first_chapter.meta.chapter_no}章标题: {first_chapter_title_cn}\n"
    previous_summary += f"第{first_chapter.meta.chapter_no}章剧情: {first_chapter_summary_cn}\n\n"

    for chapter in implemented_chapters[1:]:
        chapter_no = chapter.meta.chapter_no
        payload = await _chat_completion_json(
            llm_config,
            system_prompt=_SUMMARY_SYSTEM_PROMPT,
            user_prompt=_build_chapter_prompt(chapter_no, chapter, previous_summary, limit=limit),
        )
        title_key = f"ep_{chapter_no}_title"
        summary_key = f"ep_{chapter_no}_summary"
        chapter_title_cn = _require_text_field(payload, title_key)
        chapter_summary_cn = _require_text_field(payload, summary_key)
        chapter_rows_by_no[chapter_no] = {
            "chapter_no": chapter_no,
            "title_jp": chapter.meta.title_jp,
            "title_cn": chapter_title_cn,
            "summary_cn": chapter_summary_cn,
            "character_ids": list(chapter.character_ids),
            "image_url": chapter.meta.image_url,
        }
        previous_summary += f"第{chapter_no}章标题: {chapter_title_cn}\n"
        previous_summary += f"第{chapter_no}章剧情: {chapter_summary_cn}\n\n"

    end_payload = await _chat_completion_json(
        llm_config,
        system_prompt=_SUMMARY_SYSTEM_PROMPT,
        user_prompt=_build_end_prompt(previous_summary, limit=limit),
    )
    summary_cn = _require_text_field(end_payload, "summary")

    chapter_rows: list[dict[str, Any]] = []
    for chapter in chapter_contents:
        existing_row = chapter_rows_by_no.get(chapter.meta.chapter_no)
        if existing_row is not None:
            chapter_rows.append(existing_row)
            continue
        chapter_rows.append(
            {
                "chapter_no": chapter.meta.chapter_no,
                "title_jp": chapter.meta.title_jp,
                "title_cn": chapter.meta.title_jp,
                "summary_cn": "(章节剧情未实装)",
                "character_ids": list(chapter.character_ids),
                "image_url": chapter.meta.image_url,
            }
        )

    return title_cn, outline_cn, summary_cn, chapter_rows


def _resolve_llm_config(llm_config: LLMConfig | None) -> LLMConfig:
    if llm_config is not None:
        return llm_config

    api_key = os.environ.get("STORY_SUMMARY_API_KEY", "").strip()
    if not api_key:
        raise StorySummaryError("Missing STORY_SUMMARY_API_KEY")

    base_url = os.environ.get("STORY_SUMMARY_BASE_URL", "https://api.openai.com/v1").strip() or "https://api.openai.com/v1"
    model = os.environ.get("STORY_SUMMARY_MODEL", "gpt-4.1-mini").strip() or "gpt-4.1-mini"
    return LLMConfig(api_key=api_key, base_url=base_url, model=model)


def _output_path(output_dir: Path, event_id: int) -> Path:
    return output_dir / f"event_{event_id:03d}.json"


def _append_step_summary(output_path: Path, payload: dict[str, Any]) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY", "").strip()
    if not summary_path:
        return

    lines = [
        "## 最新活动剧情总结",
        f"- 活动 ID：`{payload['event_id']}`",
        f"- 活动标题：{payload['title_cn']} / {payload['title_jp']}",
        f"- 输出文件：`{output_path.as_posix()}`",
        f"- 章节数：{len(payload.get('chapters', []))}",
        "",
        payload["summary_cn"],
        "",
    ]
    with Path(summary_path).open("a", encoding="utf-8") as f:
        f.write("\n".join(lines))


def _existing_chapter_count(output_path: Path) -> int:
    existing_payload = read_json(output_path, default=None)
    existing_chapters = existing_payload.get("chapters") if isinstance(existing_payload, dict) else None
    return len(existing_chapters) if isinstance(existing_chapters, list) else 0


def _should_skip_event(output_path: Path, episode_count: int, *, force: bool) -> bool:
    if force or not output_path.exists():
        return False
    return _existing_chapter_count(output_path) >= episode_count


async def _generate_event_summary_file(
    event_meta: EventMeta,
    *,
    asset_dir: Path,
    output_dir: Path,
    character2d_map: dict[int, int],
    llm_config: LLMConfig,
    translation_name_map: dict[str, str],
) -> tuple[int, int]:
    chapter_contents = _build_chapter_contents(event_meta, asset_dir, character2d_map)
    title_cn, outline_cn, summary_cn, chapter_rows = await _generate_summary_rows(
        llm_config,
        event_meta,
        chapter_contents,
        translation_name_map,
    )

    payload = {
        "event_id": event_meta.event_id,
        "title_jp": event_meta.title_jp,
        "title_cn": title_cn,
        "outline_jp": event_meta.outline_jp,
        "outline_cn": outline_cn,
        "summary_cn": summary_cn,
        "chapters": chapter_rows,
    }
    output_path = _output_path(output_dir, event_meta.event_id)
    write_json(output_path, payload)
    _append_step_summary(output_path, payload)
    return len(chapter_rows), sum(chapter.snippet_count for chapter in chapter_contents)


async def update_story_summary(
    *,
    event_id: int | None = None,
    asset_dir: Path = _DEFAULT_ASSET_DIR,
    output_dir: Path = _DEFAULT_OUTPUT_DIR,
    force: bool = False,
    llm_config: LLMConfig | None = None,
) -> dict[str, int]:
    event_metas = await _fetch_event_metas(event_id)

    if event_id is not None:
        event_meta = event_metas[0]
        output_path = _output_path(output_dir, event_meta.event_id)
        if _should_skip_event(output_path, len(event_meta.episodes), force=force):
            return {
                "event_id": event_meta.event_id,
                "chapters_total": len(event_meta.episodes),
                "dialogue_lines_total": 0,
                "generated_files": 0,
                "skipped_existing": 1,
            }

        resolved_llm_config = _resolve_llm_config(llm_config)
        character2d_map = await _fetch_character2d_map()
        translation_name_map = _load_translation_name_map()
        chapters_total, dialogue_lines_total = await _generate_event_summary_file(
            event_meta,
            asset_dir=asset_dir,
            output_dir=output_dir,
            character2d_map=character2d_map,
            llm_config=resolved_llm_config,
            translation_name_map=translation_name_map,
        )
        return {
            "event_id": event_meta.event_id,
            "chapters_total": chapters_total,
            "dialogue_lines_total": dialogue_lines_total,
            "generated_files": 1,
            "skipped_existing": 0,
        }

    pending_event_metas: list[EventMeta] = []
    skipped_existing = 0
    for event_meta in event_metas:
        output_path = _output_path(output_dir, event_meta.event_id)
        if _should_skip_event(output_path, len(event_meta.episodes), force=force):
            skipped_existing += 1
            continue
        pending_event_metas.append(event_meta)

    if not pending_event_metas:
        return {
            "events_total": len(event_metas),
            "generated_events": 0,
            "chapters_total": 0,
            "dialogue_lines_total": 0,
            "generated_files": 0,
            "failed_events": 0,
            "skipped_existing": skipped_existing,
        }

    resolved_llm_config = _resolve_llm_config(llm_config)
    character2d_map = await _fetch_character2d_map()
    translation_name_map = _load_translation_name_map()

    generated_events = 0
    failed_events = 0
    chapters_total = 0
    dialogue_lines_total = 0
    for event_meta in pending_event_metas:
        try:
            event_chapters_total, event_dialogue_lines_total = await _generate_event_summary_file(
                event_meta,
                asset_dir=asset_dir,
                output_dir=output_dir,
                character2d_map=character2d_map,
                llm_config=resolved_llm_config,
                translation_name_map=translation_name_map,
            )
        except Exception as exc:
            failed_events += 1
            print(f"[story-summary] skip event_id={event_meta.event_id}: {type(exc).__name__}: {exc}")
            continue
        generated_events += 1
        chapters_total += event_chapters_total
        dialogue_lines_total += event_dialogue_lines_total

    return {
        "events_total": len(event_metas),
        "generated_events": generated_events,
        "chapters_total": chapters_total,
        "dialogue_lines_total": dialogue_lines_total,
        "generated_files": generated_events,
        "failed_events": failed_events,
        "skipped_existing": skipped_existing,
    }
