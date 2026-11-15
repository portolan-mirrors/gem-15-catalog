#!/usr/bin/env python3
"""The tiling invariants `build_terrain.py`'s compositing rests on.

`build_zoom_rewarp` no longer materializes a tile list per cell and a
contributor count per tile for the whole requested scope: at global z14
those two structures measured 9.0 GB and the real run was OOM-killed.
It derives the same numbers one cell at a time from integer tile rects
instead (`cell_tile_rect`, `_CellTileIndex`).

That refactor is only safe while the rects and the counts agree exactly
with what `mercantile.tiles` says, so this gate checks that directly:

1. `cell_tile_rect` equals min/max over the real `mercantile.tiles`
   output, and that output really is the full rectangle.
2. `_CellTileIndex.contributor_counts` equals the superseded
   `remaining` dict, tile for tile, including with a partial `done`
   set (the resume case).
3. The admission list is the same cells in the same order the
   superseded `cell_tiles` dict gave.

A count one too high strands a tile forever (it is never written); one
too low finalizes a shared boundary tile a cell short, which bakes 0 m
into up to 23 z13 tiles per 1-degree boundary -- a real bug this
catalog has already paid for once. Both are silent in the output.

Offline and dependency-light: it uses the cached cell list when one is
present and a synthetic 1-degree grid otherwise, so it runs on a clean
checkout.

Run: python3 tests/test_terrain_tiling.py
"""
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import mercantile  # noqa: E402
import numpy as np  # noqa: E402

from build_terrain import (  # noqa: E402
    _CellTileIndex,
    cell_tile_rect,
    keys_present,
    pack_xy,
    unpack_xy,
)

errors: list[str] = []

# Zooms worth checking, not every zoom: z0/z1 are the degenerate case
# where one tile covers thousands of cells (so every cell overlaps every
# other and contributor_counts takes its other branch), z9 is the
# shallowest zoom at which a tile is smaller than a degree, and z13/z14
# are the real build targets.
ZOOMS = (0, 1, 5, 9, 12, 13, 14)


@dataclass(frozen=True)
class FakeCell:
    cell_id: str
    bbox: tuple[float, float, float, float]


def load_cells():
    """Real cells if the discovery cache is on disk, else a synthetic
    block of adjacent 1-degree cells. Both exercise the same invariants;
    the real list additionally covers high latitude, the antimeridian
    edge, and isolated single cells with no neighbours at all."""
    cache = ROOT / "staging" / "cache"
    if (cache / "collection.json").exists():
        try:
            from _gem15_cells import discover_cells

            cells = discover_cells(cache, log=lambda m: None)
            if cells:
                # A contiguous slice plus a far-away one: adjacency is
                # what the contributor counts are about, and a cell with
                # no selected neighbour must come out with count 1
                # everywhere.
                return cells[:120] + cells[-40:], f"{len(cells)} cached cells"
        except Exception as exc:  # noqa: BLE001 - fall through to synthetic
            print(f"  (cached cell list unusable: {exc}; using synthetic)")
    cells = [
        FakeCell(f"w{lon}s{lat}", (float(lon), float(lat), lon + 1.0, lat + 1.0))
        for lon in range(2, 8)
        for lat in range(41, 47)
    ]
    cells.append(FakeCell("w120s60", (120.0, 60.0, 121.0, 61.0)))  # isolated
    return cells, "synthetic grid"


def check_rects(cells):
    """Invariant 1."""
    for z in ZOOMS:
        for cell in cells:
            tiles = list(mercantile.tiles(*cell.bbox, [z]))
            if not tiles:
                continue
            xs = [t.x for t in tiles]
            ys = [t.y for t in tiles]
            want = (min(xs), max(xs), min(ys), max(ys))
            got = cell_tile_rect(cell.bbox, z)
            if got != want:
                errors.append(
                    f"cell_tile_rect z{z} {cell.cell_id}: {got} != {want}")
                continue
            area = (want[1] - want[0] + 1) * (want[3] - want[2] + 1)
            if len(tiles) != area:
                errors.append(
                    f"mercantile.tiles z{z} {cell.cell_id} is not a full "
                    f"rectangle: {len(tiles)} tiles in a {area}-tile rect")


def old_structures(cells, z, done):
    """The superseded setup, verbatim, as the reference to compare to."""
    cell_tiles: dict = {}
    remaining: dict[tuple[int, int], int] = {}
    for cell in cells:
        tiles = [t for t in mercantile.tiles(*cell.bbox, [z])
                 if (t.x, t.y) not in done]
        if not tiles:
            continue
        cell_tiles[cell] = tiles
        for t in tiles:
            remaining[(t.x, t.y)] = remaining.get((t.x, t.y), 0) + 1
    return cell_tiles, remaining


def check_counts(cells, z, done):
    """Invariants 2 and 3, for one zoom and one `done` set."""
    cell_tiles, remaining = old_structures(cells, z, done)
    done_keys = np.array(sorted(pack_xy(x, y) for x, y in done), dtype=np.int64)
    index = _CellTileIndex(cells, z, done_keys)

    admitted = [i for i in range(len(cells)) if index.todo_count(i)]
    if [cells[i] for i in admitted] != list(cell_tiles.keys()):
        errors.append(
            f"z{z} (done={len(done)}): admission list differs from the "
            f"superseded cell_tiles order/membership")

    for i in admitted:
        x0, x1, y0, y1 = index.rects[i]
        counts = index.contributor_counts(i)
        dmask = index.done_mask(i)
        old_tiles = {(t.x, t.y) for t in cell_tiles[cells[i]]}
        new_tiles = set()
        for ix in range(x1 - x0 + 1):
            for iy in range(y1 - y0 + 1):
                xy = (x0 + ix, y0 + iy)
                if dmask[ix, iy]:
                    if xy in remaining:
                        errors.append(
                            f"z{z} {cells[i].cell_id} {xy}: masked as already "
                            f"built but the reference still wants it")
                    continue
                new_tiles.add(xy)
                if int(counts[ix, iy]) != remaining.get(xy):
                    errors.append(
                        f"z{z} {cells[i].cell_id} {xy}: contributor count "
                        f"{int(counts[ix, iy])} != reference "
                        f"{remaining.get(xy)}")
        if new_tiles != old_tiles:
            errors.append(
                f"z{z} {cells[i].cell_id}: tile set differs from the "
                f"reference ({len(new_tiles)} vs {len(old_tiles)})")


def check_key_packing():
    rng = np.random.default_rng(0)
    for x, y in zip(rng.integers(0, 1 << 20, 500), rng.integers(0, 1 << 20, 500)):
        if unpack_xy(pack_xy(int(x), int(y))) != (int(x), int(y)):
            errors.append(f"pack/unpack round-trip failed for ({x}, {y})")
            break
    keys = np.array(sorted(pack_xy(i, i * 3) for i in range(100)), dtype=np.int64)
    probe = np.array([pack_xy(5, 15), pack_xy(5, 16), pack_xy(99, 297)],
                     dtype=np.int64)
    got = keys_present(keys, probe).tolist()
    if got != [True, False, True]:
        errors.append(f"keys_present gave {got}, expected [True, False, True]")
    if keys_present(np.empty(0, dtype=np.int64), probe).any():
        errors.append("keys_present on an empty skip list found a match")


def main() -> int:
    cells, source = load_cells()
    print(f"{len(cells)} cell(s) from {source}; zooms {list(ZOOMS)}")

    check_key_packing()
    check_rects(cells)

    for z in ZOOMS:
        # Fresh start, then a resume with roughly a third of the zoom's
        # tiles already written -- including whole cells and partial
        # cells, which is what a real resumed run looks like.
        check_counts(cells, z, set())
        all_tiles = sorted({
            (t.x, t.y)
            for cell in cells
            for t in mercantile.tiles(*cell.bbox, [z])
        })
        rng = np.random.default_rng(z)
        pick = rng.random(len(all_tiles)) < 0.34
        done = {xy for xy, keep in zip(all_tiles, pick) if keep}
        check_counts(cells, z, done)
        print(f"  z{z}: {len(all_tiles)} distinct tile(s), "
              f"resume case used {len(done)} already-built")

    if errors:
        print(f"\nFAILED ({len(errors)} error(s)):")
        for e in errors[:25]:
            print("  " + e)
        if len(errors) > 25:
            print(f"  ... and {len(errors) - 25} more")
        return 1
    print("\nterrain tiling invariants hold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
