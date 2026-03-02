from src.tasks.event_bvid import WikiEventEntry, extract_bvid, match_event_name, normalize_event_name


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

