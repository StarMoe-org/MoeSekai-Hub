import math
from pathlib import Path

from src.tasks.story_asset import (
    _asset_url,
    _collect_card_urls,
    _collect_event_urls,
    _collect_self_urls,
    _collect_special_urls,
    _collect_talk_urls,
    _collect_unit_urls,
    _extract_asset_path,
    _load_urls,
    _url_to_local_path,
)


def _tpl(asset_type: str, src: str = "sekai.best") -> str:
    urls = _load_urls()
    return _asset_url(urls, src, "jp", asset_type)


def _sekai_best_extractor() -> tuple[str, int]:
    urls = _load_urls()
    return urls["sekai.best"]["path_extractor"], urls["sekai.best"]["path_extractor_group"]


def _haruki_extractor() -> tuple[str, int]:
    urls = _load_urls()
    return urls["haruki"]["path_extractor"], urls["haruki"]["path_extractor_group"]


def test_extract_asset_path_sekai_best() -> None:
    url = "https://storage.sekai.best/sekai-jp-assets/event_story/ev_01/scenario/ev_01_01.asset"
    pattern, group = _sekai_best_extractor()
    result = _extract_asset_path(url, pattern, group)
    assert result == "event_story/ev_01/scenario/ev_01_01.asset"


def test_extract_asset_path_haruki_ondemand() -> None:
    url = "https://sekai-assets-bdf29c81.seiunx.net/jp-assets/ondemand/event_story/ev_01/scenario/ev_01_01.asset"
    pattern, group = _haruki_extractor()
    result = _extract_asset_path(url, pattern, group)
    assert result == "event_story/ev_01/scenario/ev_01_01.asset"


def test_extract_asset_path_haruki_startapp() -> None:
    url = "https://sekai-assets-bdf29c81.seiunx.net/jp-assets/startapp/scenario/unitstory/unit_01/unit_01_01.asset"
    pattern, group = _haruki_extractor()
    result = _extract_asset_path(url, pattern, group)
    assert result == "scenario/unitstory/unit_01/unit_01_01.asset"


def test_url_to_local_path_unified() -> None:
    """Both sources should map the same logical asset to the same local path."""
    sekai_url = "https://storage.sekai.best/sekai-jp-assets/event_story/ev_01/scenario/ev_01_01.asset"
    haruki_url = "https://sekai-assets-bdf29c81.seiunx.net/jp-assets/ondemand/event_story/ev_01/scenario/ev_01_01.asset"
    out = Path("story_assets")

    sp, sg = _sekai_best_extractor()
    hp, hg = _haruki_extractor()

    sekai_path = _url_to_local_path(sekai_url, out, sp, sg, "jp")
    haruki_path = _url_to_local_path(haruki_url, out, hp, hg, "jp")
    assert sekai_path == haruki_path
    assert sekai_path == Path("story_assets/pjsk-jp-assets/event_story/ev_01/scenario/ev_01_01.asset.br")


def test_url_to_local_path_lang_separation() -> None:
    """jp and cn assets should land in different subdirectories."""
    url = "https://storage.sekai.best/sekai-jp-assets/event_story/ev_01/scenario/ev_01_01.asset"
    sp, sg = _sekai_best_extractor()
    jp_path = _url_to_local_path(url, Path("story_assets"), sp, sg, "jp")
    cn_path = _url_to_local_path(url, Path("story_assets"), sp, sg, "cn")
    assert jp_path != cn_path
    assert "pjsk-jp-assets" in jp_path.parts
    assert "pjsk-cn-assets" in cn_path.parts


def test_collect_event_urls() -> None:
    event_stories = [
        {
            "assetbundleName": "event_stella_2020",
            "eventStoryEpisodes": [
                {"scenarioId": "event_01_01"},
                {"scenarioId": "event_01_02"},
            ],
        }
    ]
    urls = _collect_event_urls(event_stories, _tpl("event_asset"))
    assert len(urls) == 2
    assert "event_stella_2020/scenario/event_01_01.asset" in urls[0]
    assert "event_stella_2020/scenario/event_01_02.asset" in urls[1]


def test_collect_event_urls_skips_empty_fields() -> None:
    event_stories = [
        {"assetbundleName": "", "eventStoryEpisodes": [{"scenarioId": "x"}]},
        {"assetbundleName": "a", "eventStoryEpisodes": [{"scenarioId": ""}]},
    ]
    assert _collect_event_urls(event_stories, _tpl("event_asset")) == []


def test_collect_unit_urls() -> None:
    unit_stories = [
        {
            "chapters": [
                {
                    "assetbundleName": "unit_01",
                    "episodes": [{"scenarioId": "unit_01_01"}],
                }
            ]
        }
    ]
    urls = _collect_unit_urls(unit_stories, _tpl("unit_asset"))
    assert len(urls) == 1
    assert "unit_01/unit_01_01.asset" in urls[0]


def test_collect_card_urls() -> None:
    card_episodes = [
        {"cardId": 1, "scenarioId": "card_01_01"},
        {"cardId": 1, "scenarioId": "card_01_02"},
        {"cardId": 999, "scenarioId": "card_999_01"},  # no matching card
    ]
    cards_lookup = {1: {"assetbundleName": "res001"}}
    urls = _collect_card_urls(card_episodes, cards_lookup, _tpl("card_asset"))
    assert len(urls) == 2
    assert "res001/card_01_01.asset" in urls[0]


def test_collect_talk_urls() -> None:
    action_sets = [
        {"id": 150, "scenarioId": "areatalk_ev_01"},
        {"id": 200},  # no scenarioId
    ]
    urls = _collect_talk_urls(action_sets, _tpl("talk_asset"))
    assert len(urls) == 1
    group = math.floor(150 / 100)
    assert f"group{group}/areatalk_ev_01.asset" in urls[0]


def test_collect_self_urls() -> None:
    profiles = [{"scenarioId": "self_01_02"}]
    urls = _collect_self_urls(profiles, _tpl("self_asset"))
    assert len(urls) == 2
    assert "self_01.asset" in urls[0]  # grade 1
    assert "self_01_02.asset" in urls[1]  # grade 2


def test_collect_special_urls_skips_id2() -> None:
    stories = [
        {"id": 2, "episodes": [{"assetbundleName": "sp2", "scenarioId": "sp_02_01"}]},
        {"id": 3, "episodes": [{"assetbundleName": "sp3", "scenarioId": "sp_03_01"}]},
    ]
    urls = _collect_special_urls(stories, _tpl("special_asset"))
    assert len(urls) == 1
    assert "sp3/sp_03_01.asset" in urls[0]


def test_load_urls_has_both_sources() -> None:
    urls = _load_urls()
    assert "sekai.best" in urls
    assert "haruki" in urls
    for src in ("sekai.best", "haruki"):
        for key in ("master", "event_asset", "unit_asset", "card_asset", "talk_asset", "self_asset", "special_asset",
                    "path_extractor", "path_extractor_group"):
            assert key in urls[src], f"missing {key} in {src}"

