#!/usr/bin/env python3
"""Build the global overview COG: Google's cyclic-palette hillshade render.

Reproduces the look of Google's own Earth Engine example script
(``docs/research/gem15-example.js``, ``visualizeCyclic``): elevation mod
100 m through its cyclic palette (named for 256 steps; see ``load_palette``
for why the array as shipped has 268), multiplied by a hillshade scaled into
[0.4, 1.0], computed after reprojecting to EPSG:3857 at a near-native scale.
The palette itself is parsed out of that same JS file at import time, so
there is exactly one copy of Google's colors in this repository.

    python3 tools/build_overview.py                          # full build
    python3 tools/build_overview.py --cells wm91sm1 \\
        --output staging/smoke/overview-sample.tif            # smoke test

Per cell this does, entirely through GDAL (reads tuned for a remote COG):

1. Open ``.../dsm/zall/mosaic.tif`` band 1 over ``/vsicurl/``.
2. Warp it to EPSG:3857 at ``--resolution-m`` (default 960 m/px), bilinear
   resampling, target-aligned pixels. GDAL's automatic overview selection
   picks the DSM's 480 m internal overview as the source for this -- close
   enough to 960 m/px that warping it is a simple 2x decimation, so no finer
   (and much larger) level is ever read.
3. Hillshade that same warped grid (``gdal.DEMProcessing``, az 315 / alt 45).
4. ``color = palette[floor(mod(dsm, 100) / 100 * 255)] * (hillshade/255 *
   0.6 + 0.4)``, alpha 0 wherever the source was NaN.
5. Cache the 4-band (RGBA) result per cell under ``--tile-cache-dir``
   (resumable: a cell already cached is not re-rendered).

After every requested cell is cached, all cached tiles are mosaicked with
``gdal.BuildVRT`` and compressed to a COG with ``gdal_translate`` (shelled
out, so the exact command matches what RUNBOOK.md and this script's
``--lineage`` output both say).

Snapping every cell warp to the same EPSG:3857 grid (target-aligned pixels at
a fixed resolution) is what lets independently-rendered cell tiles mosaic
without seams or an expensive global resample step.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _gem15_cells import discover_cells, filter_cells  # noqa: E402

PALETTE_JS = ROOT / "docs" / "research" / "gem15-example.js"
DEFAULT_CACHE_DIR = ROOT / "staging" / "cache"
DEFAULT_TILE_CACHE_DIR = ROOT / "staging" / "cache" / "overview"
DEFAULT_OUTPUT = ROOT / "staging" / "publish" / "gem-15" / "gem-15_overview.tif"

AZIMUTH = 315.0
ALTITUDE = 45.0
HS_SCALE_MIN = 0.4
HS_SCALE_RANGE = 0.6
CYCLE_MAX = 100.0

# GDAL/CPL tuning for reading a remote, sparsely-accessed COG: multiplexed
# HTTP/2 connections for the many concurrent /vsicurl/ reads, no directory
# listing probes (the bucket has no sidecar files to find), and a generous
# per-dataset block cache so a warp's repeated small reads from one COG
# don't each pay a fresh range request.
VSICURL_ENV = {
    "GDAL_HTTP_MULTIPLEX": "YES",
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
    "VSI_CACHE": "TRUE",
    "VSI_CACHE_SIZE": "67108864",
    "GDAL_HTTP_TIMEOUT": "60",
}


def log(msg: str) -> None:
    print(msg, flush=True)


def load_palette(js_path: Path = PALETTE_JS) -> np.ndarray:
    """Parse the cyclic hex palette out of Google's EE example script.

    The array there is named for its intended 256 steps, but as shipped it
    holds 268 hex colors (one wrap-around color short of a clean repeat).
    We use whatever length it actually has rather than assume 256: the
    lookup below indexes by fraction-of-cycle * len(palette), which is
    correct for any N and degrades to the documented 256-step behavior if
    Google ever trims the array to exactly that.
    """
    text = js_path.read_text()
    m = re.search(r"var elevationPalette = \[(.*?)\];", text, re.S)
    if not m:
        raise RuntimeError(f"could not find elevationPalette array in {js_path}")
    hexes = re.findall(r"#([0-9a-fA-F]{6})", m.group(1))
    if len(hexes) < 2:
        raise RuntimeError(f"found no usable palette entries in {js_path}")
    arr = np.array(
        [[int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)] for h in hexes],
        dtype=np.uint8,
    )
    return arr


def lineage_text(resolution_m: float, n_colors: int) -> str:
    return (
        f"Rendered from GEM-15 DSM band 1 (internal 480 m overview, "
        f"bilinear-decimated to {resolution_m:g} m/px), reprojected to "
        f"EPSG:3857 with target-aligned pixels; colored with Google's "
        f"{n_colors}-entry cyclic palette over elevation mod 100 m "
        f"(docs/research/gem15-example.js), multiplied by hillshade "
        f"(azimuth={AZIMUTH:g}, altitude={ALTITUDE:g}) scaled to "
        f"[{HS_SCALE_MIN:g}, {HS_SCALE_MIN + HS_SCALE_RANGE:g}]; alpha 0 "
        f"where the source DSM is NaN. Per-cell tiles mosaicked and "
        f"compressed with: gdal_translate -of COG -co COMPRESS=ZSTD "
        f"-co BLOCKSIZE=512 -co OVERVIEW_RESAMPLING=AVERAGE -stats"
    )


def render_cell(cell, resolution_m: float, out_path: Path, palette: np.ndarray) -> bool:
    """Render one cell's RGBA tile to out_path. Returns False if the source
    mosaic could not be opened (e.g. a masked/missing cell)."""
    from osgeo import gdal

    gdal.UseExceptions()
    for k, v in VSICURL_ENV.items():
        gdal.SetConfigOption(k, v)

    src_url = f"/vsicurl/{cell.dsm_url}"
    try:
        src_ds = gdal.Open(src_url)
    except RuntimeError:
        return False
    if src_ds is None:
        return False

    band1_vrt = gdal.Translate("", src_ds, format="VRT", bandList=[1])
    warped = gdal.Warp(
        "",
        band1_vrt,
        format="MEM",
        dstSRS="EPSG:3857",
        xRes=resolution_m,
        yRes=resolution_m,
        targetAlignedPixels=True,
        resampleAlg="bilinear", errorThreshold=0.0,
        srcNodata="nan",
        dstNodata="nan",
        multithread=True,
    )
    if warped is None or warped.RasterXSize == 0 or warped.RasterYSize == 0:
        return False

    dsm = warped.GetRasterBand(1).ReadAsArray().astype(np.float32)
    if not np.any(np.isfinite(dsm)):
        return False  # fully masked/void cell; nothing to render

    hillshade_ds = gdal.DEMProcessing(
        "",
        warped,
        "hillshade",
        format="MEM",
        azimuth=AZIMUTH,
        altitude=ALTITUDE,
        computeEdges=True,
    )
    hs = hillshade_ds.GetRasterBand(1).ReadAsArray().astype(np.float32)

    nan_mask = ~np.isfinite(dsm)
    dsm_safe = np.where(nan_mask, 0.0, dsm)
    n_colors = len(palette)
    cyclic = np.mod(dsm_safe, CYCLE_MAX)  # numpy mod: result in [0, CYCLE_MAX)
    idx = np.clip((cyclic / CYCLE_MAX * n_colors).astype(np.int32), 0, n_colors - 1)
    rgb = palette[idx]  # (H, W, 3) uint8

    hs_factor = (hs / 255.0) * HS_SCALE_RANGE + HS_SCALE_MIN
    color = np.clip(rgb.astype(np.float32) * hs_factor[..., None], 0, 255)
    color = color.astype(np.uint8)
    alpha = np.where(nan_mask, 0, 255).astype(np.uint8)

    driver = gdal.GetDriverByName("GTiff")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(".tmp.tif")
    out_ds = driver.Create(
        str(tmp_path), warped.RasterXSize, warped.RasterYSize, 4, gdal.GDT_Byte,
        options=["COMPRESS=DEFLATE", "TILED=YES"],
    )
    out_ds.SetGeoTransform(warped.GetGeoTransform())
    out_ds.SetProjection(warped.GetProjection())
    for i in range(3):
        out_ds.GetRasterBand(i + 1).WriteArray(color[:, :, i])
    out_ds.GetRasterBand(4).WriteArray(alpha)
    out_ds.FlushCache()
    out_ds = None
    tmp_path.replace(out_path)
    return True


def build_mosaic(tile_paths: list[Path], output: Path) -> None:
    """VRT-mosaic the cached cell tiles, then gdal_translate to a COG."""
    from osgeo import gdal

    gdal.UseExceptions()
    output.parent.mkdir(parents=True, exist_ok=True)
    vrt_path = output.with_suffix(".vrt")
    gdal.BuildVRT(str(vrt_path), [str(p) for p in tile_paths])

    cmd = [
        "gdal_translate", str(vrt_path), str(output),
        "-of", "COG",
        "-co", "COMPRESS=ZSTD",
        "-co", "BLOCKSIZE=512",
        "-co", "OVERVIEW_RESAMPLING=AVERAGE",
        "-stats",
    ]
    log("running: " + " ".join(cmd))
    subprocess.run(cmd, check=True)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    p.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR,
                   help="shared group/item catalog cache")
    p.add_argument("--tile-cache-dir", type=Path, default=DEFAULT_TILE_CACHE_DIR,
                   help="per-cell rendered RGBA tile cache (resumable)")
    p.add_argument("--resolution-m", type=float, default=960.0)
    p.add_argument("--cells", type=str, default=None,
                   help="comma-separated cell ids; for smoke tests")
    p.add_argument("--bbox", type=str, default=None,
                   help="minlon,minlat,maxlon,maxlat; subset of cells to render")
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--force", action="store_true",
                   help="re-render cells already in the tile cache")
    p.add_argument("--skip-mosaic", action="store_true",
                   help="only render/cache tiles; skip the final COG build "
                        "(for cheap iterative smoke runs)")
    args = p.parse_args()

    palette = load_palette()
    log(f"loaded {len(palette)}-entry palette from {PALETTE_JS.relative_to(ROOT)}")

    cells = discover_cells(args.cache_dir)
    cell_ids = args.cells.split(",") if args.cells else None
    bbox = tuple(float(x) for x in args.bbox.split(",")) if args.bbox else None
    cells = filter_cells(cells, cell_ids=cell_ids, bbox=bbox)
    if not cells:
        sys.exit("no cells matched --cells/--bbox")
    log(f"rendering {len(cells)} cell(s) at {args.resolution_m} m/px")

    def tile_path(cell) -> Path:
        return args.tile_cache_dir / cell.group_id / f"{cell.cell_id}.tif"

    to_render = [c for c in cells if args.force or not tile_path(c).exists()]
    log(f"{len(cells) - len(to_render)} cell(s) already cached, "
        f"{len(to_render)} to render")

    failed = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(render_cell, c, args.resolution_m, tile_path(c), palette): c
            for c in to_render
        }
        done = 0
        for fut in as_completed(futures):
            cell = futures[fut]
            done += 1
            try:
                ok = fut.result()
                if not ok:
                    log(f"  skip {cell.cell_id}: no usable DSM data")
            except Exception as exc:  # noqa: BLE001
                failed.append(cell.cell_id)
                log(f"  FAILED {cell.cell_id}: {exc}")
            if done % 25 == 0 or done == len(to_render):
                rate = done / max(time.time() - t0, 1e-6)
                log(f"  {done}/{len(to_render)} rendered ({rate:.2f}/s)")

    # Pick up any previously-cached tiles for cells we did not just render.
    rendered = sorted(set(tile_path(c) for c in cells if tile_path(c).exists()))
    log(f"{len(rendered)} tile(s) available for mosaic, {len(failed)} failed")

    if args.skip_mosaic:
        log("--skip-mosaic set; leaving cached tiles in place")
        return 1 if failed else 0

    if not rendered:
        sys.exit("no tiles rendered; nothing to mosaic")
    build_mosaic(rendered, args.output)

    lineage_path = args.output.with_suffix(".lineage.txt")
    lineage_path.write_text(lineage_text(args.resolution_m, len(palette)) + "\n")
    log(f"lineage written to {lineage_path}")
    log(f"wrote {args.output} ({args.output.stat().st_size / 1e6:.2f} MB)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
