#!/usr/bin/env python3
"""Render ``catalog/gem-15/thumbnail.png``, the one derived file this
pipeline commits to git (it is a small PNG, not a GeoParquet/COG/PMTiles --
see AGENTS.md's "data never enters git" rule, which this doesn't violate).

Same cyclic-palette-times-hillshade look as ``build_overview.py``
(``render_cell``, reused directly rather than reimplemented), over the
Barcelona demo area -- the same place ``build_terrain.py``'s demo PMTiles
and the portolan-spec example app use -- at a much finer ~90-120 m/px, since
a thumbnail covers two cells, not the planet.

    python3 tools/build_thumbnail.py                 # writes the real file
    python3 tools/build_thumbnail.py --output staging/smoke/thumbnail.png

The output is exactly 800x533 (3:2, matching the Portolan catalog card
shape). The source bbox is computed to already be close to 3:2 in EPSG:3857
meters (not degrees -- a 1 degree box is not 3:2 once projected), centered
on Barcelona (2.165 E, 41.395 N, the EE example script's own
``Map.setCenter``), so the final forced resize to 800x533 does not visibly
stretch the render.
"""
from __future__ import annotations

import argparse
import math
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_overview import load_palette, render_cell  # noqa: E402
from _gem15_cells import discover_cells, filter_cells  # noqa: E402

DEFAULT_CACHE_DIR = ROOT / "staging" / "cache"
DEFAULT_TILE_CACHE_DIR = ROOT / "staging" / "cache" / "thumbnail"
DEFAULT_OUTPUT = ROOT / "catalog" / "gem-15" / "thumbnail.png"

CENTER_LON = 2.165
CENTER_LAT = 41.395
HALF_HEIGHT_DEG = 0.55  # ~1.1 degrees of latitude, both GEM-15 cells (w1s41, w2s41)
THUMB_W, THUMB_H = 800, 533  # 3:2

EARTH_R = 6378137.0


def mercator_y(lat_deg: float) -> float:
    lat_rad = math.radians(lat_deg)
    return EARTH_R * math.log(math.tan(math.pi / 4 + lat_rad / 2))


def thumbnail_bbox() -> tuple[float, float, float, float]:
    """(west, south, east, north) in degrees, close to 3:2 once projected."""
    lat0, lat1 = CENTER_LAT - HALF_HEIGHT_DEG, CENTER_LAT + HALF_HEIGHT_DEG
    height_m = mercator_y(lat1) - mercator_y(lat0)
    width_m = height_m * (THUMB_W / THUMB_H)
    meters_per_degree_lon = EARTH_R * math.pi / 180.0  # constant in EPSG:3857
    half_width_deg = (width_m / 2) / meters_per_degree_lon
    return (CENTER_LON - half_width_deg, lat0, CENTER_LON + half_width_deg, lat1)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    p.add_argument("--resolution-m", type=float, default=90.0)
    p.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    p.add_argument("--tile-cache-dir", type=Path, default=DEFAULT_TILE_CACHE_DIR)
    p.add_argument("--force", action="store_true")
    args = p.parse_args()

    bbox = thumbnail_bbox()
    print(f"thumbnail bbox (deg): {bbox}")

    palette = load_palette()
    cells = discover_cells(args.cache_dir)
    cells = filter_cells(cells, bbox=bbox)
    if not cells:
        sys.exit("no cells intersect the thumbnail bbox")
    print(f"{len(cells)} cell(s): {[c.cell_id for c in cells]}")

    tiles = []
    for cell in cells:
        out = args.tile_cache_dir / cell.group_id / f"{cell.cell_id}.tif"
        if args.force or not out.exists():
            ok = render_cell(cell, args.resolution_m, out, palette)
            if not ok:
                print(f"  skip {cell.cell_id}: no usable DSM data")
                continue
        tiles.append(out)
    if not tiles:
        sys.exit("no tiles rendered")

    from osgeo import gdal

    gdal.UseExceptions()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    vrt_path = args.output.with_suffix(".vrt")
    gdal.BuildVRT(str(vrt_path), [str(t) for t in tiles])

    # Crop to the exact bbox (reprojected to the VRT's own EPSG:3857) and
    # force the exact 800x533 output size in one gdal_translate call.
    west, south, east, north = bbox
    merc_ulx, merc_uly = EARTH_R * math.radians(west), mercator_y(north)
    merc_lrx, merc_lry = EARTH_R * math.radians(east), mercator_y(south)
    cmd = [
        "gdal_translate", str(vrt_path), str(args.output),
        "-of", "PNG",
        "-projwin", str(merc_ulx), str(merc_uly), str(merc_lrx), str(merc_lry),
        "-projwin_srs", "EPSG:3857",
        "-outsize", str(THUMB_W), str(THUMB_H),
        "-r", "bilinear",
    ]
    print("running: " + " ".join(cmd))
    subprocess.run(cmd, check=True)
    # gdal PNG driver leaves a .aux.xml sidecar; the published thumbnail is
    # the PNG alone.
    aux = args.output.with_suffix(args.output.suffix + ".aux.xml")
    if aux.exists():
        aux.unlink()
    print(f"wrote {args.output} ({args.output.stat().st_size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
