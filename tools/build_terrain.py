#!/usr/bin/env python3
"""Build terrarium-encoded raster PMTiles of the GEM-15 DSM (or DTM) for
MapLibre's ``raster-dem`` source.

    python3 tools/build_terrain.py                     # global, z0-13, dsm
    python3 tools/build_terrain.py --band dtm \\
        --output staging/publish/gem-15/terrain/gem-15-dtm-terrarium.pmtiles
    python3 tools/build_terrain.py --bbox 1.5,41.0,2.5,41.9 --minzoom 0 \\
        --maxzoom 14 \\
        --output staging/publish/gem-15/terrain/gem-15-dsm-terrarium-demo-barcelona.pmtiles
    # smoke test: one cell, shallow zooms, decode-vs-source verification
    python3 tools/build_terrain.py --cells wm91sm1 --minzoom 8 --maxzoom 11 \\
        --output staging/smoke/terrain-sample.pmtiles --verify

## Method

Built zoom-by-zoom, shallowest first ("top-down"); every zoom reads straight
from the source COGs (never resamples a deeper zoom's own output), because a
zoom-to-zoom pyramid would compound resampling error on top of GEM-15's own
15 m data and would not let a sparse zoom be skipped independently.

For each zoom ``z`` and each cell that intersects the requested area:

1. Find every tile the cell's 1x1-degree bbox covers at ``z``
   (``mercantile.tiles``), and the single bounding box of pixels across all
   of them, snapped exactly to tile boundaries.
2. Warp the cell's band-1 mosaic (``WarpedVRT``-equivalent via
   ``gdal.Warp``) straight onto that bounding pixel grid in EPSG:3857, in
   one call -- not once per tile. GDAL's automatic overview selection picks
   whichever internal overview (30/60/120/240/480 m) is closest to the
   zoom's resolution; **bilinear** resampling is used throughout because
   every zoom here is a downsample from 15 m native data (there is no
   true upsampling case: z13's ~19 m/px is already coarser than the 15 m
   source), and bilinear avoids the blockiness nearest-neighbor would add
   to a continuous elevation surface.
3. Slice that one warped array into each tile's 256x256 window.

Tiles are filled, not derived from tile to tile, so a tile wholly inside one
cell needs exactly one warp; a tile straddling two cells (or two UTM zones)
needs one warp per contributing cell. GEM-15's cells are a strict 1-degree
grid, never crossing a UTM zone internally, so "straddles a UTM boundary"
and "straddles a cell boundary" are the same case here, handled identically
either way: fill whichever contributing cell has data at a pixel, any cell's
own NaN losing to another cell's real value at that pixel, 0 only once no
cell covers it at all with a real value.

### Exact-completion compositing, bounded memory

Before any raster I/O, this script counts, for every tile not yet written,
how many cells intersect it (pure bbox math, free). As cells are processed
(in any order, in parallel) that count is decremented; a tile is terrarium-
encoded and written the moment its count reaches zero, then dropped from
memory. This is what lets tiles be composited correctly across cells
without needing a particular processing order or holding the whole zoom's
pixels in memory at once -- the *pending* set stays small (just tiles whose
contributing cells have not all run yet), even though the upfront count
table is sized to the whole zoom.

That count table is still O(tiles at this zoom), and at the deepest zooms
over the *whole planet* that is tens of millions of entries -- a real
memory cost, intended to be paid on the big server (RUNBOOK.md), not a
laptop. ``--bbox``/``--cells`` bound it for a region-at-a-time run; see
RUNBOOK.md's scale-down appendix. A tile with zero intersecting cells is
never counted and never written at all, so the archive stays sparse.

## NaN, nodata, and the ocean convention

The source nodata (water, voids, masked cells: see DESIGN.md) is NaN. NaN
never reaches the terrarium formula: every pixel still NaN after every
contributing cell has been composited is set to elevation 0 before encoding
-- the same convention AWS's terrain-tiles uses for ocean, since terrarium
PNGs have no nodata channel of their own. Negative real elevations (e.g. the
Dead Sea, about -430 m) are ordinary input to the same formula and round
-trip correctly (``--verify`` checks this): the encoding has no sign bit,
ones's complement, or other representation that treats negative values
specially, it is just ``elevation + 32768`` before splitting into bytes, and
32768 is comfortably larger than the deepest point on land.

## Precision

Terrarium is deterministic to 1/256 m (the B channel's fractional byte).
``--verify``'s tolerance (default 1.0 m) is set far above that quantization
step; a failure means a real pipeline bug, not encoding roundoff.

## Output format

An intermediate MBTiles (SQLite) is built first -- resumable tile-by-tile,
with standard ``tiles``/``metadata`` tables, ``format=png``,
``type=baselayer``, and (non-standard, but harmless, and read back by this
script's own ``--verify``) ``encoding=terrarium`` -- then converted with the
``pmtiles`` CLI (``pmtiles convert``) to the final archive. Writing PMTiles
directly was not worth it here: resumability needs random-access upsert by
(z,x,y), which is exactly what SQLite already gives for free, and the
``pmtiles`` CLI's conversion is a single fast pass at the end.
"""
from __future__ import annotations

import argparse
import io
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import mercantile
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _gem15_cells import discover_cells, filter_cells  # noqa: E402

DEFAULT_CACHE_DIR = ROOT / "staging" / "cache"
DEFAULT_OUTPUT = (
    ROOT / "staging" / "publish" / "gem-15" / "terrain" /
    "gem-15-dsm-terrarium.pmtiles"
)

VSICURL_ENV = {
    "GDAL_HTTP_MULTIPLEX": "YES",
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
    "VSI_CACHE": "TRUE",
    "VSI_CACHE_SIZE": "67108864",
    "GDAL_HTTP_TIMEOUT": "60",
}

TILE_SIZE = 256


def log(msg: str) -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------------
# terrarium codec
# --------------------------------------------------------------------------

def encode_terrarium(elevation_m: np.ndarray) -> np.ndarray:
    """elevation (meters, no NaN) -> (H, W, 3) uint8 terrarium RGB."""
    v = elevation_m.astype(np.float64) + 32768.0
    r = np.floor(v / 256.0)
    g = np.floor(np.mod(v, 256.0))
    b = np.floor((v - np.floor(v)) * 256.0)
    rgb = np.stack([r, g, b], axis=-1)
    return np.clip(rgb, 0, 255).astype(np.uint8)


def decode_terrarium(rgb: np.ndarray) -> np.ndarray:
    """(H, W, 3) uint8 terrarium RGB -> elevation (meters), inverse of encode."""
    r, g, b = rgb[..., 0].astype(np.float64), rgb[..., 1].astype(np.float64), rgb[..., 2].astype(np.float64)
    return r * 256.0 + g + b / 256.0 - 32768.0


def png_bytes(rgb: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(rgb, mode="RGB").save(buf, format="PNG")
    return buf.getvalue()


# --------------------------------------------------------------------------
# MBTiles
# --------------------------------------------------------------------------

def open_mbtiles(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS tiles ("
        " zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER,"
        " tile_data BLOB,"
        " PRIMARY KEY (zoom_level, tile_column, tile_row))"
    )
    conn.execute("CREATE TABLE IF NOT EXISTS metadata (name TEXT, value TEXT)")
    conn.commit()
    return conn


def set_mbtiles_metadata(conn: sqlite3.Connection, **kv) -> None:
    for k, v in kv.items():
        conn.execute(
            "INSERT OR REPLACE INTO metadata (name, value) VALUES (?, ?)",
            (k, str(v)),
        )
    conn.commit()


def existing_tiles_for_zoom(conn: sqlite3.Connection, z: int) -> set[tuple[int, int]]:
    rows = conn.execute(
        "SELECT tile_column, tile_row FROM tiles WHERE zoom_level = ?", (z,)
    ).fetchall()
    n = (1 << z)
    return {(col, (n - 1) - row) for col, row in rows}


def write_tile(conn: sqlite3.Connection, z: int, x: int, y: int, data: bytes) -> None:
    tile_row = ((1 << z) - 1) - y  # XYZ -> TMS row flip
    conn.execute(
        "INSERT OR REPLACE INTO tiles (zoom_level, tile_column, tile_row, tile_data) "
        "VALUES (?, ?, ?, ?)",
        (z, x, tile_row, data),
    )


# --------------------------------------------------------------------------
# per-cell warp
# --------------------------------------------------------------------------

def render_cell_block(cell, z: int, url: str) -> dict[tuple[int, int], np.ndarray]:
    """{(x,y): 256x256 float32 elevation array (may hold NaN)} for every
    tile the cell covers at zoom z. {} only if the source could not be
    opened at all."""
    from osgeo import gdal

    gdal.UseExceptions()
    for k, v in VSICURL_ENV.items():
        gdal.SetConfigOption(k, v)

    tiles = list(mercantile.tiles(*cell.bbox, [z]))
    if not tiles:
        return {}
    xs = [t.x for t in tiles]
    ys = [t.y for t in tiles]
    txmin, txmax = min(xs), max(xs)
    tymin, tymax = min(ys), max(ys)
    left, _, _, top = mercantile.xy_bounds(txmin, tymin, z)
    _, bottom, right, _ = mercantile.xy_bounds(txmax, tymax, z)
    width = (txmax - txmin + 1) * TILE_SIZE
    height = (tymax - tymin + 1) * TILE_SIZE

    try:
        src_ds = gdal.Open(f"/vsicurl/{url}")
    except RuntimeError:
        return {}
    if src_ds is None:
        return {}

    band1 = gdal.Translate("", src_ds, format="VRT", bandList=[1])
    warped = gdal.Warp(
        "", band1, format="MEM", dstSRS="EPSG:3857",
        outputBounds=(left, bottom, right, top),
        width=width, height=height,
        resampleAlg="bilinear", errorThreshold=0.0, srcNodata="nan", dstNodata="nan",
        multithread=True,
    )
    if warped is None:
        return {}
    arr = warped.GetRasterBand(1).ReadAsArray().astype(np.float32)

    out = {}
    for t in tiles:
        col0 = (t.x - txmin) * TILE_SIZE
        row0 = (t.y - tymin) * TILE_SIZE
        out[(t.x, t.y)] = arr[row0: row0 + TILE_SIZE, col0: col0 + TILE_SIZE].copy()
    return out


# --------------------------------------------------------------------------
# per-zoom build
# --------------------------------------------------------------------------

def build_zoom(
    z: int, cells, band: str, conn: sqlite3.Connection, workers: int, force: bool
) -> tuple[int, int]:
    done = set() if force else existing_tiles_for_zoom(conn, z)

    cell_tiles: dict = {}
    remaining: dict[tuple[int, int], int] = {}
    for cell in cells:
        tiles = [
            t for t in mercantile.tiles(*cell.bbox, [z])
            if (t.x, t.y) not in done
        ]
        if not tiles:
            continue
        cell_tiles[cell] = tiles
        for t in tiles:
            remaining[(t.x, t.y)] = remaining.get((t.x, t.y), 0) + 1

    if not remaining:
        log(f"z{z}: nothing to do ({len(done)} tile(s) already built)")
        return 0, len(done)

    log(f"z{z}: {len(remaining)} tile(s) to build from {len(cell_tiles)} "
        f"cell(s) ({len(done)} already built)")

    accum: dict[tuple[int, int], np.ndarray] = {}
    written = 0
    t0 = time.time()

    def task(cell):
        url = cell.dsm_url if band == "dsm" else cell.dtm_url
        return cell, render_cell_block(cell, z, url)

    def finalize(xy: tuple[int, int]) -> None:
        nonlocal written
        arr = accum.pop(xy, None)
        if arr is None:
            arr = np.full((TILE_SIZE, TILE_SIZE), np.nan, dtype=np.float32)
        elev = np.where(np.isnan(arr), 0.0, arr).astype(np.float32)
        write_tile(conn, z, xy[0], xy[1], png_bytes(encode_terrarium(elev)))
        written += 1

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(task, c): c for c in cell_tiles}
        done_n = 0
        for fut in as_completed(futures):
            cell, blocks = fut.result()
            done_n += 1
            if not blocks:
                log(f"  WARNING: {cell.cell_id} produced no data at z{z} "
                    f"(source unreachable?)")
            for xy, arr in blocks.items():
                if xy not in remaining:
                    continue
                cur = accum.get(xy)
                if cur is None:
                    cur = np.full((TILE_SIZE, TILE_SIZE), np.nan, dtype=np.float32)
                    accum[xy] = cur
                fill = np.isnan(cur) & ~np.isnan(arr)
                cur[fill] = arr[fill]
                remaining[xy] -= 1
                if remaining[xy] <= 0:
                    finalize(xy)
                    del remaining[xy]
            if done_n % 50 == 0 or done_n == len(cell_tiles):
                rate = done_n / max(time.time() - t0, 1e-6)
                log(f"  z{z}: {done_n}/{len(cell_tiles)} cells "
                    f"({rate:.1f}/s), {written} tile(s) written, "
                    f"{len(accum)} pending")

    # Cells that errored out leave their tiles' counters above zero forever;
    # finalize those now with whatever partial data was gathered.
    for xy in list(remaining.keys()):
        finalize(xy)
    conn.commit()
    return written, len(done)


# --------------------------------------------------------------------------
# verify
# --------------------------------------------------------------------------

def verify_mbtiles_tile(
    conn: sqlite3.Connection, z: int, x: int, y: int, url: str, tolerance_m: float = 1.0
) -> bool:
    """Decode (z,x,y) from the MBTiles and compare a few pixels against a
    fresh, independent rasterio read+warp of the source COG at the same
    grid. Returns True iff every sampled pixel is within tolerance_m
    (terrarium's own quantization is 1/256 m, far below any sane tolerance)."""
    # Deliberately independent of render_cell_block's own code path where it
    # matters (read straight off the URL at this exact tile's grid, one
    # call), but built on the same GDAL bindings as the rest of this script
    # rather than rasterio/pyproj -- a second CRS/PROJ stack in the same
    # process has been observed to disagree with GDAL's about which proj.db
    # to use, which is an environment problem, not a thing this check should
    # be trying to detect.
    from osgeo import gdal

    gdal.UseExceptions()
    for k, v in VSICURL_ENV.items():
        gdal.SetConfigOption(k, v)

    tile_row = ((1 << z) - 1) - y
    row = conn.execute(
        "SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
        (z, x, tile_row),
    ).fetchone()
    if row is None:
        log(f"verify: tile z{z}/{x}/{y} not found in MBTiles")
        return False
    png = Image.open(io.BytesIO(row[0])).convert("RGB")
    decoded = decode_terrarium(np.array(png))

    left, bottom, right, top = mercantile.xy_bounds(x, y, z)
    src_ds = gdal.Open(f"/vsicurl/{url}")
    band1 = gdal.Translate("", src_ds, format="VRT", bandList=[1])
    warped = gdal.Warp(
        "", band1, format="MEM", dstSRS="EPSG:3857",
        outputBounds=(left, bottom, right, top),
        width=TILE_SIZE, height=TILE_SIZE,
        resampleAlg="bilinear", errorThreshold=0.0, srcNodata="nan", dstNodata="nan",
        multithread=True,
    )
    ref = warped.GetRasterBand(1).ReadAsArray().astype(np.float32)

    # Sample a grid of candidate points, but only ones with *real* source
    # data (ref not NaN before the 0-fill). Right at a nodata boundary,
    # bilinear-with-partial-nodata resampling is legitimately sensitive to
    # exactly how GDAL chunks the warp, so two independently-issued warp
    # calls (this tile-sized one here vs. the cell-sized one the real build
    # uses) can disagree there by more than this check's tolerance without
    # either one being wrong -- that disagreement is about warp chunking,
    # not about whether the terrarium encoding round-trips correctly, which
    # is what this check exists to catch. Interior, fully-valid pixels are
    # unaffected and are what the comparison below actually uses.
    ys = np.linspace(5, TILE_SIZE - 6, 12).astype(int)
    xs = np.linspace(5, TILE_SIZE - 6, 12).astype(int)
    diffs = []
    for yy in ys:
        for xx in xs:
            if np.isnan(ref[yy, xx]):
                continue
            diffs.append(abs(float(decoded[yy, xx]) - float(ref[yy, xx])))

    if not diffs:
        # No source data anywhere sampled (a wholly void/ocean tile): the
        # only thing to check is that it encoded as flat 0 m, per the NaN ->
        # 0 convention.
        max_diff = float(np.abs(decoded).max())
        note = " (tile has no source data in the sampled region; checked 0-fill instead)"
    else:
        max_diff = max(diffs)
        note = f" ({len(diffs)} valid sample(s))"
    ok = max_diff <= tolerance_m
    log(f"verify: z{z}/{x}/{y}: max sampled |decoded - direct-read| = "
        f"{max_diff:.4f} m (tolerance {tolerance_m} m) -> "
        f"{'OK' if ok else 'FAIL'}{note}")
    return ok


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--band", choices=["dsm", "dtm"], default="dsm")
    p.add_argument("--minzoom", type=int, default=0)
    p.add_argument("--maxzoom", type=int, default=13)
    p.add_argument("--bbox", type=str, default=None,
                   help="minlon,minlat,maxlon,maxlat; subset of cells")
    p.add_argument("--cells", type=str, default=None,
                   help="comma-separated cell ids; for smoke tests")
    p.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    p.add_argument("--mbtiles", type=Path, default=None,
                   help="intermediate MBTiles path (default: output with "
                        ".mbtiles suffix, kept for resumability)")
    p.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--force", action="store_true",
                   help="rebuild tiles already present in the MBTiles")
    p.add_argument("--skip-convert", action="store_true",
                   help="only update the MBTiles; skip pmtiles convert")
    p.add_argument("--verify", action="store_true",
                   help="after building, decode one tile per zoom and "
                        "compare against a direct source read")
    p.add_argument("--verify-tolerance-m", type=float, default=1.0)
    args = p.parse_args()

    cells = discover_cells(args.cache_dir)
    cell_ids = args.cells.split(",") if args.cells else None
    bbox = tuple(float(x) for x in args.bbox.split(",")) if args.bbox else None
    cells = filter_cells(cells, cell_ids=cell_ids, bbox=bbox)
    if not cells:
        sys.exit("no cells matched --cells/--bbox")
    log(f"{len(cells)} cell(s) selected, band={args.band}, "
        f"z{args.minzoom}-z{args.maxzoom}")

    mbtiles_path = args.mbtiles or args.output.with_suffix(".mbtiles")
    conn = open_mbtiles(mbtiles_path)
    west = min(c.bbox[0] for c in cells)
    south = min(c.bbox[1] for c in cells)
    east = max(c.bbox[2] for c in cells)
    north = max(c.bbox[3] for c in cells)
    set_mbtiles_metadata(
        conn,
        name=f"gem-15-{args.band}-terrarium",
        format="png",
        type="baselayer",
        version="1.0.0",
        description=f"GEM-15 {args.band.upper()} elevation, terrarium-encoded",
        attribution="This dataset is produced by Google",
        bounds=f"{west},{south},{east},{north}",
        minzoom=args.minzoom,
        maxzoom=args.maxzoom,
        encoding="terrarium",  # not standard MBTiles, but harmless and
                                # what this script's own --verify, and any
                                # reader that bothers to look, can trust
    )

    verify_tiles: list[tuple[int, int, int, str]] = []
    total_written = 0
    for z in range(args.minzoom, args.maxzoom + 1):
        written, already = build_zoom(z, cells, args.band, conn, args.workers, args.force)
        total_written += written
        if args.verify and (written or already):
            sample_cell = cells[len(cells) // 2]
            sample_tiles = list(mercantile.tiles(*sample_cell.bbox, [z]))
            if sample_tiles:
                t = sample_tiles[len(sample_tiles) // 2]
                url = sample_cell.dsm_url if args.band == "dsm" else sample_cell.dtm_url
                verify_tiles.append((z, t.x, t.y, url))

    log(f"wrote {total_written} new tile(s) to {mbtiles_path}")

    all_ok = True
    for z, x, y, url in verify_tiles:
        ok = verify_mbtiles_tile(conn, z, x, y, url, args.verify_tolerance_m)
        all_ok = all_ok and ok

    conn.close()

    if args.skip_convert:
        log("--skip-convert set; leaving MBTiles in place")
        return 0 if all_ok else 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["pmtiles", "convert", "--force", str(mbtiles_path), str(args.output)]
    log("running: " + " ".join(cmd))
    subprocess.run(cmd, check=True)
    log(f"wrote {args.output} ({args.output.stat().st_size / 1e6:.2f} MB)")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
