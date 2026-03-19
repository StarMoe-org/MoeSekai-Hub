import math

from src.tasks.story_asset import (
    _asset_url,
    _collect_card_urls,
    _collect_event_urls,
    _collect_self_urls,
    _collect_special_urls,
    _collect_talk_urls,
    _collect_unit_urls,
    _load_urls,
    _url_to_local_path,
)
from pathlib import Path


def _tpl(asset_type: str) -> str:
    urls = _load_urls()
    return _asset_url(urls, "sekai.best", "jp", asset_type)


def test_url_to_local_path() -> None:
    url = "https://storage.sekai.best/sekai-jp-assets/event_story/ev_01/scenario/ev_01_01.asset"
    result = _url_to_local_path(url, Path("story_assets"))
    assert result == Path("story_assets/storage.sekai.best/sekai-jp-assets/event_story/ev_01/scenario/ev_01_01.asset.br")


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
        for key in ("master", "event_asset", "unit_asset", "card_asset", "talk_asset", "self_asset", "special_asset"):
            assert key in urls[src], f"missing {key} in {src}"
