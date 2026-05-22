from __future__ import annotations

import asyncio
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from src.common.http import RETRYABLE_STATUS_CODES, RetryConfig, create_async_client
from src.common.io import read_json, utc_now_iso, write_json

INDEX_BASE_URL = "https://storage2.pjsk.moe/sekai-jp-assets/"
ASSET_BASE_URL = "https://storage.pjsk.moe/sekai-jp-assets/"
BGM_PREFIXES = ("sound/scenario/bgm/", "mysekai/sound/bgm/")
OUTPUT_PATH = Path("data/bgm_duration/bgm_durations.json")
FETCH_CONCURRENCY = 3
MP3_HEADER_BYTES = 64 * 1024
MP3_RETRY_CONFIG = RetryConfig(attempts=4, backoff_base_seconds=0.5, backoff_jitter_seconds=0.0)
S3_XML_NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}


@dataclass(frozen=True, slots=True)
class BgmObject:
    key: str
    route: str
    size: int
    etag: str
    last_modified: str


@dataclass(frozen=True, slots=True)
class AudioDuration:
    seconds: float
    source: str


@dataclass(frozen=True, slots=True)
class Mp3FrameHeader:
    offset: int
    version_id: int
    layer_id: int
    bitrate_kbps: int
    sample_rate: int
    channel_mode: int
    samples_per_frame: int


BITRATE_TABLE: dict[tuple[int, int], list[int | None]] = {
    # version_id 3 = MPEG 1, 2 = MPEG 2, 0 = MPEG 2.5
    # layer_id 3 = Layer I, 2 = Layer II, 1 = Layer III
    (3, 3): [None, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448, None],
    (3, 2): [None, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384, None],
    (3, 1): [None, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, None],
    (2, 3): [None, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256, None],
    (2, 2): [None, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, None],
    (2, 1): [None, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, None],
    (0, 3): [None, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256, None],
    (0, 2): [None, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, None],
    (0, 1): [None, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, None],
}

SAMPLE_RATE_TABLE: dict[int, list[int]] = {
    3: [44100, 48000, 32000],
    2: [22050, 24000, 16000],
    0: [11025, 12000, 8000],
}


def _asset_url(key: str) -> str:
    return f"{ASSET_BASE_URL}{urllib.parse.quote(key, safe='/')}"


def _index_url(prefix: str, marker: str | None = None) -> str:
    query: dict[str, str] = {"prefix": prefix}
    if marker:
        query["marker"] = marker
    return f"{INDEX_BASE_URL}?{urllib.parse.urlencode(query)}"


def _strip_etag(value: str | None) -> str:
    if value is None:
        return ""
    return value.strip().strip('"')


def parse_bucket_index_xml(xml_text: str, route: str) -> tuple[list[BgmObject], str | None]:
    root = ET.fromstring(xml_text)
    objects: list[BgmObject] = []

    for contents in root.findall("s3:Contents", S3_XML_NS):
        key = contents.findtext("s3:Key", default="", namespaces=S3_XML_NS)
        if not key.endswith(".mp3"):
            continue
        size_text = contents.findtext("s3:Size", default="0", namespaces=S3_XML_NS)
        try:
            size = int(size_text)
        except ValueError:
            size = 0
        objects.append(
            BgmObject(
                key=key,
                route=route,
                size=size,
                etag=_strip_etag(contents.findtext("s3:ETag", default="", namespaces=S3_XML_NS)),
                last_modified=contents.findtext("s3:LastModified", default="", namespaces=S3_XML_NS),
            )
        )

    is_truncated = root.findtext("s3:IsTruncated", default="false", namespaces=S3_XML_NS).lower() == "true"
    if not is_truncated:
        return objects, None

    next_marker = root.findtext("s3:NextMarker", default="", namespaces=S3_XML_NS)
    if next_marker:
        return objects, next_marker
    if objects:
        return objects, objects[-1].key
    return objects, None


async def fetch_index_objects(
    client: httpx.AsyncClient,
    prefixes: Iterable[str] = BGM_PREFIXES,
) -> tuple[list[BgmObject], int]:
    objects_by_key: dict[str, BgmObject] = {}
    page_count = 0

    for prefix in prefixes:
        marker: str | None = None
        while True:
            response = await client.get(_index_url(prefix, marker))
            response.raise_for_status()
            page_count += 1
            page_objects, marker = parse_bucket_index_xml(response.text, prefix)
            for obj in page_objects:
                objects_by_key[obj.key] = obj
            if marker is None:
                break

    return sorted(objects_by_key.values(), key=lambda item: item.key), page_count


def _skip_id3v2(data: bytes) -> int:
    if len(data) < 10 or data[:3] != b"ID3":
        return 0
    tag_size = (data[6] & 0x7F) << 21 | (data[7] & 0x7F) << 14 | (data[8] & 0x7F) << 7 | (data[9] & 0x7F)
    footer_size = 10 if data[5] & 0x10 else 0
    return min(len(data), 10 + tag_size + footer_size)


def _samples_per_frame(version_id: int, layer_id: int) -> int:
    if layer_id == 3:
        return 384
    if layer_id == 2:
        return 1152
    if version_id == 3:
        return 1152
    return 576


def _parse_mp3_frame_header(data: bytes, offset: int) -> Mp3FrameHeader | None:
    if offset + 4 > len(data):
        return None
    header = int.from_bytes(data[offset : offset + 4], "big")
    if (header >> 21) & 0x7FF != 0x7FF:
        return None

    version_id = (header >> 19) & 0x03
    layer_id = (header >> 17) & 0x03
    bitrate_index = (header >> 12) & 0x0F
    sample_rate_index = (header >> 10) & 0x03
    channel_mode = (header >> 6) & 0x03

    if version_id == 1 or layer_id == 0 or sample_rate_index == 3:
        return None

    bitrate = BITRATE_TABLE.get((version_id, layer_id), [None] * 16)[bitrate_index]
    sample_rates = SAMPLE_RATE_TABLE.get(version_id)
    if bitrate is None or sample_rates is None:
        return None

    return Mp3FrameHeader(
        offset=offset,
        version_id=version_id,
        layer_id=layer_id,
        bitrate_kbps=bitrate,
        sample_rate=sample_rates[sample_rate_index],
        channel_mode=channel_mode,
        samples_per_frame=_samples_per_frame(version_id, layer_id),
    )


def _find_first_mp3_frame(data: bytes) -> Mp3FrameHeader | None:
    start = _skip_id3v2(data)
    for offset in range(start, max(start, len(data) - 3)):
        frame_header = _parse_mp3_frame_header(data, offset)
        if frame_header is not None:
            return frame_header
    return None


def _xing_offset(frame_header: Mp3FrameHeader) -> int | None:
    if frame_header.layer_id != 1:
        return None
    if frame_header.version_id == 3:
        side_info_size = 17 if frame_header.channel_mode == 3 else 32
    else:
        side_info_size = 9 if frame_header.channel_mode == 3 else 17
    return frame_header.offset + 4 + side_info_size


def _parse_xing_duration(data: bytes, frame_header: Mp3FrameHeader) -> AudioDuration | None:
    offset = _xing_offset(frame_header)
    if offset is None or offset + 16 > len(data):
        return None
    tag = data[offset : offset + 4]
    if tag not in {b"Xing", b"Info"}:
        return None
    flags = int.from_bytes(data[offset + 4 : offset + 8], "big")
    if not flags & 0x01:
        return None
    frames = int.from_bytes(data[offset + 8 : offset + 12], "big")
    if frames <= 0:
        return None
    seconds = frames * frame_header.samples_per_frame / frame_header.sample_rate
    return AudioDuration(seconds=seconds, source=tag.decode("ascii").lower())


def _parse_vbri_duration(data: bytes, frame_header: Mp3FrameHeader) -> AudioDuration | None:
    offset = frame_header.offset + 4 + 32
    if offset + 18 > len(data) or data[offset : offset + 4] != b"VBRI":
        return None
    frames = int.from_bytes(data[offset + 14 : offset + 18], "big")
    if frames <= 0:
        return None
    seconds = frames * frame_header.samples_per_frame / frame_header.sample_rate
    return AudioDuration(seconds=seconds, source="vbri")


def parse_mp3_duration(data: bytes, object_size: int | None = None) -> AudioDuration:
    frame_header = _find_first_mp3_frame(data)
    if frame_header is None:
        raise ValueError("No MP3 frame header found")

    duration = _parse_xing_duration(data, frame_header)
    if duration is not None:
        return duration

    duration = _parse_vbri_duration(data, frame_header)
    if duration is not None:
        return duration

    if object_size is None or object_size <= frame_header.offset:
        raise ValueError("No VBR duration metadata found and object size is unavailable")

    audio_bytes = object_size - frame_header.offset
    seconds = audio_bytes * 8 / (frame_header.bitrate_kbps * 1000)
    return AudioDuration(seconds=seconds, source="cbr_estimate")


def _is_valid_duration(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def load_previous_track_map(path: Path) -> dict[str, dict[str, Any]]:
    payload = read_json(path, default={})
    if not isinstance(payload, dict):
        return {}
    tracks = payload.get("tracks")
    if not isinstance(tracks, list):
        return {}

    result: dict[str, dict[str, Any]] = {}
    for track in tracks:
        if not isinstance(track, dict):
            continue
        key = track.get("key")
        if isinstance(key, str) and _is_valid_duration(track.get("duration_seconds")):
            result[key] = track
    return result


def build_track_record(
    obj: BgmObject,
    duration: AudioDuration,
    *,
    fetched_at: str,
) -> dict[str, Any]:
    duration_seconds = round(duration.seconds, 3)
    return {
        "key": obj.key,
        "route": obj.route,
        "file_name": obj.key.rsplit("/", 1)[-1],
        "size": obj.size,
        "etag": obj.etag,
        "last_modified": obj.last_modified,
        "duration_seconds": duration_seconds,
        "duration_milliseconds": round(duration.seconds * 1000),
        "duration_source": duration.source,
        "duration_fetched_at": fetched_at,
    }


def build_cached_track_record(obj: BgmObject, previous: dict[str, Any]) -> dict[str, Any]:
    duration_seconds = previous["duration_seconds"]
    duration_milliseconds = previous.get("duration_milliseconds")
    if not isinstance(duration_milliseconds, int):
        duration_milliseconds = round(float(duration_seconds) * 1000)

    return {
        "key": obj.key,
        "route": obj.route,
        "file_name": obj.key.rsplit("/", 1)[-1],
        "size": obj.size,
        "etag": obj.etag,
        "last_modified": obj.last_modified,
        "duration_seconds": duration_seconds,
        "duration_milliseconds": duration_milliseconds,
        "duration_source": previous.get("duration_source", "cached"),
        "duration_fetched_at": previous.get("duration_fetched_at", ""),
    }


async def _fetch_mp3_header(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_bytes: int = MP3_HEADER_BYTES,
    retry_config: RetryConfig = MP3_RETRY_CONFIG,
) -> bytes:
    last_error: Exception | None = None
    headers = {"Range": f"bytes=0-{max_bytes - 1}"}

    for attempt in range(1, retry_config.attempts + 1):
        try:
            async with client.stream("GET", url, headers=headers) as response:
                if response.status_code in RETRYABLE_STATUS_CODES and attempt < retry_config.attempts:
                    await asyncio.sleep(retry_config.backoff_base_seconds * (2 ** (attempt - 1)))
                    continue
                response.raise_for_status()
                buffer = bytearray()
                async for chunk in response.aiter_bytes():
                    remaining = max_bytes - len(buffer)
                    if remaining <= 0:
                        break
                    buffer.extend(chunk[:remaining])
                    if len(buffer) >= max_bytes:
                        break
                if not buffer:
                    raise ValueError("Empty MP3 response")
                return bytes(buffer)
        except (httpx.HTTPError, ValueError) as exc:
            last_error = exc
            if attempt >= retry_config.attempts:
                raise
            await asyncio.sleep(retry_config.backoff_base_seconds * (2 ** (attempt - 1)))

    if last_error is not None:
        raise last_error
    raise RuntimeError("Retry loop exited unexpectedly")


async def _fetch_duration_for_object(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    obj: BgmObject,
    *,
    fetched_at: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    async with semaphore:
        try:
            data = await _fetch_mp3_header(client, _asset_url(obj.key))
            duration = parse_mp3_duration(data, obj.size)
            return build_track_record(obj, duration, fetched_at=fetched_at), None
        except Exception as exc:
            return None, {
                "key": obj.key,
                "route": obj.route,
                "error": f"{type(exc).__name__}: {exc}",
            }


def build_payload(
    objects: list[BgmObject],
    tracks: list[dict[str, Any]],
    failures: list[dict[str, Any]],
    *,
    generated_at: str,
) -> dict[str, Any]:
    return {
        "generated_at": generated_at,
        "source": {
            "index_base_url": INDEX_BASE_URL,
            "asset_base_url": ASSET_BASE_URL,
            "prefixes": list(BGM_PREFIXES),
        },
        "total_indexed": len(objects),
        "total_recorded": len(tracks),
        "tracks": tracks,
        "failures": failures,
    }


async def update_bgm_duration(
    output_path: Path = OUTPUT_PATH,
    *,
    prefixes: Iterable[str] = BGM_PREFIXES,
) -> dict[str, int]:
    previous_track_map = load_previous_track_map(output_path)
    generated_at = utc_now_iso()

    async with create_async_client(headers={"Accept": "application/xml,text/xml,*/*"}, timeout_seconds=30.0) as client:
        objects, index_pages = await fetch_index_objects(client, prefixes)
        cached_tracks: dict[str, dict[str, Any]] = {}
        to_fetch: list[BgmObject] = []
        for obj in objects:
            previous = previous_track_map.get(obj.key)
            if previous is None:
                to_fetch.append(obj)
            else:
                cached_tracks[obj.key] = build_cached_track_record(obj, previous)

        fetched_tracks: dict[str, dict[str, Any]] = {}
        failures: list[dict[str, Any]] = []
        if to_fetch:
            semaphore = asyncio.Semaphore(FETCH_CONCURRENCY)
            tasks = [
                _fetch_duration_for_object(client, semaphore, obj, fetched_at=generated_at)
                for obj in to_fetch
            ]
            results = await asyncio.gather(*tasks)
            for track, failure in results:
                if track is not None:
                    fetched_tracks[track["key"]] = track
                if failure is not None:
                    failures.append(failure)

    tracks: list[dict[str, Any]] = []
    for obj in objects:
        track = cached_tracks.get(obj.key) or fetched_tracks.get(obj.key)
        if track is not None:
            tracks.append(track)

    tracks.sort(key=lambda item: item["key"])
    failures.sort(key=lambda item: item["key"])
    current_track_keys = {track["key"] for track in tracks}
    output_changed = (
        not output_path.exists()
        or len(fetched_tracks) > 0
        or len(failures) > 0
        or current_track_keys != set(previous_track_map)
    )
    if output_changed:
        write_json(output_path, build_payload(objects, tracks, failures, generated_at=generated_at))

    return {
        "index_pages": index_pages,
        "indexed_total": len(objects),
        "cached_durations": len(cached_tracks),
        "fetched_durations": len(fetched_tracks),
        "fetch_failed": len(failures),
        "recorded_total": len(tracks),
        "output_changed": int(output_changed),
    }
