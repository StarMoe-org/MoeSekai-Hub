from __future__ import annotations

import pytest

from src.tasks.b30_csv import merge_b30_csv_texts, parse_csv_rows


def test_parse_csv_rows_strips_bom() -> None:
    csv_text = "\ufeffSong,,Constant\nA,Alpha,30.1\n"
    fields, rows = parse_csv_rows(csv_text)
    assert fields == ["Song", "", "Constant"]
    assert rows == [{"Song": "A", "": "Alpha", "Constant": "30.1"}]


def test_merge_b30_csv_texts_combines_without_server_column() -> None:
    header = "Song,,Constant,Level,Note Count,Difficulty,Song ID,Notes\n"
    jp_rows = [
        f"JP-{idx},JP-{idx},30.1,30,1000,Master,{idx},n{idx}\n"
        for idx in range(1, 101)
    ]
    cn_rows = [
        f"CN-{idx},CN-{idx},29.8,29,900,Master,{1000 + idx},n{1000 + idx}\n"
        for idx in range(1, 6)
    ]
    jp_text = header + "".join(jp_rows)
    cn_text = header + "".join(cn_rows)
    merged_text, jp_rows, cn_rows = merge_b30_csv_texts(jp_text, cn_text)

    assert jp_rows == 100
    assert cn_rows == 5

    header = merged_text.splitlines()[0]
    assert header == "Song,,Constant,Level,Note Count,Difficulty,Song ID,Notes"
    assert "server" not in header

    fields, rows = parse_csv_rows(merged_text)
    assert fields == ["Song", "", "Constant", "Level", "Note Count", "Difficulty", "Song ID", "Notes"]
    assert len(rows) == 105
    assert rows[0]["Song"] == "JP-1"
    assert rows[100]["Song"] == "CN-1"


def test_merge_b30_csv_texts_rejects_mismatched_headers() -> None:
    jp_text = "Song,,Constant\nA,Alpha,30.1\n"
    cn_text = "Song,Constant\nB,29.8\n"
    with pytest.raises(ValueError, match="header mismatch"):
        merge_b30_csv_texts(jp_text, cn_text)


def test_merge_b30_csv_texts_rejects_html_block_page() -> None:
    blocked = "<!DOCTYPE html>\n<html>blocked</html>\n"
    with pytest.raises(ValueError, match="header mismatch"):
        merge_b30_csv_texts(blocked, blocked)


def test_merge_b30_csv_texts_rejects_too_few_rows() -> None:
    header = "Song,,Constant,Level,Note Count,Difficulty,Song ID,Notes\n"
    jp_text = header + "A,Alpha,30.1,30,1000,Master,1,n1\n"
    cn_text = header + "B,Beta,29.8,29,900,Master,2,n2\n"
    with pytest.raises(ValueError, match="row count too small"):
        merge_b30_csv_texts(jp_text, cn_text)
