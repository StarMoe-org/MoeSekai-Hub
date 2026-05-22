from src.tasks.event_bvid import WikiEventEntry, build_cached_event_payloads, extract_bvid, match_event_name, normalize_event_name


def test_normalize_event_name_symbol_variants() -> None:
    left = "Legend still Vivid"
    right = "legend still vivid"
    assert normalize_event_name(left) == normalize_event_name(right)


def test_normalize_event_name_whitespace_and_symbols() -> None:
    left = "交わる旋律、灯るぬくもり"
    right = "交わる旋律 灯るぬくもり"
    assert normalize_event_name(left) == normalize_event_name(right)


def test_extract_bvid_from_standard_url() -> None:
    url = "https://www.bilibili.com/video/BV17a411A72N"
    assert extract_bvid(url) == "BV17a411A72N"


def test_extract_bvid_from_b23_style_url_with_bv_path() -> None:
    url = "https://b23.tv/BV1ut4y1e7Aa"
    assert extract_bvid(url) == "BV1ut4y1e7Aa"


def test_extract_bvid_invalid_url_returns_none() -> None:
    url = "https://example.com/video/av123456"
    assert extract_bvid(url) is None


def test_match_event_name_uses_exact_then_normalized() -> None:
    exact_map = {
        "交わる旋律 灯るぬくもり": WikiEventEntry(
            translate="交织旋律，点亮温暖",
            original="交わる旋律 灯るぬくもり",
            bilibili_url="https://www.bilibili.com/video/BV1abcde1234",
            bvid="BV1abcde1234",
        )
    }
    normalized_map = {
        normalize_event_name("交わる旋律 灯るぬくもり"): WikiEventEntry(
            translate="交织旋律，点亮温暖",
            original="交わる旋律 灯るぬくもり",
            bilibili_url="https://www.bilibili.com/video/BV1abcde1234",
            bvid="BV1abcde1234",
        )
    }

    exact_entry, exact_status = match_event_name("交わる旋律 灯るぬくもり", exact_map, normalized_map)
    assert exact_entry is not None
    assert exact_status == "exact"

    normalized_entry, normalized_status = match_event_name("交わる旋律、灯るぬくもり", exact_map, normalized_map)
    assert normalized_entry is not None
    assert normalized_status == "normalized"


def test_build_cached_event_payloads_reuses_existing_bvids_and_marks_new_events_unmatched() -> None:
    events = [
        {"id": 2, "name": "囚われのマリオネット"},
        {"id": 1, "name": "雨上がりの一番星"},
        {"id": 3, "name": "新しいイベント"},
    ]
    cached_payload = {
        "events": [
            {
                "event_id": 1,
                "event_name": "雨上がりの一番星",
                "bilibili_url": "https://www.bilibili.com/video/BV17a411A72N",
                "bvid": "BV17a411A72N",
                "match_status": "exact",
            },
            {
                "event_id": 2,
                "event_name": "囚われのマリオネット",
                "bilibili_url": "https://www.bilibili.com/video/BV1ut4y1e7Aa",
                "bvid": "BV1ut4y1e7Aa",
                "match_status": "normalized",
            },
        ]
    }

    main_payload, unmatched_payload = build_cached_event_payloads(events, cached_payload)

    assert [event["event_id"] for event in main_payload["events"]] == [1, 2, 3]
    assert main_payload["events"][0] == {
        "event_id": 1,
        "event_name": "雨上がりの一番星",
        "bilibili_url": "https://www.bilibili.com/video/BV17a411A72N",
        "bvid": "BV17a411A72N",
        "match_status": "exact",
    }
    assert main_payload["events"][1]["bvid"] == "BV1ut4y1e7Aa"
    assert main_payload["events"][1]["match_status"] == "normalized"
    assert main_payload["events"][2] == {
        "event_id": 3,
        "event_name": "新しいイベント",
        "bilibili_url": None,
        "bvid": None,
        "match_status": "unmatched",
    }
    assert unmatched_payload["unmatched_events"] == [{"event_id": 3, "event_name": "新しいイベント"}]

