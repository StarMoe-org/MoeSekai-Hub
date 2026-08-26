from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from src.cli import _parse_music_bpm_ids
from src.tasks import music_bpm as module
from src.tasks.music_bpm import build_bpm_record, parse_bpm_segments


def test_parse_bpm_segments_multiple_sorted() -> None:
    text = (
        "#BPM01: 185\n#BPM02: 142\n"
        "#00008: 01\n#01208: 02\n#02908: 01\n"
        "#05220: 26\n"  # 最后事件小节 52，end_bar=53
    )
    segments = parse_bpm_segments(text)
    assert segments is not None
    assert [(s["bpm"], s["start_bar"]) for s in segments] == [(185.0, 0.0), (142.0, 12.0), (185.0, 29.0)]
    assert segments[0]["duration_sec"] == pytest.approx(12 * 4 * 60 / 185, abs=0.01)  # 15.6
    assert segments[2]["duration_sec"] == pytest.approx(24 * 4 * 60 / 185, abs=0.01)  # 尾段 29 → 53
    assert abs(sum(s["duration_sec"] for s in segments) - (12 * 4 * 60 / 185 + 17 * 4 * 60 / 142 + 24 * 4 * 60 / 185)) < 0.05


def test_parse_bpm_segments_single_with_reference() -> None:
    text = "#BPM01: 150\n#00008: 01\n#08010: 41\n"
    segments = parse_bpm_segments(text)
    assert segments is not None
    assert len(segments) == 1
    assert segments[0]["bpm"] == 150.0
    assert segments[0]["start_bar"] == 0.0
    assert segments[0]["duration_sec"] == 81 * 4 * 60 / 150


def test_parse_bpm_segments_defaults_to_first_definition_without_reference() -> None:
    segments = parse_bpm_segments("#BPM01: 200\n#BPM02: 100\n")
    assert segments is not None
    assert len(segments) == 1
    assert segments[0]["bpm"] == 200.0


def test_parse_bpm_segments_supports_underscore_and_16th_beat_fraction() -> None:
    text = "#BPM_01: 200\n#BPM_02: 300\n#00008: 0102\n#02020: 41\n"
    segments = parse_bpm_segments(text)
    assert segments is not None
    # bar0 处数据 "0102": beat0 -> BPM01(200), beat 1/2 -> BPM02(300)
    assert segments[0]["bpm"] == 200.0
    assert segments[0]["start_bar"] == 0.0
    assert segments[0]["end_bar"] == 0.5
    assert segments[1]["bpm"] == 300.0
    assert segments[1]["start_bar"] == 0.5
    assert segments[1]["duration_sec"] == pytest.approx(20.5 * 4 * 60 / 300, abs=0.01)


def test_parse_bpm_segments_skips_zero_pairs_and_glide() -> None:
    # 48 风格:同小节渐变(0204)与 00 占位(0006)
    text = (
        "#BPM01: 165\n#BPM02: 150\n#BPM03: 120\n#BPM04: 145\n#BPM06: 110\n"
        "#00008: 01\n#00808: 0204\n#01008: 0006\n#01108: 01\n"
    )
    segments = parse_bpm_segments(text)
    assert segments is not None
    assert [(s["bpm"], s["start_bar"], s["end_bar"]) for s in segments] == [
        (165.0, 0.0, 8.0),
        (150.0, 8.0, 8.5),
        (145.0, 8.5, 10.5),
        (110.0, 10.5, 11.0),
        (165.0, 11.0, 12.0),  # end_bar = 最大小节 11 + 1
    ]


def test_parse_bpm_segments_reads_beats_per_bar() -> None:
    text = "#BPM01: 120\n#00008: 01\n#00002: 3\n#00920: 41\n"
    segments = parse_bpm_segments(text)
    assert segments is not None
    assert segments[0]["duration_sec"] == 10 * 3 * 60 / 120  # 3 拍/小节


def test_parse_bpm_segments_no_bpm_returns_none() -> None:
    assert parse_bpm_segments("") is None
    assert parse_bpm_segments("#TIL00: \"1'0:1\"\n#00002: 4\n#VOLUME: \"\"\n") is None


def test_build_bpm_record_picks_longest_segment() -> None:
    segments = [
        {"bpm": 185.0, "start_bar": 0.0, "end_bar": 12.0, "duration_sec": 15.6},
        {"bpm": 142.0, "start_bar": 12.0, "end_bar": 29.0, "duration_sec": 28.7},
        {"bpm": 185.0, "start_bar": 29.0, "end_bar": 106.0, "duration_sec": 99.9},
    ]
    record = build_bpm_record(segments)
    assert record is not None
    assert record["bpm"] == 185.0
    # 按时间顺序的完整变化值,不去重(185 出现两次)
    assert record["bpms"] == [185.0, 142.0, 185.0]
    assert record["bpm_count"] == 3
    assert record["bpm_segments"] == segments

    assert build_bpm_record([]) is None


def test_parse_music_bpm_ids() -> None:
    assert _parse_music_bpm_ids("1,10-12,5") == [1, 5, 10, 11, 12]
    assert _parse_music_bpm_ids(" 2 , 4 ,2 ") == [2, 4]
    assert _parse_music_bpm_ids("") == []
    with pytest.raises(argparse.ArgumentTypeError):
        _parse_music_bpm_ids("abc")
    with pytest.raises(argparse.ArgumentTypeError):
        _parse_music_bpm_ids("5-2")
    with pytest.raises(argparse.ArgumentTypeError):
        _parse_music_bpm_ids("-3")


def make_musics(ids: list[int]) -> list[dict[str, Any]]:
    return [{"id": music_id, "title": f"song_{music_id}"} for music_id in ids]


def _http_404(url: str) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", url)
    return httpx.HTTPStatusError("404 Not Found", request=request, response=httpx.Response(404))


def chart(bpm01: float, bpm02: float | None = None) -> str:
    lines = [f"#BPM01: {bpm01}"]
    if bpm02 is not None:
        lines.append(f"#BPM02: {bpm02}")
    lines.append("#00008: 01")
    if bpm02 is not None:
        lines.append("#01208: 02")  # bar12 切换:185 段 12 小节 > 142 段 9 小节
    lines.append("#02020: 41")  # 保证 end_bar=21
    return "\n".join(lines) + "\n"


class ServerHarness:
    def __init__(self, tmp_path: Path, monkeypatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch
        self.music_lists = {"jp": [], "cn": [], "tw": [], "en": [], "kr": []}
        self.info_contents: dict[int, str | None] = {}
        self.difficulty_contents: dict[tuple[int, str], str] = {}
        self.requests: list[str] = []

        class DummyClient:
            async def __aenter__(self) -> object:
                return object()

            async def __aexit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001
                return False

        async def fake_get_json(client, url, **kwargs):  # noqa: ANN001
            lang = next(seg for seg in url.split("/") if seg in self.music_lists)
            return self.music_lists[lang]

        async def fake_get_text(client, url, **kwargs):  # noqa: ANN001
            self.requests.append(url)
            music_id = int(url.rsplit("/", 2)[-2].split("_")[0])
            file_name = url.rsplit("/", 1)[-1]
            if file_name == "info.txt":
                content = self.info_contents.get(music_id)
            else:
                difficulty = file_name[:-4]
                content = self.difficulty_contents.get((music_id, difficulty))
            if content is None:
                raise _http_404(url)
            return content

        monkeypatch.setattr(module, "create_async_client", lambda **kwargs: DummyClient())
        monkeypatch.setattr(module, "get_json", fake_get_json)
        monkeypatch.setattr(module, "get_text", fake_get_text)

    def run(self, **kwargs: Any) -> dict[str, int]:
        return asyncio.run(module.update_music_bpm(output_dir=self.tmp_path, **kwargs))

    def songs(self) -> dict[int, dict[str, Any]]:
        payload = json.loads((self.tmp_path / "music_bpms.json").read_text(encoding="utf-8"))
        return {record["music_id"]: record for record in payload["songs"]}


def test_update_music_bpm_priority_dedup_and_output(tmp_path, monkeypatch) -> None:
    h = ServerHarness(tmp_path, monkeypatch)
    h.music_lists = {
        "jp": make_musics([1, 2, 3]),
        "cn": make_musics([2, 4]),
        "tw": make_musics([5]),
        "en": make_musics([1, 6]),
        "kr": make_musics([]),
    }
    h.info_contents = {
        1: chart(150),
        2: None,  # 谱面完全缺失(404)
        3: chart(185, 142),
        4: "",  # 无 BPM
        5: chart(200),
        6: chart(180),
    }

    stats = h.run()

    assert stats["source_jp"] == 3
    assert stats["source_cn"] == 1
    assert stats["source_tw"] == 1
    assert stats["source_en"] == 1
    assert stats["source_kr"] == 0
    assert stats["songs_total"] == 6
    assert stats["fetched"] == 4  # 1/3/5/6 成功
    assert stats["songs_with_bpm"] == 4
    assert stats["missing"] == 2  # id=2(404) 与 id=4(无 BPM)

    songs = h.songs()
    assert songs[1]["source"] == "jp" and songs[1]["bpm"] == 150.0
    assert songs[1]["bpms"] == [150.0]
    assert songs[3]["source"] == "jp" and songs[3]["bpms"] == [185.0, 142.0]
    assert songs[3]["bpm"] == 185.0  # 185 段 12 小节 vs 142 段 9 小节,更长者为主
    assert songs[3]["bpm_count"] == 2
    assert len(songs[3]["bpm_segments"]) == 2
    assert songs[4]["source"] == "cn" and "bpm" not in songs[4]
    assert songs[5]["source"] == "tw" and songs[5]["bpm"] == 200.0
    assert songs[6]["source"] == "en" and songs[6]["bpm"] == 180.0
    assert "prefix" not in songs[1]


def test_update_music_bpm_falls_back_to_difficulty_files(tmp_path, monkeypatch) -> None:
    h = ServerHarness(tmp_path, monkeypatch)
    h.music_lists = {"jp": make_musics([2]), "cn": [], "tw": [], "en": [], "kr": []}
    h.info_contents = {2: None}  # info.txt 缺失
    # master/append/hard/normal 均缺失,由 easy 命中
    h.difficulty_contents = {(2, "easy"): chart(138)}

    stats = h.run()

    assert stats["songs_total"] == 1
    assert stats["songs_with_bpm"] == 1
    assert stats["missing"] == 0
    songs = h.songs()
    assert songs[2]["bpm"] == 138.0
    # 尝试顺序: master -> append -> hard -> normal -> easy,命中后不再请求 info.txt,共 5 次请求
    assert len(h.requests) == 5
    difficulties = [url.rsplit("/", 1)[-1] for url in h.requests]
    assert difficulties == ["master.txt", "append.txt", "hard.txt", "normal.txt", "easy.txt"]


def test_update_music_bpm_prefers_master_over_info(tmp_path, monkeypatch) -> None:
    h = ServerHarness(tmp_path, monkeypatch)
    h.music_lists = {"jp": make_musics([9]), "cn": [], "tw": [], "en": [], "kr": []}
    h.info_contents = {9: chart(150)}
    h.difficulty_contents = {(9, "master"): chart(170)}  # master 优先

    stats = h.run()

    assert stats["songs_with_bpm"] == 1
    songs = h.songs()
    assert songs[9]["bpm"] == 170.0
    assert h.requests[0].endswith("master.txt")


def test_update_music_bpm_incremental_reuses_cache(tmp_path, monkeypatch) -> None:
    h = ServerHarness(tmp_path, monkeypatch)
    h.music_lists = {"jp": make_musics([1, 3]), "cn": [], "tw": [], "en": [], "kr": []}
    h.info_contents = {1: chart(150), 3: chart(185, 142)}

    stats_first = h.run()
    assert stats_first["fetched"] == 2

    request_count_after_first = len(h.requests)
    stats_second = h.run()  # 增量:缓存命中,不再请求

    assert stats_second["reused"] == 2
    assert stats_second["fetched"] == 0
    assert len(h.requests) == request_count_after_first

    # 新增歌曲会被增量抓取
    h.music_lists["jp"].append({"id": 7, "title": "song_7"})
    h.info_contents[7] = chart(160)
    stats_third = h.run()
    assert stats_third["reused"] == 2
    assert stats_third["fetched"] == 1
    assert stats_third["songs_total"] == 3


def test_update_music_bpm_force_refetches_all(tmp_path, monkeypatch) -> None:
    h = ServerHarness(tmp_path, monkeypatch)
    h.music_lists = {"jp": make_musics([1, 3]), "cn": [], "tw": [], "en": [], "kr": []}
    h.info_contents = {1: chart(150), 3: chart(185, 142)}

    h.run()
    request_count_after_first = len(h.requests)
    stats = h.run(force=True)

    assert stats["fetched"] == 2
    assert stats["reused"] == 0
    # 每首歌 5 次难度 404 + 1 次 info 命中 = 6 次请求
    assert len(h.requests) == request_count_after_first + 12


def test_update_music_bpm_ids_force_subset(tmp_path, monkeypatch) -> None:
    h = ServerHarness(tmp_path, monkeypatch)
    h.music_lists = {"jp": make_musics([1, 2, 3]), "cn": [], "tw": [], "en": [], "kr": []}
    h.info_contents = {1: chart(150), 2: chart(140), 3: chart(130)}

    h.run()
    request_count_after_first = len(h.requests)
    stats = h.run(ids=[2, 3])  # 子集强制重抓，其余复用缓存

    assert stats["fetched"] == 2  # 2、3 重抓
    assert stats["reused"] == 1  # 1 复用缓存
    assert stats["songs_total"] == 3  # 输出保持全量
    # 每首歌 5 次难度 404 + 1 次 info 命中 = 6 次请求
    assert len(h.requests) == request_count_after_first + 12
    assert set(h.songs()) == {1, 2, 3}
    assert h.songs()[1]["bpm"] == 150.0  # 缓存值未被丢弃


def test_update_music_bpm_ids_no_match_raises(tmp_path, monkeypatch) -> None:
    h = ServerHarness(tmp_path, monkeypatch)
    h.music_lists = {"jp": make_musics([1]), "cn": [], "tw": [], "en": [], "kr": []}
    h.info_contents = {1: chart(150)}

    with pytest.raises(ValueError, match="exist in the merged server lists"):
        h.run(ids=[999])
