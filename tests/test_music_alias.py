import asyncio
import json
from pathlib import Path

from src.common.io import write_json
from src.tasks import music_alias as module
from src.tasks.music_alias import build_output_musics, parse_alias_payload


def test_parse_alias_payload_valid_and_deduplicate() -> None:
    payload = {
        "music_id": 1,
        "aliases": ["tyw", "tell your world", "tyw", "  ", "Tell Your World"],
    }
    assert parse_alias_payload(payload) == ["tyw", "tell your world", "Tell Your World"]


def test_parse_alias_payload_empty_aliases() -> None:
    payload = {
        "music_id": 1000,
        "aliases": [],
    }
    assert parse_alias_payload(payload) == []


def test_parse_alias_payload_invalid_json_shapes() -> None:
    assert parse_alias_payload({"music_id": 1}) == []
    assert parse_alias_payload({"music_id": 1, "aliases": "tyw"}) == []
    assert parse_alias_payload(["not-a-dict"]) == []


def test_build_output_musics_falls_back_to_previous_aliases() -> None:
    records = [(1, "A"), (2, "B")]
    fetched_alias_map = {1: None, 2: ["b1"]}
    previous_alias_map = {1: ["a1"]}

    rows, failed = build_output_musics(records, fetched_alias_map, previous_alias_map)

    assert failed == 1
    assert rows == [
        {"music_id": 1, "title": "A", "aliases": ["a1"]},
        {"music_id": 2, "title": "B", "aliases": ["b1"]},
    ]


def test_update_music_aliases_probe_failure_still_writes_file(tmp_path, monkeypatch) -> None:
    class DummyClient:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    output_path = Path(tmp_path) / "music_aliases.json"
    write_json(
        output_path,
        {
            "generated_at": "2026-01-01T00:00:00Z",
            "source": {},
            "musics": [{"music_id": 1, "title": "Old 1", "aliases": ["old"]}],
        },
    )

    async def fake_get_json(client, url, **kwargs):  # noqa: ANN001
        if url == module.MUSICS_SOURCE_URL:
            return [{"id": 1, "title": "Song 1"}, {"id": 2, "title": "Song 2"}]
        raise ValueError("simulated API block")

    monkeypatch.setattr(module, "create_async_client", lambda **kwargs: DummyClient())
    monkeypatch.setattr(module, "get_json", fake_get_json)

    stats = asyncio.run(module.update_music_aliases(output_path=output_path))
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    musics = payload["musics"]

    assert stats["used_cached_data"] == 1
    assert stats["musics_total"] == 2
    assert len(musics) == 2
    assert musics[0]["music_id"] == 1 and musics[0]["aliases"] == ["old"]
    assert musics[1]["music_id"] == 2 and musics[1]["aliases"] == []
