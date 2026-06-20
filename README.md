# Sekai Data Pipeline

统一仓库维护以下数据：

**日常维护**：
1. `eventID -> 哔哩哔哩链接` 映射
2. 资讯站汉化四格漫画元数据与图片
3. Haruki 音乐别名库（全量歌曲 ID）
4. PJSK B30 JP/CN CSV 与合并表
5. PJSK BGM / MySekai BGM 时长索引
6. PJSK 活动剧情摘要（AI生成的中文翻译与总结）

**已停止维护**：
- PJSK 剧情原始 asset（brotli 压缩）- 代码保留但不再自动更新

## 目录结构

- `src/cli.py`：统一命令入口
- `src/tasks/event_bvid.py`：活动 B 站链接抓取与 eventID 映射
- `src/tasks/manga.py`：四格漫画元数据抓取与图片增量下载
- `src/tasks/music_alias.py`：Haruki 音乐别名抓取
- `src/tasks/b30_csv.py`：B30 JP/CN CSV 抓取与合并
- `src/tasks/bgm_duration.py`：BGM / MySekai BGM 时长增量索引
- `src/tasks/story_summary.py`：活动剧情摘要生成（基于LLM）
- `data/event_bvid/events_bilibili.json`：活动映射主文件
- `data/event_bvid/unmatched_events.json`：未匹配活动清单
- `data/music_alias/music_aliases.json`：音乐别名主文件
- `data/bgm_duration/bgm_durations.json`：BGM / MySekai BGM 时长索引
- `data/pjskb30/jp_chart.csv`：B30 日服原表
- `data/pjskb30/cn_chart.csv`：B30 国服原表
- `data/pjskb30/merged_chart.csv`：B30 合并表（不附加 `server` 字段）
- `mangas/mangas.json`、`mangas/*.webp`：四格漫画历史数据与图片（WebP 格式，由 `src/tasks/manga.py` 下载时实时转码）
- `story/detail/event_*.json`：活动剧情摘要（中文翻译与总结）
- `guides/guides-index.json`：攻略文章索引
- `guides/**/*.md`：攻略 Markdown 文件（按分类子目录组织）

## 本地使用

```bash
uv sync
uv run python -m src.cli update-event-bvid
uv run python -m src.cli update-manga
uv run python -m src.cli update-music-alias
uv run python -m src.cli update-b30-csv
uv run python -m src.cli update-bgm-duration
uv run python -m src.cli run-all
```

`update-manga` 可选读取环境变量 `BILIBILI_COOKIE`（私密仓库配置时使用）。

### 故事摘要生成

需要配置 LLM API：

```bash
export STORY_SUMMARY_API_KEY="your-api-key"
export STORY_SUMMARY_BASE_URL="https://api.openai.com/v1"  # 可选
export STORY_SUMMARY_MODEL="gpt-4.1-mini"  # 可选

# 生成最新活动摘要
uv run python -m src.cli update-story-summary

# 生成指定活动摘要
uv run python -m src.cli update-story-summary --event-id 123

# 强制重新生成
uv run python -m src.cli update-story-summary --force
```

### 漫画图片格式迁移

漫画图片以 WebP 格式存储。如需将历史 `mangas/*.png` 批量转换为 WebP，可使用迁移脚本：

```bash
uv run python scripts/convert_mangas_to_webp.py            # 转换并删除原 PNG
uv run python scripts/convert_mangas_to_webp.py --keep-png # 仅生成 WebP，保留原 PNG
```

## 数据格式

### `data/event_bvid/events_bilibili.json`

- 顶层：`generated_at`、`source`、`events`
- `events` 每项：`event_id`、`event_name`、`bilibili_url`、`bvid`、`match_status`
- 未匹配活动保留 `null` 链接与 `unmatched` 状态

### `data/music_alias/music_aliases.json`

- 顶层：`generated_at`、`source`、`musics`
- `musics` 每项：`music_id`、`title`、`aliases`
- 空别名保留为 `aliases: []`

### `data/pjskb30/merged_chart.csv`

- 列结构与源表一致：`Song,,Constant,Level,Note Count,Difficulty,Song ID,Notes`
- 合并规则：按顺序拼接 JP 行 + CN 行，不新增任何额外字段
- 校验规则：表头必须匹配；行数过小会报错并阻止落盘

### `data/bgm_duration/bgm_durations.json`

- 顶层：`generated_at`、`source`、`total_indexed`、`total_recorded`、`tracks`、`failures`
- `tracks` 每项：`key`、`route`、`file_name`、`size`、`etag`、`last_modified`、`duration_seconds`、`duration_milliseconds`、`duration_source`、`duration_fetched_at`
- 索引来源：`storage2.pjsk.moe/sekai-jp-assets/` 的 `sound/scenario/bgm/` 与 `mysekai/sound/bgm/`
- 时长来源：从 `storage.pjsk.moe/sekai-jp-assets/{key}` 读取 MP3 头部解析；已有有效时长的 `key` 会复用缓存，重复 CI 不重新请求音频文件

### `story/detail/event_*.json`

活动剧情摘要，包含：
- `event_id`：活动ID
- `title_jp`、`title_cn`：活动标题（日文/中文）
- `outline_jp`、`outline_cn`：活动简介（日文/中文）
- `summary_cn`：整体剧情概要（中文）
- `chapters`：章节列表，每章包含：
  - `chapter_no`：章节编号
  - `title_jp`、`title_cn`：章节标题（日文/中文）
  - `summary_cn`：章节剧情总结（中文）
  - `character_ids`：出场角色ID列表
  - `image_url`：章节封面图片URL

数据来源：从 `https://storage.exmeaning.com/sekai-jp-assets/` 获取剧情JSON，通过LLM生成中文翻译与摘要。

## GitHub Actions

- `daily-update.yml`：每天 UTC `00:00`（北京时间 `08:00`），运行四类基础数据更新任务
- `story-summary-update.yml`：每天 UTC `03:00`（北京时间 `11:00`），生成最新活动剧情摘要

## 主要数据来源

- 萌娘百科历史活动页（活动名 + B 站链接）
- `https://database.pjsekai.moe/events.json`
- B 站资讯站动态接口（四格漫画）
- `https://raw.githubusercontent.com/Team-Haruki/haruki-sekai-master/refs/heads/main/master/musics.json`
- `https://public-api.haruki.seiunx.com/alias/v1/music/{mid}`
- `https://storage2.pjsk.moe/sekai-jp-assets/`（BGM 对象索引）
- `https://storage.pjsk.moe/sekai-jp-assets/`（BGM MP3 头部读取）
- `https://docs.google.com/spreadsheets/d/1B8tX9VL2PcSJKyuHFVd2UT_8kYlY4ZdwHwg9MfWOPug/export?format=csv&gid=1855810409`
- `https://docs.google.com/spreadsheets/d/1Yv3GXnCIgEIbHL72EuZ-d5q_l-auPgddWi4Efa14jq0/export?format=csv&gid=182216`
- `https://sekaimaster.exmeaning.com/master/`（游戏主数据）
- `https://storage.exmeaning.com/sekai-jp-assets/`（剧情JSON数据）
- `https://storage.sekai.best/`（sekai.best asset CDN，已停止使用）
- `https://sekai-assets-bdf29c81.seiunx.net/`（haruki asset CDN，已停止使用）
