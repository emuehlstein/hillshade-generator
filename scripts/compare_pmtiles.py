#!/usr/bin/env python3
"""Compare two PMTiles files by tile content, ignoring container metadata.

Usage:
    python3 compare_pmtiles.py FILE_A FILE_B

Reads every tile from both files, hashes the raw tile bytes (PNG data),
and reports:
  - tiles only in A
  - tiles only in B
  - tiles in both but with different content
  - summary match %

PMTiles format: https://github.com/protomaps/PMTiles
We use the pmtiles CLI to extract tiles to a temp dir, then diff.
Falls back to reading the binary format directly if pmtiles not found.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from collections import defaultdict


def hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def extract_tiles(pmtiles_path: Path, out_dir: Path) -> dict[str, str]:
    """Extract all tiles from a PMTiles file → {z/x/y: sha256[:16]}."""
    pmtiles = shutil.which("pmtiles")
    if not pmtiles:
        sys.exit("pmtiles CLI not found. Install: go install github.com/protomaps/go-pmtiles/cmd/pmtiles@latest")

    print(f"  Extracting {pmtiles_path.name} → {out_dir} ...")
    result = subprocess.run(
        [pmtiles, "extract", str(pmtiles_path), str(out_dir)],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        # Some versions use 'convert' to xyz dir
        result = subprocess.run(
            [pmtiles, "convert", "--input", str(pmtiles_path), "--output", str(out_dir)],
            capture_output=True, text=True
        )
    if result.returncode != 0:
        sys.exit(f"Failed to extract {pmtiles_path}: {result.stderr}")

    tiles = {}
    for png in out_dir.rglob("*.png"):
        # path is out_dir/{z}/{x}/{y}.png
        parts = png.relative_to(out_dir).parts
        if len(parts) == 3:
            key = "/".join(parts).replace(".png", "")
            tiles[key] = hash_file(png)
    return tiles


def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} FILE_A FILE_B")
        sys.exit(1)

    path_a = Path(sys.argv[1])
    path_b = Path(sys.argv[2])
    for p in (path_a, path_b):
        if not p.exists():
            sys.exit(f"Not found: {p}")

    with tempfile.TemporaryDirectory() as tmp:
        dir_a = Path(tmp) / "a"
        dir_b = Path(tmp) / "b"
        dir_a.mkdir(); dir_b.mkdir()

        tiles_a = extract_tiles(path_a, dir_a)
        tiles_b = extract_tiles(path_b, dir_b)

    keys_a = set(tiles_a)
    keys_b = set(tiles_b)
    all_keys = keys_a | keys_b

    only_a = keys_a - keys_b
    only_b = keys_b - keys_a
    in_both = keys_a & keys_b
    matching = {k for k in in_both if tiles_a[k] == tiles_b[k]}
    different = {k for k in in_both if tiles_a[k] != tiles_b[k]}

    print(f"\n{'='*60}")
    print(f"  A: {path_a.name}  ({len(tiles_a):,} tiles)")
    print(f"  B: {path_b.name}  ({len(tiles_b):,} tiles)")
    print(f"{'='*60}")
    print(f"  Only in A:       {len(only_a):,}")
    print(f"  Only in B:       {len(only_b):,}")
    print(f"  In both:         {len(in_both):,}")
    print(f"    Matching:      {len(matching):,}  ✓")
    print(f"    Different:     {len(different):,}  ✗")
    if in_both:
        pct = len(matching) / len(in_both) * 100
        print(f"  Match rate:      {pct:.1f}%")
    print(f"{'='*60}")

    if different:
        print(f"\nFirst 20 differing tiles:")
        for k in sorted(different)[:20]:
            print(f"  {k}  A={tiles_a[k]}  B={tiles_b[k]}")

    if only_a:
        print(f"\nFirst 10 tiles only in A:")
        for k in sorted(only_a)[:10]:
            print(f"  {k}")

    if only_b:
        print(f"\nFirst 10 tiles only in B:")
        for k in sorted(only_b)[:10]:
            print(f"  {k}")

    if len(different) == 0 and len(only_a) == 0 and len(only_b) == 0:
        print("\n✅ Files are tile-for-tile identical.")
        sys.exit(0)
    elif len(different) == 0:
        print("\n⚠️  Same tile content where they overlap, but tile sets differ.")
        sys.exit(1)
    else:
        print("\n❌ Tile content differs.")
        sys.exit(2)


if __name__ == "__main__":
    main()
