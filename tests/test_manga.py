from src.tasks.manga import build_metadata_to_persist


def test_build_metadata_to_persist_keeps_old_url_when_changed_url_download_failed() -> None:
    previous_metadata = {
        "10": {
            "id": 10,
            "title": "旧标题",
            "manga": "https://example.com/old.png",
            "date": 1,
            "url": "https://example.com/opus/10",
            "contributors": {},
        }
    }
    latest_metadata = {
        "10": {
            "id": 10,
            "title": "新标题",
            "manga": "https://example.com/new.png",
            "date": 2,
            "url": "https://example.com/opus/10-new",
            "contributors": {"翻译": "A"},
        }
    }

    persisted = build_metadata_to_persist(latest_metadata, previous_metadata, {10})

    assert persisted["10"]["manga"] == "https://example.com/old.png"
    assert persisted["10"]["title"] == "新标题"
    assert persisted["10"]["date"] == 2


def test_build_metadata_to_persist_uses_latest_when_not_failed() -> None:
    previous_metadata = {
        "10": {"id": 10, "manga": "https://example.com/old.png"},
    }
    latest_metadata = {
        "10": {"id": 10, "manga": "https://example.com/new.png"},
    }

    persisted = build_metadata_to_persist(latest_metadata, previous_metadata, set())
    assert persisted["10"]["manga"] == "https://example.com/new.png"

