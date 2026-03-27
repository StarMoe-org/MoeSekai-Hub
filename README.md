# Sekai Data Pipeline

统一仓库维护五类日更数据：
1. `eventID -> 哔哩哔哩链接` 映射
2. 资讯站汉化四格漫画元数据与图片
3. Haruki 音乐别名库（全量歌曲 ID）
4. PJSK B30 JP/CN CSV 与合并表
5. PJSK 剧情原始 asset（brotli 压缩）

## 目录结构

- `src/cli.py`：统一命令入口
- `src/tasks/event_bvid.py`：活动 B 站链接抓取与 eventID 映射
- `src/tasks/manga.py`：四格漫画元数据抓取与图片增量下载
- `src/tasks/music_alias.py`：Haruki 音乐别名抓取
- `src/tasks/b30_csv.py`：B30 JP/CN CSV 抓取与合并
- `src/tasks/story_asset.py`：剧情 asset 爬虫
- `src/tasks/story_asset_urls.json`：数据源 URL 配置
- `data/event_bvid/events_bilibili.json`：活动映射主文件
- `data/event_bvid/unmatched_events.json`：未匹配活动清单
- `data/music_alias/music_aliases.json`：音乐别名主文件
- `data/pjskb30/jp_chart.csv`：B30 日服原表
- `data/pjskb30/cn_chart.csv`：B30 国服原表
- `data/pjskb30/merged_chart.csv`：B30 合并表（不附加 `server` 字段）
- `mangas/mangas.json`、`mangas/*.png`：四格漫画历史数据与图片
- `story_assets/`：剧情原始 asset，按原始 URL 有效路径镜像存储，`.asset.br` 为 brotli 压缩的 JSON
- `guides/guides-index.json`：攻略文章索引
- `guides/**/*.md`：攻略 Markdown 文件（按分类子目录组织）

## 本地使用

```bash
uv sync
uv run python -m src.cli update-event-bvid
uv run python -m src.cli update-manga
uv run python -m src.cli update-music-alias
uv run python -m src.cli update-b30-csv
uv run python -m src.cli run-all
```

`update-manga` 可选读取环境变量 `BILIBILI_COOKIE`（私密仓库配置时使用）。

剧情 asset 爬虫单独运行：

```bash
# 增量更新（默认四个源）
uv run python -m src.cli update-story-asset

# 指定源
uv run python -m src.cli update-story-asset --lang-src jp sekai.best --lang-src cn haruki

# 全量覆盖更新
uv run python -m src.cli update-story-asset --full
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

### `story_assets/`

- 路径结构镜像原始 URL，去掉通用前缀后直接作为相对路径，附加 `.br` 后缀
- 例：`https://storage.sekai.best/sekai-jp-assets/event_story/ev_01/scenario/ev_01_01.asset`
  → `story_assets/pjsk-jp-assets/event_story/ev_01/scenario/ev_01_01.asset.br`
- 文件内容为原始 JSON compact 序列化后经 brotli（quality=11）压缩的二进制数据
- 覆盖六类剧情：活动剧情（event）、组合剧情（unit）、卡面剧情（card）、区域对话（talk）、自我介绍（self）、特殊剧情（special）
- 支持两个语言、两个数据源：`jp haruki sekai.best`、`cn haruki sekai.best`

读取示例：

```python
import brotli, json
data = json.loads(brotli.decompress(open("story_assets/...", "rb").read()))
```

## GitHub Actions

- `daily-update.yml`：每天 UTC `00:00`（北京时间 `08:00`），运行原有四类数据更新任务
- `story-asset-daily-update.yml`：每天 UTC `07:00`（北京时间 `15:00`），四个源增量爬取剧情 asset
- `story-asset-full-update.yml`：手动触发，四个源全量覆盖更新，支持自定义 lang/src 组合

每个任务均 `continue-on-error: true`；仅当全部任务失败时 workflow 失败。

## 主要数据来源

- 萌娘百科历史活动页（活动名 + B 站链接）
- `https://database.pjsekai.moe/events.json`
- B 站资讯站动态接口（四格漫画）
- `https://raw.githubusercontent.com/Team-Haruki/haruki-sekai-master/refs/heads/main/master/musics.json`
- `https://public-api.haruki.seiunx.com/alias/v1/music/{mid}`
- `https://docs.google.com/spreadsheets/d/1B8tX9VL2PcSJKyuHFVd2UT_8kYlY4ZdwHwg9MfWOPug/export?format=csv&gid=1855810409`
- `https://docs.google.com/spreadsheets/d/1Yv3GXnCIgEIbHL72EuZ-d5q_l-auPgddWi4Efa14jq0/export?format=csv&gid=182216`
- `https://storage.sekai.best/`（sekai.best asset CDN）
- `https://sekai-assets-bdf29c81.seiunx.net/`（haruki asset CDN）
