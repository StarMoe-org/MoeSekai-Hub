from __future__ import annotations

from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any

from src.common.http import RetryConfig, create_async_client, get_json
from src.common.io import write_json

MUSIC_METAS_URL = "https://storage.sekai.best/sekai-best-assets/music_metas.json"
POWER = 250_000
BOOST_BONUS_DICT = {0: 1, 1: 5, 2: 10, 3: 15}
INTERVAL_MULTI = 45
INTERVAL_AUTO = 35
SOLO_SKILLS = [1.20, 1.00, 1.00, 1.00, 1.00]
AUTO_SKILLS = [1.20, 1.00, 1.00, 1.00, 1.00]
MULTI_SKILLS = [2.00, 2.00, 2.00, 2.00, 2.00]
PSPI_METRICS = [
    "auto_score",
    "solo_score",
    "multi_score",
    "auto_pt_max",
    "solo_pt_max",
    "multi_pt_max",
    "pt_per_hour_auto",
    "pt_per_hour_multi",
]
RANKING_METRICS: list[tuple[str, bool]] = [
    ("pt_per_hour_multi", True),
    ("pt_per_hour_auto", True),
    ("auto_score", True),
    ("solo_score", True),
    ("multi_score", True),
    ("auto_pt_max", True),
    ("solo_pt_max", True),
    ("multi_pt_max", True),
    ("cycles_multi", True),
]


def score_bonus(score: int) -> int:
    return score // 20_000


def truncate_to_two_decimal(num: float) -> float:
    decimal_value = Decimal(str(num))
    return float(decimal_value.quantize(Decimal("0.01"), rounding=ROUND_DOWN))


def calc_event_pt(score: int, event_rate: int, event_bonus: int = 0, live_bonus: int = 0) -> int:
    score_b = score_bonus(score)
    scaled_score = truncate_to_two_decimal((100 + score_b) * (100 + event_bonus) / 100)
    basic_pt = int(scaled_score * event_rate / 100)
    return basic_pt * BOOST_BONUS_DICT.get(live_bonus, 1)


def _require_str(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Invalid {field_name}: expected string")
    return value


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _require_number(value: Any, field_name: str) -> float:
    if not _is_number(value):
        raise ValueError(f"Invalid {field_name}: expected number")
    return float(value)


def _require_int(value: Any, field_name: str) -> int:
    if not _is_number(value):
        raise ValueError(f"Invalid {field_name}: expected integer")
    return int(value)


def _require_score_vector(value: Any, field_name: str) -> list[float]:
    if not isinstance(value, list):
        raise ValueError(f"Invalid {field_name}: expected list")
    if len(value) < 6:
        raise ValueError(f"Invalid {field_name}: expected at least 6 entries")
    return [_require_number(item, field_name) for item in value[:6]]


def calculate_scores(music_metas: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    results: list[dict[str, Any]] = []
    baseline: dict[str, Any] | None = None
    sorted_solo_skills = sorted(SOLO_SKILLS, reverse=True)

    for meta in music_metas:
        music_id = _require_int(meta.get("music_id"), "music_id")
        difficulty = _require_str(meta.get("difficulty"), "difficulty")
        music_time = _require_number(meta.get("music_time", 120), "music_time")
        event_rate = _require_int(meta.get("event_rate", 100), "event_rate")
        base_score = _require_number(meta.get("base_score"), "base_score")
        base_score_auto = _require_number(meta.get("base_score_auto", 0.7), "base_score_auto")
        skill_score_solo = _require_score_vector(meta.get("skill_score_solo"), "skill_score_solo")
        skill_score_auto = _require_score_vector(meta.get("skill_score_auto", skill_score_solo), "skill_score_auto")
        skill_score_multi = _require_score_vector(meta.get("skill_score_multi"), "skill_score_multi")
        fever_score = _require_number(meta.get("fever_score"), "fever_score")

        sorted_indices = sorted(range(5), key=lambda idx: skill_score_solo[idx], reverse=True)
        solo_skill_contribution = sum(
            skill_score_solo[idx] * sorted_solo_skills[rank]
            for rank, idx in enumerate(sorted_indices)
        )
        solo_skill_contribution += skill_score_solo[5] * SOLO_SKILLS[0]
        solo_score = int(POWER * (base_score + solo_skill_contribution) * 4)

        sorted_indices_auto = sorted(range(5), key=lambda idx: skill_score_auto[idx], reverse=True)
        auto_skill_contribution = sum(
            skill_score_auto[idx] * sorted_solo_skills[rank]
            for rank, idx in enumerate(sorted_indices_auto)
        )
        auto_skill_contribution += skill_score_auto[5] * AUTO_SKILLS[0]
        auto_score = int(POWER * (base_score_auto + auto_skill_contribution) * 4)

        multi_skill_contribution = sum(skill_score_multi[idx] * MULTI_SKILLS[idx] for idx in range(5))
        multi_skill_contribution += skill_score_multi[5] * MULTI_SKILLS[0]
        multi_score_pct = base_score + multi_skill_contribution + fever_score * 0.5 + 0.01875
        multi_score = int(POWER * multi_score_pct * 4)

        solo_pt_0fire = calc_event_pt(solo_score, event_rate, 0, 0)
        solo_pt_max = calc_event_pt(solo_score, event_rate, 200, 3)
        auto_pt_0fire = calc_event_pt(auto_score, event_rate, 0, 0)
        auto_pt_max = calc_event_pt(auto_score, event_rate, 200, 3)
        multi_pt_0fire = calc_event_pt(multi_score, event_rate, 0, 0)
        multi_pt_max = calc_event_pt(multi_score, event_rate, 200, 3)

        cycles_auto = round(3600 / (music_time + INTERVAL_AUTO), 1)
        cycles_multi = round(3600 / (music_time + INTERVAL_MULTI), 1)
        pt_per_hour_auto = round(cycles_auto * auto_pt_max)
        pt_per_hour_multi = round(cycles_multi * multi_pt_max)

        result = meta.copy()
        result.update(
            {
                "solo_score": solo_score,
                "solo_pt_0fire": solo_pt_0fire,
                "solo_pt_max": solo_pt_max,
                "auto_score": auto_score,
                "auto_pt_0fire": auto_pt_0fire,
                "auto_pt_max": auto_pt_max,
                "multi_score": multi_score,
                "multi_pt_0fire": multi_pt_0fire,
                "multi_pt_max": multi_pt_max,
                "cycles_auto": cycles_auto,
                "cycles_multi": cycles_multi,
                "pt_per_hour_auto": pt_per_hour_auto,
                "pt_per_hour_multi": pt_per_hour_multi,
            }
        )
        results.append(result)

        if music_id == 1 and difficulty == "easy":
            baseline = result

    return results, baseline


def resolve_baseline(
    results: list[dict[str, Any]],
    baseline: dict[str, Any] | None,
) -> tuple[dict[str, Any], int]:
    if baseline is not None:
        return baseline, 0
    if not results:
        raise ValueError("No music meta rows available to build baseline")
    return results[0], 1


def calculate_pspi(results: list[dict[str, Any]], baseline: dict[str, Any]) -> list[dict[str, Any]]:
    for record in results:
        for metric in PSPI_METRICS:
            baseline_value = baseline.get(metric)
            current_value = record.get(metric)
            if _is_number(baseline_value) and baseline_value > 0 and _is_number(current_value):
                record[f"pspi_{metric}"] = round((current_value / baseline_value) * 1000, 1)
            else:
                record[f"pspi_{metric}"] = 0
    return results


def _generated_at() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def generate_rankings(results: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    generated_at = _generated_at()
    rankings_all: dict[str, Any] = {
        "total_charts": len(results),
        "generated_at": generated_at,
        "rankings": {},
    }

    for metric, descending in RANKING_METRICS:
        sorted_records = sorted(results, key=lambda item: item.get(metric, 0), reverse=descending)
        ranking: list[dict[str, Any]] = []
        for rank, record in enumerate(sorted_records, start=1):
            entry: dict[str, Any] = {
                "rank": rank,
                "music_id": record["music_id"],
                "difficulty": record["difficulty"],
                "value": record.get(metric, 0),
            }
            pspi_key = f"pspi_{metric}"
            if pspi_key in record:
                entry["pspi"] = record[pspi_key]
            ranking.append(entry)
        rankings_all["rankings"][metric] = ranking

    rankings_best: dict[str, Any] = {
        "total_songs": len({_require_int(record.get("music_id"), "music_id") for record in results}),
        "generated_at": generated_at,
        "rankings": {},
    }

    for metric, descending in RANKING_METRICS:
        per_song_best: dict[int, dict[str, Any]] = {}
        for record in results:
            music_id = _require_int(record.get("music_id"), "music_id")
            current_best = per_song_best.get(music_id)
            if current_best is None or record.get(metric, 0) > current_best.get(metric, 0):
                per_song_best[music_id] = record

        sorted_records = sorted(per_song_best.values(), key=lambda item: item.get(metric, 0), reverse=descending)
        ranking: list[dict[str, Any]] = []
        for rank, record in enumerate(sorted_records, start=1):
            entry = {
                "rank": rank,
                "music_id": record["music_id"],
                "difficulty": record["difficulty"],
                "value": record.get(metric, 0),
            }
            pspi_key = f"pspi_{metric}"
            if pspi_key in record:
                entry["pspi"] = record[pspi_key]
            ranking.append(entry)
        rankings_best["rankings"][metric] = ranking

    return rankings_all, rankings_best


async def update_music_meta(output_dir: Path = Path("data/music_meta")) -> dict[str, int]:
    output_dir.mkdir(parents=True, exist_ok=True)

    async with create_async_client() as client:
        payload = await get_json(client, MUSIC_METAS_URL, retry_config=RetryConfig(attempts=6))

    if not isinstance(payload, list):
        raise ValueError("Invalid music metas source payload: expected a list")

    music_metas = [item for item in payload if isinstance(item, dict)]
    if not music_metas:
        raise ValueError("Music meta source payload is empty")

    results, baseline = calculate_scores(music_metas)
    baseline_record, used_fallback_baseline = resolve_baseline(results, baseline)
    calculate_pspi(results, baseline_record)
    results.sort(key=lambda item: item["multi_pt_max"], reverse=True)
    rankings_all, rankings_best = generate_rankings(results)

    write_json(output_dir / "music_metas.json", results)
    write_json(output_dir / "rankings_all.json", rankings_all)
    write_json(output_dir / "rankings_best.json", rankings_best)

    return {
        "charts_total": len(results),
        "songs_total": len({_require_int(record.get("music_id"), "music_id") for record in results}),
        "used_fallback_baseline": used_fallback_baseline,
        "output_files": 3,
    }
