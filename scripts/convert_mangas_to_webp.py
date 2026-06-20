"""One-shot migration: convert ``mangas/*.png`` to ``mangas/*.webp``.

Reuses the same WebP encoding parameters as the live download path in
``src.tasks.manga`` so the migrated images are byte-for-byte consistent with
newly downloaded ones.

Usage::

    uv run python scripts/convert_mangas_to_webp.py [--mangas-dir mangas] [--keep-png]

By default the source ``.png`` files are deleted after a successful conversion.
Pass ``--keep-png`` to retain them (useful for a dry-run / backup).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from src.tasks.manga import MANGA_IMAGE_FORMAT, encode_image_as_webp


def convert_directory(mangas_dir: Path, *, keep_png: bool) -> tuple[int, int]:
    png_files = sorted(mangas_dir.glob("*.png"))
    converted = 0
    failed = 0

    for png_path in png_files:
        webp_path = png_path.with_suffix(f".{MANGA_IMAGE_FORMAT}")
        try:
            webp_bytes = encode_image_as_webp(png_path.read_bytes())
        except Exception as exc:  # noqa: BLE001 - report and continue
            failed += 1
            print(f"[fail] {png_path.name}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue

        webp_path.write_bytes(webp_bytes)
        if not keep_png:
            png_path.unlink()
        converted += 1

    return converted, failed


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert manga PNG images to WebP in place.")
    parser.add_argument("--mangas-dir", type=Path, default=Path("mangas"), help="Directory containing manga images.")
    parser.add_argument("--keep-png", action="store_true", help="Keep the original .png files after conversion.")
    args = parser.parse_args()

    mangas_dir: Path = args.mangas_dir
    if not mangas_dir.is_dir():
        print(f"[error] not a directory: {mangas_dir}", file=sys.stderr)
        return 2

    converted, failed = convert_directory(mangas_dir, keep_png=args.keep_png)
    print(f"[done] converted={converted} failed={failed} keep_png={args.keep_png}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
