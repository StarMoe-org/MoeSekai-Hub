from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from src.tasks import music_meta as module
from src.tasks.music_meta import (
    calc_event_pt,
    calculate_pspi,
    calculate_scores,
    generate_rankings,
    resolve_baseline,
    score_bonus,
    truncate_to_two_decimal,
)


def build_meta(
    music_id: int,
    difficulty: str,
    *,
    base_score: float,
    base_score_auto: float,
    fever_score: float,
    event_rate: int = 100,
    music_time: int = 120,
    solo_scale: float = 1.0,
    auto_scale: float = 1.0,
    multi_scale: float = 1.0,
) -> dict[str, Any]:
    return {
        "music_id": music_id,
        "difficulty": difficulty,
        "music_time": music_time,
        "event_rate": event_rate,
        "base_score": base_score,
        "base_score_auto": base_score_auto,
        "skill_score_solo": [
            0.1 * solo_scale,
            0.2 * solo_scale,
            0.3 * solo_scale,
            0.4 * solo_scale,
            0.5 * solo_scale,
            0.6 * solo_scale,
        ],
        "skill_score_auto": [
            0.05 * auto_scale,
            0.1 * auto_scale,
            0.15 * auto_scale,
            0.2 * auto_scale,
            0.25 * auto_scale,
            0.3 * auto_scale,
        ],
        "skill_score_multi": [
            0.01 * multi_scale,
            0.02 * multi_scale,
            0.03 * multi_scale,
            0.04 * multi_scale,
            0.05 * multi_scale,
            0.06 * multi_scale,
        ],
        "fever_score": fever_score,
    }


def build_ranking_record(music_id: int, difficulty: str, base: int) -> dict[str, Any]:
    return {
        "music_id": music_id,
        "difficulty": difficulty,
        "pt_per_hour_multi": base + 90,
        "pt_per_hour_auto": base + 80,
        "auto_score": base + 70,
        "solo_score": base + 60,
        "multi_score": base + 50,
        "auto_pt_max": base + 40,
        "solo_pt_max": base + 30,
        "multi_pt_max": base + 20,
        "cycles_multi": base + 10,
        "pspi_pt_per_hour_multi": float(base) + 0.5,
        "pspi_pt_per_hour_auto": float(base) + 1.5,
        "pspi_auto_score": float(base) + 2.5,
        "pspi_solo_score": float(base) + 3.5,
        "pspi_multi_score": float(base) + 4.5,
        "pspi_auto_pt_max": float(base) + 5.5,
        "pspi_solo_pt_max": float(base) + 6.5,
        "pspi_multi_pt_max": float(base) + 7.5,
    }


def test_score_bonus_and_event_pt_formula() -> None:
    assert score_bonus(40_000) == 2
    assert truncate_to_two_decimal(123.456) == 123.45
    assert calc_event_pt(40_000, 100, event_bonus=200, live_bonus=3) == 4_590


def test_calculate_scores_matches_source_formula() -> None:
    results, baseline = calculate_scores(
        [
            build_meta(
                1,
                "easy",
                base_score=1.0,
                base_score_auto=0.5,
                fever_score=0.07,
            )
        ]
    )

    assert baseline is not None
    result = results[0]
    assert result["solo_score"] == 3_320_000
    assert result["solo_pt_0fire"] == 266
    assert result["solo_pt_max"] == 11_970
    assert result["auto_score"] == 1_660_000
    assert result["auto_pt_max"] == 8_235
    assert result["multi_score"] == 1_473_750
    assert result["multi_pt_max"] == 7_785
    assert result["cycles_auto"] == 23.2
    assert result["cycles_multi"] == 21.8
    assert result["pt_per_hour_auto"] == 191_052
    assert result["pt_per_hour_multi"] == 169_713


def test_resolve_baseline_falls_back_to_first_record() -> None:
    results, baseline = calculate_scores(
        [
            build_meta(
                2,
                "hard",
                base_score=0.8,
                base_score_auto=0.4,
                fever_score=0.05,
            )
        ]
    )

    assert baseline is None
    baseline_record, used_fallback = resolve_baseline(results, baseline)
    assert used_fallback == 1
    assert baseline_record["music_id"] == 2


def test_calculate_pspi_uses_baseline_values() -> None:
    results = [
        {
            "music_id": 1,
            "difficulty": "easy",
            "auto_score": 100,
            "solo_score": 200,
            "multi_score": 300,
            "auto_pt_max": 400,
            "solo_pt_max": 500,
            "multi_pt_max": 600,
            "pt_per_hour_auto": 700,
            "pt_per_hour_multi": 800,
        },
        {
            "music_id": 2,
            "difficulty": "expert",
            "auto_score": 150,
            "solo_score": 100,
            "multi_score": 600,
            "auto_pt_max": 200,
            "solo_pt_max": 1_000,
            "multi_pt_max": 300,
            "pt_per_hour_auto": 1_400,
            "pt_per_hour_multi": 400,
        },
    ]

    calculate_pspi(results, results[0])

    assert results[0]["pspi_auto_score"] == 1000.0
    assert results[1]["pspi_auto_score"] == 1500.0
    assert results[1]["pspi_solo_score"] == 500.0
    assert results[1]["pspi_multi_score"] == 2000.0
    assert results[1]["pspi_pt_per_hour_auto"] == 2000.0
    assert results[1]["pspi_pt_per_hour_multi"] == 500.0


def test_generate_rankings_uses_best_chart_per_song() -> None:
    results = [
        build_ranking_record(1, "easy", 100),
        build_ranking_record(1, "master", 200),
        build_ranking_record(2, "expert", 150),
    ]

    rankings_all, rankings_best = generate_rankings(results)

    assert rankings_all["total_charts"] == 3
    assert rankings_best["total_songs"] == 2
    assert rankings_all["rankings"]["pt_per_hour_multi"][0]["difficulty"] == "master"
    assert rankings_all["rankings"]["pt_per_hour_multi"][0]["pspi"] == 200.5
    best_multi = rankings_best["rankings"]["pt_per_hour_multi"]
    assert [entry["music_id"] for entry in best_multi] == [1, 2]
    assert [entry["difficulty"] for entry in best_multi] == ["master", "expert"]


def test_update_music_meta_writes_expected_files(tmp_path, monkeypatch) -> None:
    class DummyClient:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001
            return False

    payload = [
        build_meta(1, "easy", base_score=1.0, base_score_auto=0.5, fever_score=0.07),
        build_meta(
            2,
            "expert",
            base_score=1.2,
            base_score_auto=0.8,
            fever_score=0.09,
            solo_scale=1.1,
            auto_scale=1.1,
            multi_scale=1.1,
        ),
    ]

    async def fake_get_json(client, url, **kwargs):  # noqa: ANN001
        assert url == module.MUSIC_METAS_URL
        return payload

    monkeypatch.setattr(module, "create_async_client", lambda **kwargs: DummyClient())
    monkeypatch.setattr(module, "get_json", fake_get_json)

    stats = asyncio.run(module.update_music_meta(output_dir=Path(tmp_path)))

    music_metas = json.loads((tmp_path / "music_metas.json").read_text(encoding="utf-8"))
    rankings_all = json.loads((tmp_path / "rankings_all.json").read_text(encoding="utf-8"))
    rankings_best = json.loads((tmp_path / "rankings_best.json").read_text(encoding="utf-8"))

    assert stats == {
        "charts_total": 2,
        "songs_total": 2,
        "used_fallback_baseline": 0,
        "output_files": 3,
    }
    assert len(music_metas) == 2
    assert music_metas[0]["multi_pt_max"] >= music_metas[1]["multi_pt_max"]
    assert rankings_all["total_charts"] == 2
    assert rankings_best["total_songs"] == 2
    assert "pt_per_hour_multi" in rankings_all["rankings"]
    assert "multi_pt_max" in rankings_best["rankings"]
