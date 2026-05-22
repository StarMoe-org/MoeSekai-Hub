from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from src.common.io import write_json
from src.tasks import bgm_duration as module
from src.tasks.bgm_duration import (
    AudioDuration,
    BgmObject,
    build_cached_track_record,
    load_previous_track_map,
    parse_bucket_index_xml,
    parse_mp3_duration,
)


def _xing_mp3_header(*, frames: int = 1839, sample_rate_index: int = 0) -> bytes:
    # ID3v2 header with a tiny 35-byte tag, followed by an MPEG1 Layer III 320kbps frame.
    id3 = b"ID3\x04\x00\x00\x00\x00\x00#" + (b"\x00" * 35)
    frame = bytearray(256)
    frame[0:4] = b"\xff\xfb\xe0\x00"
    xing_offset = 4 + 32
    frame[xing_offset : xing_offset + 4] = b"Info"
    frame[xing_offset + 4 : xing_offset + 8] = (0x01).to_bytes(4, "big")
    frame[xing_offset + 8 : xing_offset + 12] = frames.to_bytes(4, "big")
    if sample_rate_index:
        header = int.from_bytes(frame[0:4], "big")
        header = (header & ~(0x03 << 10)) | (sample_rate_index << 10)
        frame[0:4] = header.to_bytes(4, "big")
    return id3 + bytes(frame)


def _cbr_mp3_header(*, bitrate_index: int = 9) -> bytes:
    # MPEG1 Layer III, default bitrate index 9 = 128kbps, 44.1kHz.
    header = 0
    header |= 0x7FF << 21
    header |= 0x03 << 19
    header |= 0x01 << 17
    header |= 0x01 << 16
    header |= bitrate_index << 12
    header |= 0x00 << 10
    return header.to_bytes(4, "big") + (b"\x00" * 256)


def test_parse_bucket_index_xml_collects_mp3_and_next_marker() -> None:
    xml_text = """<?xml version="1.0" encoding="UTF-8"?>
<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <Name>sekai-jp-assets</Name>
  <Prefix>sound/scenario/bgm/</Prefix>
  <NextMarker>sound/scenario/bgm/next</NextMarker>
  <IsTruncated>true</IsTruncated>
  <Contents>
    <Key>sound/scenario/bgm/bgm00024/bgm00024.mp3</Key>
    <ETag>&#34;abc123&#34;</ETag>
    <Size>1922656</Size>
    <LastModified>2026-04-28T13:09:24Z</LastModified>
  </Contents>
  <Contents>
    <Key>sound/scenario/bgm/bgm00024/soundbundlebuilddata.json</Key>
    <ETag>&#34;ignored&#34;</ETag>
    <Size>398</Size>
    <LastModified>2026-04-28T13:09:24Z</LastModified>
  </Contents>
</ListBucketResult>"""

    objects, marker = parse_bucket_index_xml(xml_text, "sound/scenario/bgm/")

    assert marker == "sound/scenario/bgm/next"
    assert objects == [
        BgmObject(
            key="sound/scenario/bgm/bgm00024/bgm00024.mp3",
            route="sound/scenario/bgm/",
            size=1_922_656,
            etag="abc123",
            last_modified="2026-04-28T13:09:24Z",
        )
    ]


def test_parse_bucket_index_xml_falls_back_to_last_key_marker() -> None:
    xml_text = """<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
  <IsTruncated>true</IsTruncated>
  <Contents><Key>mysekai/sound/bgm/music0001/music0001.mp3</Key><Size>1</Size></Contents>
</ListBucketResult>"""

    objects, marker = parse_bucket_index_xml(xml_text, "mysekai/sound/bgm/")

    assert len(objects) == 1
    assert marker == "mysekai/sound/bgm/music0001/music0001.mp3"


def test_parse_mp3_duration_from_xing_info_frame() -> None:
    duration = parse_mp3_duration(_xing_mp3_header(frames=1839), object_size=1_922_656)

    assert duration.source == "info"
    assert duration.seconds == pytest.approx(48.039, abs=0.001)


def test_parse_mp3_duration_uses_cbr_estimate_when_no_vbr_tag() -> None:
    duration = parse_mp3_duration(_cbr_mp3_header(bitrate_index=9), object_size=1_280_004)

    assert duration.source == "cbr_estimate"
    assert duration.seconds == pytest.approx(80.0, abs=0.001)


def test_load_previous_track_map_keeps_only_valid_durations(tmp_path: Path) -> None:
    output_path = tmp_path / "bgm_durations.json"
    write_json(
        output_path,
        {
            "tracks": [
                {"key": "ok.mp3", "duration_seconds": 1.23},
                {"key": "bad.mp3", "duration_seconds": 0},
                {"key": 123, "duration_seconds": 4.56},
            ]
        },
    )

    result = load_previous_track_map(output_path)

    assert list(result) == ["ok.mp3"]


def test_build_cached_track_record_refreshes_object_metadata() -> None:
    obj = BgmObject(
        key="sound/scenario/bgm/new/new.mp3",
        route="sound/scenario/bgm/",
        size=456,
        etag="new-etag",
        last_modified="2026-05-01T00:00:00Z",
    )
    previous = {
        "key": obj.key,
        "route": "old-route",
        "file_name": "old.mp3",
        "size": 123,
        "etag": "old-etag",
        "last_modified": "2026-04-01T00:00:00Z",
        "duration_seconds": 12.345,
        "duration_source": "info",
        "duration_fetched_at": "2026-04-02T00:00:00Z",
    }

    record = build_cached_track_record(obj, previous)

    assert record["size"] == 456
    assert record["etag"] == "new-etag"
    assert record["duration_seconds"] == 12.345
    assert record["duration_milliseconds"] == 12_345
    assert record["duration_fetched_at"] == "2026-04-02T00:00:00Z"


class DummyClient:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return False


def test_update_bgm_duration_fetches_only_missing_tracks(tmp_path: Path, monkeypatch) -> None:
    output_path = tmp_path / "bgm_durations.json"
    cached_key = "sound/scenario/bgm/cached/cached.mp3"
    missing_key = "mysekai/sound/bgm/missing/missing.mp3"
    write_json(
        output_path,
        {
            "generated_at": "2026-01-01T00:00:00Z",
            "tracks": [
                {
                    "key": cached_key,
                    "route": "sound/scenario/bgm/",
                    "file_name": "cached.mp3",
                    "size": 100,
                    "etag": "old",
                    "last_modified": "2026-01-01T00:00:00Z",
                    "duration_seconds": 10.0,
                    "duration_milliseconds": 10000,
                    "duration_source": "info",
                    "duration_fetched_at": "2026-01-01T00:00:00Z",
                }
            ],
        },
    )

    objects = [
        BgmObject(
            key=cached_key,
            route="sound/scenario/bgm/",
            size=111,
            etag="cached-new",
            last_modified="2026-05-01T00:00:00Z",
        ),
        BgmObject(
            key=missing_key,
            route="mysekai/sound/bgm/",
            size=222,
            etag="missing-etag",
            last_modified="2026-05-02T00:00:00Z",
        ),
    ]
    fetched_keys: list[str] = []

    async def fake_fetch_index_objects(client: object, prefixes: Any) -> tuple[list[BgmObject], int]:
        return objects, 2

    async def fake_fetch_duration_for_object(
        client: object,
        semaphore: asyncio.Semaphore,
        obj: BgmObject,
        *,
        fetched_at: str,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        fetched_keys.append(obj.key)
        return (
            module.build_track_record(
                obj,
                AudioDuration(seconds=20.5, source="info"),
                fetched_at=fetched_at,
            ),
            None,
        )

    monkeypatch.setattr(module, "create_async_client", lambda **kwargs: DummyClient())
    monkeypatch.setattr(module, "fetch_index_objects", fake_fetch_index_objects)
    monkeypatch.setattr(module, "_fetch_duration_for_object", fake_fetch_duration_for_object)
    monkeypatch.setattr(module, "utc_now_iso", lambda: "2026-05-22T00:00:00Z")

    stats = asyncio.run(module.update_bgm_duration(output_path=output_path))
    payload = json.loads(output_path.read_text(encoding="utf-8"))

    assert fetched_keys == [missing_key]
    assert stats == {
        "index_pages": 2,
        "indexed_total": 2,
        "cached_durations": 1,
        "fetched_durations": 1,
        "fetch_failed": 0,
        "recorded_total": 2,
        "output_changed": 1,
    }
    tracks_by_key = {track["key"]: track for track in payload["tracks"]}
    assert set(tracks_by_key) == {cached_key, missing_key}
    assert tracks_by_key[cached_key]["etag"] == "cached-new"
    assert tracks_by_key[cached_key]["duration_seconds"] == 10.0
    assert tracks_by_key[missing_key]["duration_seconds"] == 20.5


def test_update_bgm_duration_keeps_file_unchanged_when_everything_cached(tmp_path: Path, monkeypatch) -> None:
    output_path = tmp_path / "bgm_durations.json"
    cached_key = "sound/scenario/bgm/cached/cached.mp3"
    previous_payload = {
        "generated_at": "2026-01-01T00:00:00Z",
        "tracks": [
            {
                "key": cached_key,
                "route": "sound/scenario/bgm/",
                "file_name": "cached.mp3",
                "size": 100,
                "etag": "same-etag",
                "last_modified": "2026-01-01T00:00:00Z",
                "duration_seconds": 10.0,
                "duration_milliseconds": 10000,
                "duration_source": "info",
                "duration_fetched_at": "2026-01-01T00:00:00Z",
            }
        ],
    }
    write_json(output_path, previous_payload)
    before = output_path.read_text(encoding="utf-8")

    objects = [
        BgmObject(
            key=cached_key,
            route="sound/scenario/bgm/",
            size=100,
            etag="same-etag",
            last_modified="2026-01-01T00:00:00Z",
        )
    ]

    async def fake_fetch_index_objects(client: object, prefixes: Any) -> tuple[list[BgmObject], int]:
        return objects, 1

    async def fake_fetch_duration_for_object(
        client: object,
        semaphore: asyncio.Semaphore,
        obj: BgmObject,
        *,
        fetched_at: str,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        raise AssertionError("cached tracks must not fetch MP3 headers")

    monkeypatch.setattr(module, "create_async_client", lambda **kwargs: DummyClient())
    monkeypatch.setattr(module, "fetch_index_objects", fake_fetch_index_objects)
    monkeypatch.setattr(module, "_fetch_duration_for_object", fake_fetch_duration_for_object)
    monkeypatch.setattr(module, "utc_now_iso", lambda: "2026-05-22T00:00:00Z")

    stats = asyncio.run(module.update_bgm_duration(output_path=output_path))

    assert stats["cached_durations"] == 1
    assert stats["fetched_durations"] == 0
    assert stats["output_changed"] == 0
    assert output_path.read_text(encoding="utf-8") == before
