from __future__ import annotations

import csv
import io
from pathlib import Path

from src.common.http import RetryConfig, create_async_client, get_text
from src.common.io import atomic_write_text

JP_B30_CSV_URL = (
    "https://docs.google.com/spreadsheets/d/1B8tX9VL2PcSJKyuHFVd2UT_8kYlY4ZdwHwg9MfWOPug/"
    "export?format=csv&gid=1855810409"
)
CN_B30_CSV_URL = (
    "https://docs.google.com/spreadsheets/d/1Yv3GXnCIgEIbHL72EuZ-d5q_l-auPgddWi4Efa14jq0/"
    "export?format=csv&gid=182216"
)
EXPECTED_HEADERS = ["Song", "", "Constant", "Level", "Note Count", "Difficulty", "Song ID", "Notes"]
MIN_JP_ROWS = 100
MIN_CN_ROWS = 5


def parse_csv_rows(csv_text: str) -> tuple[list[str], list[dict[str, str]]]:
    reader = csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff")))
    fieldnames = reader.fieldnames
    if fieldnames is None:
        raise ValueError("CSV has no header row")
    rows = [{key: (value or "") for key, value in row.items()} for row in reader]
    return list(fieldnames), rows


def _build_csv_text(fieldnames: list[str], rows: list[dict[str, str]]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def _validate_headers(source: str, fieldnames: list[str]) -> None:
    if fieldnames != EXPECTED_HEADERS:
        raise ValueError(f"{source} CSV header mismatch: expected {EXPECTED_HEADERS}, got {fieldnames}")


def _validate_row_count(source: str, rows: list[dict[str, str]], min_rows: int) -> None:
    if len(rows) < min_rows:
        raise ValueError(f"{source} CSV row count too small: expected >= {min_rows}, got {len(rows)}")


def merge_b30_csv_texts(jp_text: str, cn_text: str) -> tuple[str, int, int]:
    jp_fields, jp_rows = parse_csv_rows(jp_text)
    cn_fields, cn_rows = parse_csv_rows(cn_text)
    _validate_headers("JP", jp_fields)
    _validate_headers("CN", cn_fields)
    _validate_row_count("JP", jp_rows, MIN_JP_ROWS)
    _validate_row_count("CN", cn_rows, MIN_CN_ROWS)

    merged_rows = jp_rows + cn_rows
    merged_text = _build_csv_text(jp_fields, merged_rows)
    return merged_text, len(jp_rows), len(cn_rows)


async def update_b30_csv(
    output_dir: Path = Path("data/pjskb30"),
) -> dict[str, int]:
    output_dir.mkdir(parents=True, exist_ok=True)

    async with create_async_client() as client:
        jp_text = await get_text(client, JP_B30_CSV_URL, retry_config=RetryConfig(attempts=6))
        cn_text = await get_text(client, CN_B30_CSV_URL, retry_config=RetryConfig(attempts=6))

    merged_text, jp_rows, cn_rows = merge_b30_csv_texts(jp_text, cn_text)

    jp_path = output_dir / "jp_chart.csv"
    cn_path = output_dir / "cn_chart.csv"
    merged_path = output_dir / "merged_chart.csv"

    atomic_write_text(jp_path, jp_text)
    atomic_write_text(cn_path, cn_text)
    atomic_write_text(merged_path, merged_text)

    return {
        "jp_rows": jp_rows,
        "cn_rows": cn_rows,
        "merged_rows": jp_rows + cn_rows,
    }
