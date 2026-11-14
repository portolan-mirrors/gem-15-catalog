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

One zoom -- ``--maxzoom`` -- reads from the source COGs. For each cell that
intersects the requested area, at that zoom:

1. Find every tile the cell's 1x1-degree bbox covers (``mercantile.tiles``),
   and the single bounding box of pixels across all of them, snapped
   exactly to tile boundaries.
2. Warp the cell's band-1 mosaic (via ``gdal.Warp``) straight onto that
   bounding pixel grid in EPSG:3857, in one call -- not once per tile.
   GDAL's automatic overview selection picks whichever internal overview
   (30/60/120/240/480 m) is closest to the zoom's resolution; **bilinear**
   resampling is used because every zoom here is a downsample from 15 m
   native data. A cell open, warp, or read that fails is retried in-process
   (``CELL_READ_ATTEMPTS``, beyond whatever retrying GDAL's own HTTP client
   already does -- see ``VSICURL_ENV``); if it still fails, that cell's
   tiles at this zoom are left unfinished, not filled with 0 (see "Fetch
   failures never bake in ocean" below).
3. Slice that one warped array into each tile's 256x256 window.

Tiles are filled, not derived from tile to tile at *this* zoom, so a tile
wholly inside one cell needs exactly one warp; a tile straddling two cells
(or two UTM zones -- the same case here, since a GEM-15 cell never crosses
a UTM zone internally) needs one warp per contributing cell: fill whichever
contributing cell has data at a pixel, any cell's own NaN losing to another
cell's real value at that pixel, 0 only once no cell covers it at all.

### ``--overview-mode``: how the other zooms are built

- ``average`` (default): every zoom shallower than ``--maxzoom`` is derived
  from the zoom one level deeper, already sitting in the working MBTiles --
  no further source reads at all. Each parent tile is a NaN-aware 2x2 box
  mean of its 4 children (a pixel that is void in all 4 contributing child
  pixels stays void; a pixel void in only some of them is the mean of the
  rest, not dragged toward 0 by the void ones -- this needs each tile's
  real pre-fill void mask, not just its already-0-filled elevation, which
  is why a compact packed-bit mask is stored alongside every tile's PNG,
  see ``void_masks`` below). Box-mean matches the DSM band's own mean
  aggregation semantics (DESIGN.md), and, criucially, this is the only mode
  that does not re-read the source COGs at every zoom: on a global build
  this is approximately where the egress estimate in RUNBOOK.md's old,
  rewarp-only design came from in the first place.
- ``rewarp``: the original design, kept as the A/B comparison. Every zoom
  independently warps straight from the source COGs, exactly like
  ``--maxzoom`` always does. No derivation, no box-averaging, no void
  masks needed for derivation (though they are still written, for
  consistency and because a later ``average``-mode run against the same
  MBTiles can then derive from any zoom this one already built).

### Fetch failures never bake in ocean

A cell whose source read fails (after in-process retries) must never be
treated as "this cell has no data" -- Google's bucket does occasionally
answer a transient HTTP failure (observed on the real build:
``HTTP response code: 0``), and GEM-15's own NaN-as-nodata convention means
a silently-empty read and a genuinely void cell are indistinguishable once
they reach the compositing step. So a cell's tiles are decremented in the
per-tile "cells remaining" count (see below) **only on success**. A tile
whose count never reaches zero this run is simply never written -- absent
from the MBTiles, not finalized as 0 m -- and a rerun picks it (and the
cell that failed it) straight back up, since ``existing_tiles_for_zoom``
only ever sees tiles that were actually written. There is deliberately no
"finalize whatever's left over at the end of the zoom" step any more: that
step is exactly what used to bake a transient failure in as permanent
ocean, because it ran unconditionally once the cell-fetch loop finished,
whether every contributing cell had actually succeeded or not.

### Exact-completion compositing, bounded memory and bounded concurrency

Before any raster I/O, ``build_zoom_rewarp`` counts, for every tile not yet
written, how many cells intersect it (pure bbox math, free). As cells are
processed that count is decremented **on success only**; a tile is
terrarium-encoded and written the moment its count reaches zero, then
dropped from memory.

Cell fetches are *not* all submitted to the thread pool at once. A fixed
in-flight window (``--window-multiplier`` x ``--workers``, default 2x) caps
how many cells' raw warped arrays (139-199 MB each at z13, measured) can be
sitting in memory waiting for the single compositing step to catch up.
Submitting all 13,485 cells' fetches up front (the original design) let
producer threads run 3-7x ahead of that one consumer, growing memory at
roughly 23 MB/s on the real run until it hit a 60 GiB cgroup cap. Bounding
the window fixes that independent of ``--workers``.

Encoding (terrarium + PNG, which spends real CPU time, unlike the mostly
memory-bound compositing step) runs on a *separate* thread pool
(``--encode-workers``) rather than inline on the thread that is also doing
compositing and submitting new fetches. PNG encoding in Pillow is a C
extension that releases the GIL during the actual zlib compression, so a
thread pool (not a process pool, which would add numpy-array pickling
overhead for no benefit here) genuinely parallelizes it.

## Per-zoom vertical quantization

Before encoding, elevation is rounded to a per-zoom step, following
Mapterhorn's technique (github.com/mapterhorn/mapterhorn,
``pipelines/utils.py::get_rounded_elevation_data``, read 2026-11-14):
``step(z) = min(2**(19 - z) / 256, 1.0)`` meters, i.e. finer than 1 m only
as zoom approaches Mapterhorn's own reference "full terrarium resolution"
zoom of 19, and capped at exactly 1 m everywhere at or below the zoom where
the formula would otherwise call for coarser-than-1 m rounding. Over this
script's z0-z14 range that step table is:

| zoom | 0-11 | 12 | 13 | 14 |
|---|---|---|---|---|
| step (m) | 1.0 | 0.5 | 0.25 | 0.125 |

Measured on this catalog's own Barcelona demo archive: -54.7% file size,
invisible to a consumer, since the step at every zoom stays at or below
that zoom's own meaningful resolution (and far below GEM-15's own several-
-meter DRMSE; see docs/research/gem15-paper-notes.md). ``--no-quantize``
disables this for an A/B comparison.

## Void-filling from Copernicus GLO-30 (terrain tiles only)

A GEM-15 masked void (water + a 9 m buffer, snow/ice, a >50 m GLO-30
outlier, or genuinely no data: see DESIGN.md) renders as a 0 m pit under
3D terrain -- confirmed, for the Sau reservoir northwest of Barcelona, to
be faithful rendering of real upstream nodata, not a pipeline bug (see
DESIGN.md's "Known issues" entry on this). By user decision 2026-11-14,
**terrain tiles only** (never the item COGs/STAC, never the overview COG,
which keeps its alpha-transparent voids) fill those pixels from Copernicus
GLO-30 instead of leaving them as a flat 0 m pit, on by default
(``--no-void-fill`` to get the old 0-fill-everything behavior back).

This happens once, at ``--maxzoom``, inside ``render_cell_block``, right
after that cell's own GEM-15 warp -- every zoom this script derives by
box-averaging (``--overview-mode average``, the default) inherits already
-filled pixels for free, with no special-casing in the derivation or
quantization code: a filled pixel just *is* a real elevation from that
point on, exactly like the GEM-15 pixel it replaced would have been.

1. **The cheap check** (per the design brief): do nothing until GEM-15's
   own warp for this cell actually has a NaN in it. No separate low-res
   probe is needed -- that warp already has to happen for the main
   render, so checking its own NaN mask costs nothing extra, and most
   cells (GEM-15 masks ~2.5% of cells, and more have *some* NaN, mostly
   coastline) never fetch GLO-30 at all.
2. **Fetch.** GLO-30 tiles on AWS Open Data are a strict 1x1-degree grid
   keyed by SW corner, named ``Copernicus_DSM_COG_10_{N|S}{lat:02d}_00_
   {E|W}{lon:03d}_00_DEM`` (verified against the GEM-15 cell for the Sau
   reservoir, N41/E002, which is the exact same 1x1-degree footprint as
   our own cell naming's south/west edges), over plain HTTPS,
   unauthenticated (``copernicus-dem-30m.s3.amazonaws.com``, confirmed
   with a real fetch). Per AWS's own dataset page: "ocean areas do not
   have tiles" there -- a cell with no land at all simply has no GLO-30
   tile to fetch, a 404, logged and skipped, not an error: GEM-15's own
   0-fill is also "flat sea" there, so there is nothing to improve.
3. **Vertical datum.** GLO-30 heights are orthometric on EGM2008
   (EPSG:3855); GEM-15 is EGM96 (EPSG:5773) -- see ``AGENTS.md``'s own
   "Quirks" entry on this for GEM-15 vs. ellipsoidal heights, a related
   but different conversion. Converted via
   ``pyproj.Transformer.from_crs("EPSG:4326+3855", "EPSG:4326+5773")``,
   which needs PROJ's ``us_nga_egm2008_1``/``us_nga_egm96_15`` grids
   (fetched over ``PROJ_NETWORK`` if not already cached locally).
   ``check_void_fill_prerequisites`` runs this once, at startup, before
   any build work, and exits with a clear message if it fails --
   deliberately never falls back to filling with an unconverted height.
   Measured at the Sau reservoir (2.40E, 41.97N): **+0.124 m**
   (EGM96 = EGM2008 + 0.124 m there). This is a per-cell constant
   (evaluated once, at the cell's bbox centroid, not per pixel): measured
   spread across the 4 Barcelona demo cells is -0.086 m to +0.584 m, so a
   single cell's own internal variation is well under that -- small next
   to GLO-30's own multi-meter accuracy budget, and not worth a per-pixel
   geoid grid evaluation for every filled cell.
4. **Resample and feather.** GLO-30 (30 m) is warped to the exact same
   EPSG:3857 grid as the GEM-15 cell block it is filling (same
   ``outputBounds``/``width``/``height``, bilinear), so no separate
   regridding step is needed. Blended toward GEM-15's own nearest valid
   value over ``VOID_FILL_FEATHER_PX`` (3) pixels at the void boundary
   (``scipy.ndimage.distance_transform_edt`` for the nearest-valid lookup
   and the blend weight) rather than dropped in with a hard edge: GLO-30
   and GEM-15 are independent measurements and can disagree by a few
   meters right at a shoreline, which a hard edge would show as a visible
   lip. Pixels more than 3 px from any real GEM-15 data are pure GLO-30
   (plus the geoid offset).
5. **If GLO-30 itself has no tile, or the fetch fails**, that cell's
   voids simply stay as GEM-15 left them (NaN, 0-filled downstream as
   before) -- logged, not treated as a cell failure (contrast with
   defect 1's GEM-15 fetch-failure handling: a missing *fill source* is
   "no improvement available," not "wrong data baked in," since GEM-15's
   own value, or its absence, is untouched either way).

**Attribution.** Per the Copernicus WorldDEM-30 licence, Article 6(b)
(modified-data notice; verified by reading the official licence PDF
directly, 2026-11-14 -- the quote below is its exact text, not a
paraphrase): the terrain PMTiles' own description must carry:

    produced using Copernicus WorldDEM-30 (c) DLR e.V. 2010-2014 and
    (c) Airbus Defence and Space GmbH 2014-2018 provided under
    COPERNICUS by the European Union and ESA; all rights reserved

See DESIGN.md for where this is recorded in the collection's own asset
contract.

## NaN, nodata, and the ocean convention

The source nodata (water, voids, masked cells: see DESIGN.md) is NaN. With
void-filling on (the default), most of it is now a real elevation from
GLO-30 by the time any zoom is encoded -- see above. What NaN reaches the
terrarium formula is only: void-fill disabled (``--no-void-fill``), no
GLO-30 tile existed for that cell (pure ocean, mostly), or the handful of
pixels where even GLO-30 has nothing. Every pixel still NaN after every
successfully-fetched contributing cell has been composited (and, when
enabled, after void-filling) is set to elevation 0 before encoding -- the
same convention AWS's terrain-tiles uses for ocean, since terrarium PNGs
have no nodata channel of their own. Negative real elevations (e.g. the
Dead Sea, about -430 m, or a GLO-30-filled void below mean sea level) are
ordinary input to the same formula and round-trip correctly (``--verify``
checks this): the encoding has no sign bit, ones's complement, or other
representation that treats negative values specially, it is just
``elevation + 32768`` before splitting into bytes, and 32768 is
comfortably larger than the deepest point on land.

``void_masks`` (below) now means "neither GEM-15 nor, if void-filling ran,
GLO-30 had data here" -- not "GEM-15 masked this." It no longer
distinguishes *why* a pixel is void (ocean vs. GEM-15's inland mask vs. no
cell at all): with filling on, inland voids mostly stop being void at all,
so the remaining cases are dominated by ocean and genuine no-coverage, and
splitting those further was not worth a second mask. ``--no-void-fill``
restores the original, unfilled meaning exactly.

## Precision

Terrarium is deterministic to 1/256 m (the B channel's fractional byte),
before the per-zoom quantization above coarsens that further at every zoom
except z13 itself. ``--verify``'s tolerance scales with each zoom's own
resolution (``RELATIVE_TOLERANCE``, ``RELATIVE_TOLERANCE_AVERAGED``); see
``verify_mbtiles_tile``'s docstring for why a fixed absolute tolerance does
not work here.

## Output format

An intermediate MBTiles (SQLite) is built first -- resumable tile-by-tile,
with standard ``tiles``/``metadata`` tables, a non-standard ``void_masks``
table this script's own derivation and ``--verify`` read back, ``format=
png``, ``type=baselayer``, and (also non-standard, but harmless)
``encoding=terrarium`` -- then converted with the ``pmtiles`` CLI
(``pmtiles convert``) to the final archive. Writing PMTiles directly was
not worth it here: resumability needs random-access upsert by (z,x,y),
which is exactly what SQLite already gives for free, and the ``pmtiles``
CLI's conversion is a single fast pass at the end.

``--db-path`` puts the working MBTiles (and its WAL/SHM files) under a
caller-chosen directory, independent of ``--output`` -- intended for
node-local scratch (RUNBOOK.md's Slurm shape uses ``/tmp`` on the compute
node), since SQLite's random-access inserts are latency-sensitive and a
shared/network filesystem is a suspected drag on them. ``--output`` itself
is unaffected and can still point at slower, shared storage: it is written
once, sequentially, at the very end, by ``pmtiles convert``.

## Sharded (``--bbox``/``--cells``) runs are standalone artifacts only

Adjacent cells can share up to 23 z13 tiles across a single 1-degree
boundary. A sharded run finalizes those boundary tiles using only the
cells it was given, which permanently bakes in "0 m" for whatever
neighboring cell was left out -- and a later full run sees those tiles as
already done and skips them. A sharded build is useful on its own (the
Barcelona demo archive is exactly this), but is never a valid way to
"pre-warm" a cache that a later global run will also write into. Never
point ``--mbtiles``/``--db-path``/``--output`` from a sharded run at paths
a global run will also use.
"""
from __future__ import annotations

import argparse
import io
import math
import os
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
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

# GDAL/CPL tuning for a remote, sparsely-accessed COG. Every value here is a
# *default*: applied with os.environ.get(key, default), so an operator's own
# exported value always wins and this script never clobbers a deliberate
# override. GDAL_HTTP_MAX_RETRY/RETRY_DELAY were added after the real build
# hit transient "HTTP response code: 0" failures from GCS; retrying at the
# HTTP-client layer (4 attempts, 1 s initial delay, GDAL backs off from
# there) eliminated all of them on the restart. This is a *first* line of
# defense; render_cell_block's own CELL_READ_ATTEMPTS is the second, for
# whatever still gets past GDAL's own retry.
VSICURL_ENV = {
    "GDAL_HTTP_MULTIPLEX": "YES",
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
    "VSI_CACHE": "TRUE",
    "VSI_CACHE_SIZE": "67108864",
    "GDAL_HTTP_TIMEOUT": "60",
    "GDAL_HTTP_MAX_RETRY": "4",
    "GDAL_HTTP_RETRY_DELAY": "1",
}


def apply_vsicurl_env() -> None:
    """Set VSICURL_ENV on GDAL, but only as a default: an operator's own
    exported environment variable of the same name always wins."""
    from osgeo import gdal

    for k, v in VSICURL_ENV.items():
        gdal.SetConfigOption(k, os.environ.get(k, v))


TILE_SIZE = 256

# This script's in-process retry for a cell whose source read still fails
# after GDAL's own HTTP-layer retry (VSICURL_ENV above) is exhausted.
CELL_READ_ATTEMPTS = 3
CELL_READ_BACKOFF_S = 2.0

# Mapterhorn's reference zoom for "full" (1/256 m) terrarium resolution; see
# quantize_elevation and the module docstring's step table.
FULL_RESOLUTION_ZOOM = 19

# Void-filling from Copernicus GLO-30 (see the module docstring's section
# on this). Public, unauthenticated HTTPS mirror on AWS Open Data; the 1x1
# degree tile naming is verified against the Sau reservoir cell (N41/E002)
# with a real fetch, 2026-11-14.
GLO30_BUCKET = "https://copernicus-dem-30m.s3.amazonaws.com"

# How many pixels of blend toward GEM-15's own nearest valid value at a
# void's boundary, before a filled pixel is pure GLO-30 (+ the geoid
# offset). See fill_voids_from_glo30.
VOID_FILL_FEATHER_PX = 3

# --verify's tolerance floor, in units of this tile's own meters/pixel, for
# a tile this script itself rewarped from source (true at every zoom in
# --overview-mode rewarp, and at --maxzoom in every mode). 2% was picked
# from the smoke test's own worst case: at z9 (~305.7 m/px), two
# independently-issued GDAL bilinear warps of the same steep-terrain pixel
# disagreed by up to ~2 m -- about 0.65% of that zoom's resolution. 2% keeps
# comfortable headroom above that for steeper terrain than the smoke test's
# one volcanic island, while a real pipeline bug (wrong band, a dropped
# +32768 offset, swapped channels) misses by orders of magnitude more than
# any resampling-kernel disagreement ever would. See verify_mbtiles_tile.
RELATIVE_TOLERANCE = 0.02

# --verify's tolerance for a tile this script instead *derived* by 2x2
# box-averaging (--overview-mode average, any zoom shallower than
# --maxzoom). verify_mbtiles_tile's ground truth is always a direct
# bilinear rewarp from source at that zoom's own resolution -- a different
# resampling philosophy from a box mean of the zoom below, which can
# legitimately disagree with a direct bilinear read by more than two
# bilinear reads disagree with each other, especially over textured
# terrain.
#
# Measured (not just reasoned), by building both --overview-mode average
# and rewarp over the same cell (w2s41, Barcelona) at z10-z13 and diffing
# every interior tile (edge tiles excluded -- a single-cell build's own
# edge tiles are a separate, much larger artifact: see the module
# docstring's sharded-runs warning, and do not use this constant to paper
# over that one). Max |average - rewarp|, as a fraction of each zoom's
# resolution, over interior tiles only:
#   z12 (one level below maxzoom): up to 69% (mean ~25%)
#   z11 (two levels below):        up to 18% (mean ~9%)
#   z10 (three levels below):      up to 11% (mean ~9%)
# Disagreement is largest immediately below --maxzoom, where the box mean
# is still averaging full-detail z13 data against a bilinear point-sample
# of that same highly-detailed area (one can catch a local peak the other
# smooths out); it shrinks at shallower zooms because both methods are by
# then looking at data the average chain has already smoothed once.
# 15% covers the typical case comfortably and most of the z12 outlier; an
# occasional flagged tile immediately below --maxzoom, on terrain steeper
# than this one Barcelona cell, is an accepted limitation of this check,
# not evidence the archive is wrong (a real pipeline bug misses by orders
# of magnitude more). RUNBOOK.md has the full measurement.
RELATIVE_TOLERANCE_AVERAGED = 0.15


class CellReadError(Exception):
    """A cell's source mosaic could not be read after in-process retries.

    Distinct from render_cell_block legitimately returning {} (a cell with
    no covering tiles at all, not an error): this must never be treated as
    "this cell has no data" by a caller, since GEM-15's own nodata
    convention (NaN) is indistinguishable from a silently-failed read once
    it would reach the compositing step. See the module docstring, "Fetch
    failures never bake in ocean".
    """


def glo30_url(west: float, south: float) -> str:
    """The Copernicus GLO-30 tile URL for the 1x1-degree cell whose SW
    corner is (west, south) -- the exact same corner GEM-15's own cell
    naming uses, verified against the Sau reservoir cell (bbox west=2,
    south=41 -> N41_00_E002_00, a real fetch confirmed 200 + matching
    bounds)."""
    w, s = int(round(west)), int(round(south))
    ns = "S" if s < 0 else "N"
    ew = "W" if w < 0 else "E"
    name = f"Copernicus_DSM_COG_10_{ns}{abs(s):02d}_00_{ew}{abs(w):03d}_00_DEM"
    return f"{GLO30_BUCKET}/{name}/{name}.tif"


_egm_transformer = None


def _get_egm_transformer():
    global _egm_transformer
    if _egm_transformer is None:
        from pyproj import Transformer

        _egm_transformer = Transformer.from_crs(
            "EPSG:4326+3855", "EPSG:4326+5773", always_xy=True
        )
    return _egm_transformer


def egm2008_to_egm96_offset(lon: float, lat: float) -> float:
    """Meters to ADD to a GLO-30 (EGM2008, EPSG:3855) orthometric height to
    get a GEM-15-comparable (EGM96, EPSG:5773) orthometric height, at
    (lon, lat). Raises if the PROJ geoid grids this needs
    (us_nga_egm2008_1, us_nga_egm96_15) cannot be loaded --
    check_void_fill_prerequisites calls this once at startup specifically
    so a missing grid fails loudly before any build work, never silently
    per-cell deep inside render_cell_block."""
    transformer = _get_egm_transformer()
    _, _, offset = transformer.transform(lon, lat, 0.0)
    if offset is None or not math.isfinite(offset):
        raise RuntimeError(
            f"EGM2008->EGM96 geoid conversion returned no usable result at "
            f"({lon}, {lat}) -- PROJ grids us_nga_egm2008_1/us_nga_egm96_15 "
            f"are unavailable (no PROJ_NETWORK and not cached locally?)"
        )
    return offset


def check_void_fill_prerequisites() -> None:
    """Fail loudly, once, before any build work, if the geoid conversion
    void-filling depends on is not actually usable. Never fill a void with
    an unconverted EGM2008 height -- see the module docstring."""
    try:
        offset = egm2008_to_egm96_offset(0.0, 0.0)
    except Exception as exc:  # noqa: BLE001 - this is the one place that must not retry/continue
        sys.exit(
            "--void-fill (the default) needs a working EGM2008->EGM96 "
            "geoid conversion (PROJ grids us_nga_egm2008_1 and "
            f"us_nga_egm96_15), and it failed: {exc}\n"
            "Fix PROJ_NETWORK/PROJ_DATA, pre-fetch the grids with "
            "`projsync --file us_nga_egm2008_1.tif --file "
            "us_nga_egm96_15.tif`, or pass --no-void-fill to keep the "
            "original 0-fill-everything behavior this run."
        )
    log(f"void-fill: EGM2008->EGM96 geoid conversion OK "
        f"(offset at 0N,0E = {offset:+.3f} m)")


def fill_voids_from_glo30(
    arr: np.ndarray,
    cell,
    left: float, bottom: float, right: float, top: float,
    width: int, height: int,
) -> np.ndarray:
    """Fill NaN in `arr` (already warped to the EPSG:3857 grid defined by
    left/bottom/right/top/width/height) from Copernicus GLO-30,
    geoid-corrected to EGM96 and feathered toward GEM-15's own nearest
    valid value near the void boundary. Returns `arr` unchanged if there
    is nothing to fill (the cheap check), or if GLO-30 has no tile or the
    fetch fails for this cell (logged, not an error -- see the module
    docstring's void-fill section)."""
    void = np.isnan(arr)
    if not void.any():
        return arr  # the cheap check: skip the GLO-30 fetch entirely

    from osgeo import gdal

    url = glo30_url(cell.bbox[0], cell.bbox[1])
    try:
        gdal.UseExceptions()
        apply_vsicurl_env()
        glo_ds = gdal.Open(f"/vsicurl/{url}")
        if glo_ds is None:
            raise RuntimeError("gdal.Open returned None")
        glo_warped = gdal.Warp(
            "", glo_ds, format="MEM", dstSRS="EPSG:3857",
            outputBounds=(left, bottom, right, top),
            width=width, height=height,
            resampleAlg="bilinear", errorThreshold=0.0,
            multithread=True,
        )
        if glo_warped is None:
            raise RuntimeError("gdal.Warp returned None")
        fill = glo_warped.GetRasterBand(1).ReadAsArray().astype(np.float32)
    except Exception as exc:  # noqa: BLE001 - no fill source is not a cell failure
        log(f"  WARNING: {cell.cell_id}: GLO-30 fill unavailable ({exc}); "
            f"voids in this cell stay 0-filled as before")
        return arr

    fill_void = np.isnan(fill)
    usable_fill = void & ~fill_void
    if not usable_fill.any():
        return arr  # GLO-30 warped to nothing usable here either

    offset = egm2008_to_egm96_offset(*cell.centroid)
    fill = fill + offset

    from scipy import ndimage

    if (~void).any():
        dist, (iy, ix) = ndimage.distance_transform_edt(void, return_indices=True)
        nearest_valid = arr[iy, ix]
        feather_w = np.clip(dist / VOID_FILL_FEATHER_PX, 0.0, 1.0)
        blended = feather_w * fill + (1.0 - feather_w) * nearest_valid
    else:
        blended = fill  # no real GEM-15 data anywhere in this block to feather toward

    out = arr.copy()
    out[usable_fill] = blended[usable_fill]
    # Pixels where neither GEM-15 nor GLO-30 has data (fill_void & void)
    # are left as NaN, same as before this feature existed.
    return out


def log(msg: str) -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------------
# terrarium codec
# --------------------------------------------------------------------------

def quantize_elevation(elevation_m: np.ndarray, z: int) -> np.ndarray:
    """Round elevation to this zoom's step, per Mapterhorn's technique (see
    module docstring). A no-op is also fine: pass --no-quantize through, or
    call with elevation already at the right precision."""
    factor = min(2.0 ** (FULL_RESOLUTION_ZOOM - z) / 256.0, 1.0)
    return (np.round(elevation_m / factor) * factor).astype(np.float32)


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


def pack_mask(void_mask: np.ndarray) -> bytes:
    """(256, 256) bool -> 8192 bytes, for the void_masks table."""
    return np.packbits(void_mask).tobytes()


def unpack_mask(data: bytes) -> np.ndarray:
    """Inverse of pack_mask."""
    return np.unpackbits(np.frombuffer(data, dtype=np.uint8)).astype(bool).reshape(TILE_SIZE, TILE_SIZE)


# --------------------------------------------------------------------------
# MBTiles
# --------------------------------------------------------------------------

def open_mbtiles(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: derive_zoom_from_child's worker threads each
    # call read_tile_and_mask on this same Connection concurrently (each
    # Connection.execute() call gets its own Cursor, so concurrent reads
    # are independent); every *write* in this script (write_tile, the
    # metadata/migration calls, commits) still only ever happens on the one
    # thread driving run_bounded's wait() loop, never inside a worker. WAL
    # mode below is exactly the mode that makes concurrent readers safe
    # alongside a single writer.
    conn = sqlite3.connect(path, timeout=60.0, check_same_thread=False)
    # WAL + synchronous=NORMAL: readers/writers don't block each other, and
    # a commit is a cheap WAL append rather than a full fsync of the main
    # file. On the real build, the only commit being once-per-zoom meant a
    # killed run rolled back everything since the last zoom boundary
    # (measured: 2.8 GB and 927 MB lost on two separate kills). Frequent
    # commits (see maybe_commit) plus WAL is what actually fixes that; this
    # pragma alone does not, but is required for the frequent-commit path
    # to not become its own bottleneck.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS tiles ("
        " zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER,"
        " tile_data BLOB,"
        " PRIMARY KEY (zoom_level, tile_column, tile_row))"
    )
    # Non-standard. Holds the pre-0-fill void mask for every tile this
    # script wrote (packed bits, 8192 bytes/tile), so --overview-mode
    # average can NaN-aware-average a zoom from the one below it without
    # re-deriving "was this really void or just coincidentally 0 m" from
    # already-filled data (impossible) or re-reading from source (defeats
    # the point of average mode). See the module docstring.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS void_masks ("
        " zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER,"
        " mask BLOB,"
        " PRIMARY KEY (zoom_level, tile_column, tile_row))"
    )
    conn.execute("CREATE TABLE IF NOT EXISTS metadata (name TEXT, value TEXT)")
    conn.commit()
    _migrate_metadata_table(conn)
    return conn


def _migrate_metadata_table(conn: sqlite3.Connection) -> None:
    """Give an existing metadata table a PRIMARY KEY(name) if it lacks one.

    The original schema had no constraint on `name` at all, so every build
    (and every `set_mbtiles_metadata` call within one build) appended a
    fresh row instead of replacing the old one -- "INSERT OR REPLACE" has
    nothing to conflict on without a key. Measured on the real build:
    duplicate rows on every rerun. A brand-new database created by this
    version of open_mbtiles never hits this path (the CREATE TABLE above
    already declares the key); this only matters for a database built by
    an earlier version of this script.
    """
    info = conn.execute("PRAGMA table_info(metadata)").fetchall()
    # table_info columns: (cid, name, type, notnull, dflt_value, pk)
    has_pk = any(row[1] == "name" and row[5] for row in info)
    if has_pk:
        return
    log("migrating metadata table: adding PRIMARY KEY(name), deduplicating")
    conn.execute(
        "CREATE TABLE metadata_migrated (name TEXT PRIMARY KEY, value TEXT)"
    )
    # Keep the latest value per name (highest rowid), dropping older
    # duplicates silently -- they are build metadata (bounds, minzoom,
    # description, ...), not tile data, so the most recent write is always
    # the one that matters.
    conn.execute(
        "INSERT INTO metadata_migrated (name, value) "
        "SELECT name, value FROM metadata WHERE rowid IN "
        "(SELECT max(rowid) FROM metadata GROUP BY name)"
    )
    conn.execute("DROP TABLE metadata")
    conn.execute("ALTER TABLE metadata_migrated RENAME TO metadata")
    conn.commit()


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


def write_tile(
    conn: sqlite3.Connection, z: int, x: int, y: int, data: bytes, void_mask: np.ndarray
) -> None:
    tile_row = ((1 << z) - 1) - y  # XYZ -> TMS row flip
    conn.execute(
        "INSERT OR REPLACE INTO tiles (zoom_level, tile_column, tile_row, tile_data) "
        "VALUES (?, ?, ?, ?)",
        (z, x, tile_row, data),
    )
    conn.execute(
        "INSERT OR REPLACE INTO void_masks (zoom_level, tile_column, tile_row, mask) "
        "VALUES (?, ?, ?, ?)",
        (z, x, tile_row, pack_mask(void_mask)),
    )


def read_tile_and_mask(conn: sqlite3.Connection, z: int, x: int, y: int):
    """(elevation float32 array, void_mask bool array), or None if absent."""
    tile_row = ((1 << z) - 1) - y
    row = conn.execute(
        "SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
        (z, x, tile_row),
    ).fetchone()
    if row is None:
        return None
    png = Image.open(io.BytesIO(row[0])).convert("RGB")
    elev = decode_terrarium(np.array(png)).astype(np.float32)

    mask_row = conn.execute(
        "SELECT mask FROM void_masks WHERE zoom_level=? AND tile_column=? AND tile_row=?",
        (z, x, tile_row),
    ).fetchone()
    if mask_row is not None:
        void = unpack_mask(mask_row[0])
    else:
        # A tile written before void_masks existed. Approximate: treat
        # exact 0 m as void. Slightly wrong at a real sea-level pixel, but
        # only for tiles from a database built by an earlier version of
        # this script; any tile this version writes always has a real mask.
        void = elev == 0.0
    return elev, void


class CommitScheduler:
    """Commits every `every_tiles` writes or `every_seconds`, whichever
    comes first -- not once per zoom. On the real build, the only commit
    being once-per-zoom meant a killed process rolled back everything since
    the last zoom boundary (measured: 2.8 GB and 927 MB lost on two kills).
    """

    def __init__(self, conn: sqlite3.Connection, every_tiles: int, every_seconds: float):
        self.conn = conn
        self.every_tiles = every_tiles
        self.every_seconds = every_seconds
        self.since_commit = 0
        self.last_commit = time.time()

    def tick(self, n: int = 1) -> None:
        self.since_commit += n
        if (self.since_commit >= self.every_tiles
                or time.time() - self.last_commit >= self.every_seconds):
            self.flush()

    def flush(self) -> None:
        if self.since_commit:
            self.conn.commit()
            self.since_commit = 0
            self.last_commit = time.time()


# --------------------------------------------------------------------------
# per-cell warp, with in-process retry
# --------------------------------------------------------------------------

def render_cell_block(
    cell, z: int, url: str, fill_voids: bool = True
) -> dict[tuple[int, int], np.ndarray]:
    """{(x,y): 256x256 float32 elevation array (may hold NaN)} for every
    tile the cell covers at zoom z.

    {} only when the cell has no covering tiles at all -- not an error.
    Raises CellReadError, after CELL_READ_ATTEMPTS, if the source could not
    be opened/warped/read; never returns {} for that case (see the module
    docstring, "Fetch failures never bake in ocean" -- the distinction is
    load-bearing for the caller).

    `fill_voids`: fill NaN from Copernicus GLO-30 before slicing into
    tiles (see the module docstring's void-fill section). On by default;
    a GLO-30 fetch failure for one cell never raises CellReadError -- a
    missing fill source leaves GEM-15's own data (or its absence)
    untouched, which is not the same failure mode as GEM-15 itself being
    unreachable.
    """
    from osgeo import gdal

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

    last_exc: Exception | None = None
    for attempt in range(1, CELL_READ_ATTEMPTS + 1):
        try:
            gdal.UseExceptions()
            apply_vsicurl_env()
            src_ds = gdal.Open(f"/vsicurl/{url}")
            if src_ds is None:
                raise CellReadError(f"gdal.Open returned None for {url}")
            band1 = gdal.Translate("", src_ds, format="VRT", bandList=[1])
            warped = gdal.Warp(
                "", band1, format="MEM", dstSRS="EPSG:3857",
                outputBounds=(left, bottom, right, top),
                width=width, height=height,
                resampleAlg="bilinear", errorThreshold=0.0, srcNodata="nan", dstNodata="nan",
                multithread=True,
            )
            if warped is None:
                raise CellReadError(f"gdal.Warp returned None for {url}")
            arr = warped.GetRasterBand(1).ReadAsArray().astype(np.float32)
            if fill_voids:
                arr = fill_voids_from_glo30(arr, cell, left, bottom, right, top, width, height)
            out = {}
            for t in tiles:
                col0 = (t.x - txmin) * TILE_SIZE
                row0 = (t.y - tymin) * TILE_SIZE
                out[(t.x, t.y)] = arr[row0: row0 + TILE_SIZE, col0: col0 + TILE_SIZE].copy()
            return out
        except Exception as exc:  # noqa: BLE001 - any failure here is retryable
            last_exc = exc
            if attempt < CELL_READ_ATTEMPTS:
                time.sleep(CELL_READ_BACKOFF_S * attempt)
    raise CellReadError(
        f"{cell.cell_id} z{z}: failed after {CELL_READ_ATTEMPTS} attempt(s): {last_exc}"
    ) from last_exc


def _fetch_cell_safe(cell, z: int, url: str, fill_voids: bool = True):
    """Never raises. Returns (cell, blocks) on success, (cell, None) on
    failure -- the sentinel a caller must check for before touching
    `remaining` (see build_zoom_rewarp)."""
    try:
        return cell, render_cell_block(cell, z, url, fill_voids=fill_voids)
    except CellReadError as exc:
        log(f"  WARNING: {cell.cell_id} z{z} failed, leaving its tiles "
            f"unfinished this run: {exc}")
        return cell, None
    except Exception as exc:  # noqa: BLE001 - a bug here must not kill the run
        log(f"  WARNING: {cell.cell_id} z{z} unexpected failure, leaving "
            f"its tiles unfinished this run: {exc}")
        return cell, None


# --------------------------------------------------------------------------
# bounded producer/consumer
# --------------------------------------------------------------------------

def run_bounded(items, submit, on_result, workers: int, window: int) -> None:
    """Run `submit(item)` on a `workers`-thread pool, calling `on_result(item,
    future)` on the (single) calling thread as each result lands, never
    holding more than `window` futures in flight at once.

    This is the fix for submitting all of a zoom's cell fetches up front
    (the original design): each result held 139-199 MB (measured, z13), and
    with every future submitted immediately, producer threads ran 3-7x
    ahead of the one consuming thread, growing RSS at ~23 MB/s on the real
    run until a 60 GiB cgroup cap killed it. window=2*workers (the default
    everywhere this is called) keeps memory bounded by a small, predictable
    multiple of one cell's array size instead of by the whole zoom's cell
    count.
    """
    with ThreadPoolExecutor(max_workers=workers) as pool:
        it = iter(items)
        in_flight: dict = {}

        def top_up() -> None:
            while len(in_flight) < window:
                try:
                    item = next(it)
                except StopIteration:
                    return
                in_flight[pool.submit(submit, item)] = item

        top_up()
        while in_flight:
            done, _ = wait(list(in_flight.keys()), return_when=FIRST_COMPLETED)
            for fut in done:
                item = in_flight.pop(fut)
                on_result(item, fut)
            top_up()


# --------------------------------------------------------------------------
# zoom build: rewarp from source
# --------------------------------------------------------------------------

def build_zoom_rewarp(
    z: int,
    cells,
    band: str,
    conn: sqlite3.Connection,
    workers: int,
    encode_workers: int,
    window_multiplier: int,
    force: bool,
    quantize: bool,
    committer: CommitScheduler,
    fill_voids: bool = True,
) -> tuple[int, int, int]:
    """Returns (written, already_done, failed_cell_count)."""
    done = set() if force else existing_tiles_for_zoom(conn, z)

    cell_tiles: dict = {}
    remaining: dict[tuple[int, int], int] = {}
    for cell in cells:
        tiles = [t for t in mercantile.tiles(*cell.bbox, [z]) if (t.x, t.y) not in done]
        if not tiles:
            continue
        cell_tiles[cell] = tiles
        for t in tiles:
            remaining[(t.x, t.y)] = remaining.get((t.x, t.y), 0) + 1

    if not remaining:
        log(f"z{z}: nothing to do ({len(done)} tile(s) already built)")
        return 0, len(done), 0

    log(f"z{z}: {len(remaining)} tile(s) to build from {len(cell_tiles)} "
        f"cell(s) ({len(done)} already built)")

    accum: dict[tuple[int, int], np.ndarray] = {}
    written = 0
    failed_cells = 0
    pending_encode: list = []
    t0 = time.time()
    done_n = 0

    void_cache: dict[tuple[int, int], np.ndarray] = {}

    with ThreadPoolExecutor(max_workers=encode_workers) as encode_pool:

        def drain_ready_encodes() -> None:
            nonlocal written
            still = []
            for ef, xy in pending_encode:
                if ef.done():
                    data = ef.result()
                    write_tile(conn, z, xy[0], xy[1], data, void_cache.pop(xy))
                    written += 1
                    committer.tick()
                else:
                    still.append((ef, xy))
            pending_encode[:] = still

        def submit_encode(xy: tuple[int, int], elev: np.ndarray, void: np.ndarray) -> None:
            if quantize:
                elev = quantize_elevation(elev, z)
            void_cache[xy] = void
            ef = encode_pool.submit(lambda e=elev: png_bytes(encode_terrarium(e)))
            pending_encode.append((ef, xy))

        def finalize(xy: tuple[int, int]) -> None:
            arr = accum.pop(xy, None)
            if arr is None:
                arr = np.full((TILE_SIZE, TILE_SIZE), np.nan, dtype=np.float32)
            void = np.isnan(arr)
            elev = np.where(void, 0.0, arr).astype(np.float32)
            submit_encode(xy, elev, void)

        def submit_fetch(cell):
            url = cell.dsm_url if band == "dsm" else cell.dtm_url
            return _fetch_cell_safe(cell, z, url, fill_voids=fill_voids)

        def on_result(cell, fut) -> None:
            nonlocal done_n, failed_cells
            done_n += 1
            try:
                _, blocks = fut.result()
            except Exception as exc:  # noqa: BLE001 - a future must never kill the run
                log(f"  WARNING: {cell.cell_id} z{z} future raised, leaving "
                    f"its tiles unfinished this run: {exc}")
                blocks = None

            if blocks is None:
                failed_cells += 1
                # Deliberately do NOT touch `remaining` for this cell's
                # tiles: they stay above zero and are simply never
                # finalized this run. See "Fetch failures never bake in
                # ocean" in the module docstring.
            else:
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

            drain_ready_encodes()
            if done_n % 50 == 0 or done_n == len(cell_tiles):
                rate = done_n / max(time.time() - t0, 1e-6)
                log(f"  z{z}: {done_n}/{len(cell_tiles)} cells ({rate:.2f}/s), "
                    f"{written} tile(s) written, {len(accum)} pending, "
                    f"{len(pending_encode)} encoding, {failed_cells} cell "
                    f"failure(s)")

        window = max(window_multiplier * workers, 1)
        run_bounded(list(cell_tiles.keys()), submit_fetch, on_result, workers, window)

        # Drain whatever encodes are still outstanding, blocking this time.
        for ef, xy in pending_encode:
            data = ef.result()
            write_tile(conn, z, xy[0], xy[1], data, void_cache.pop(xy))
            written += 1
            committer.tick()

    committer.flush()
    if remaining:
        log(f"  z{z}: {len(remaining)} tile(s) could not be completed this "
            f"run ({failed_cells} cell failure(s)); rerun to retry")
    return written, len(done), failed_cells


# --------------------------------------------------------------------------
# zoom build: derive from the zoom below (--overview-mode average)
# --------------------------------------------------------------------------

def _box_average_2x2(elev_super: np.ndarray, void_super: np.ndarray):
    """(512,512) elevation + void mask -> (256,256) NaN-aware 2x2 box mean
    + derived void mask (void only where all 4 contributing pixels were
    void)."""
    valid = (~void_super).astype(np.float32)
    elev_masked = np.where(void_super, 0.0, elev_super).astype(np.float32)

    def blocks(a):
        return a.reshape(TILE_SIZE, 2, TILE_SIZE, 2)

    valid_count = blocks(valid).sum(axis=(1, 3))
    elev_sum = blocks(elev_masked).sum(axis=(1, 3))
    with np.errstate(invalid="ignore", divide="ignore"):
        parent_elev = np.where(valid_count > 0, elev_sum / np.maximum(valid_count, 1), 0.0)
    parent_void = valid_count == 0
    return parent_elev.astype(np.float32), parent_void


def derive_zoom_from_child(
    z: int,
    child_z: int,
    conn: sqlite3.Connection,
    workers: int,
    force: bool,
    quantize: bool,
    committer: CommitScheduler,
) -> tuple[int, int]:
    """Build zoom z purely from the already-built zoom child_z (= z+1) in
    the same MBTiles -- no source reads, no network. Returns (written,
    already_done)."""
    done = set() if force else existing_tiles_for_zoom(conn, z)

    child_rows = conn.execute(
        "SELECT tile_column, tile_row FROM tiles WHERE zoom_level = ?", (child_z,)
    ).fetchall()
    n_child = 1 << child_z
    child_xy = {(col, (n_child - 1) - row) for col, row in child_rows}
    parents = {(cx // 2, cy // 2) for cx, cy in child_xy} - done

    if not parents:
        log(f"z{z}: nothing to derive from z{child_z} "
            f"({len(done)} tile(s) already built)")
        return 0, len(done)

    log(f"z{z}: deriving {len(parents)} tile(s) from z{child_z} "
        f"({len(done)} already built)")

    written = 0
    t0 = time.time()

    def read_child(cx: int, cy: int):
        return read_tile_and_mask(conn, child_z, cx, cy)

    def derive_one(parent) -> tuple:
        px, py = parent
        super_elev = np.zeros((2 * TILE_SIZE, 2 * TILE_SIZE), dtype=np.float32)
        super_void = np.ones((2 * TILE_SIZE, 2 * TILE_SIZE), dtype=bool)
        # Child placement, standard XYZ 2x2 pyramid relationship.
        for dx, dy, row_off, col_off in (
            (0, 0, 0, 0), (1, 0, 0, TILE_SIZE), (0, 1, TILE_SIZE, 0), (1, 1, TILE_SIZE, TILE_SIZE)
        ):
            child = read_child(2 * px + dx, 2 * py + dy)
            if child is None:
                continue  # missing child = void there; already the default
            c_elev, c_void = child
            super_elev[row_off:row_off + TILE_SIZE, col_off:col_off + TILE_SIZE] = c_elev
            super_void[row_off:row_off + TILE_SIZE, col_off:col_off + TILE_SIZE] = c_void
        elev, void = _box_average_2x2(super_elev, super_void)
        if quantize:
            elev = quantize_elevation(np.where(void, 0.0, elev), z)
        return parent, elev, void

    done_n = 0

    def on_result(parent, fut) -> None:
        nonlocal written, done_n
        done_n += 1
        parent_xy, elev, void = fut.result()
        write_tile(conn, z, parent_xy[0], parent_xy[1], png_bytes(encode_terrarium(elev)), void)
        written += 1
        committer.tick()
        if done_n % 500 == 0 or done_n == len(parents):
            rate = done_n / max(time.time() - t0, 1e-6)
            log(f"  z{z}: {done_n}/{len(parents)} tile(s) derived ({rate:.1f}/s)")

    # derive_one runs in run_bounded's own worker threads and only *reads*
    # from `conn` (read_tile_and_mask); on_result, which does the only
    # writing, runs on the thread driving run_bounded's wait() loop -- the
    # same single-writer/many-reader split open_mbtiles's WAL mode and
    # check_same_thread=False are set up for.
    window = max(4 * workers, 1)
    run_bounded(list(parents), derive_one, on_result, workers, window)

    committer.flush()
    return written, len(done)


# --------------------------------------------------------------------------
# verify
# --------------------------------------------------------------------------

def verify_mbtiles_tile(
    conn: sqlite3.Connection,
    z: int,
    x: int,
    y: int,
    url: str,
    tolerance_m: float,
    is_rewarped: bool,
    fill_voids: bool = False,
) -> bool:
    """Decode (z,x,y) from the MBTiles and compare a few pixels against a
    fresh, independent GDAL read+warp of the source COG at the same grid.

    `is_rewarped` says whether this exact tile was built by warping source
    data at this zoom (true at --maxzoom always, and at every zoom in
    --overview-mode rewarp) or derived by box-averaging a deeper zoom
    (--overview-mode average, any shallower zoom). The ground truth this
    check computes is always "what would a direct bilinear rewarp from
    source say at this zoom's resolution" -- for a rewarped tile that is
    the same computation the real build did, just issued independently
    (RELATIVE_TOLERANCE accounts for the two independent GDAL calls
    disagreeing slightly, see that constant); for a derived tile it is a
    genuinely different resampling philosophy (box mean of the zoom below
    vs. direct bilinear at this zoom), which can disagree by more even with
    no bug anywhere (RELATIVE_TOLERANCE_AVERAGED, wider, and measured, not
    just reasoned -- see that constant for the numbers).

    `fill_voids`: whether the archive was built with void-filling on. A
    pixel GEM-15 (hence this function's own `ref`, which is always a pure
    GEM-15 rewarp) has no data for can legitimately be non-zero in
    `decoded` when filling is on -- it is GLO-30, not GEM-15. This
    function cannot cross-check those values against GEM-15 (there is
    nothing there to check against) or against an independent GLO-30 fill
    (not implemented -- see the module docstring; this was a deliberate
    scope cut, not an oversight). Policy, instead: such pixels are
    excluded from the normal decoded-vs-ref comparison (they already were,
    incidentally, since the candidate selection below is driven by where
    `ref` has data), and a separate, looser sanity check runs over a
    sample of them -- finite, and within [-500, 9000] m (comfortably
    wider than any real elevation on Earth) -- to still catch a gross
    encoding bug (NaN leaking through, a sign error, a missing +32768)
    without needing to re-derive the whole fill pipeline a second time.
    """
    from osgeo import gdal

    gdal.UseExceptions()
    apply_vsicurl_env()

    found = read_tile_and_mask(conn, z, x, y)
    if found is None:
        log(f"verify: tile z{z}/{x}/{y} not found in MBTiles")
        return False
    decoded, _decoded_void = found

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

    # Sample from wherever ref actually has real source data, not a blind
    # grid over the whole tile. At shallow zooms one 256x256 tile can span
    # tens of degrees while the cell contributing to it is still only 1x1
    # degree, so a fixed-spacing grid can land entirely outside the cell's
    # tiny footprint and wrongly conclude "no data anywhere" when there is
    # some, just not where the grid looked. ref's own NaN mask is the
    # authoritative record of exactly which pixels in *this* tile the
    # source actually covers.
    nan_mask = np.isnan(ref)
    raw_valid = np.argwhere(~nan_mask)

    # Prefer "interior" pixels (not adjacent to a nodata pixel): right at a
    # nodata boundary, bilinear-with-partial-nodata resampling is
    # legitimately sensitive to exactly how GDAL chunks the warp, so two
    # independently-issued warp calls can disagree there by more than this
    # check's tolerance without either one being wrong.
    interior_mask = ~nan_mask
    interior_mask[0, :] = interior_mask[-1, :] = False
    interior_mask[:, 0] = interior_mask[:, -1] = False
    interior_mask[1:, :] &= ~nan_mask[:-1, :]
    interior_mask[:-1, :] &= ~nan_mask[1:, :]
    interior_mask[:, 1:] &= ~nan_mask[:, :-1]
    interior_mask[:, :-1] &= ~nan_mask[:, 1:]

    interior_valid = np.argwhere(interior_mask)
    candidates = interior_valid if len(interior_valid) else raw_valid
    diffs = []
    if len(candidates):
        n_samples = min(144, len(candidates))
        pick = np.linspace(0, len(candidates) - 1, n_samples).astype(int)
        for yy, xx in candidates[pick]:
            diffs.append(abs(float(decoded[yy, xx]) - float(ref[yy, xx])))

    # Void-fill sanity check: a sample of pixels ref (pure GEM-15) has no
    # data for, which can legitimately be non-zero in decoded when
    # fill_voids is on. Not cross-checked against any ground truth (see
    # the docstring) -- just finite and plausible, to still catch a gross
    # encoding bug.
    fill_sanity_ok = True
    fill_note = ""
    if fill_voids:
        fill_candidates = np.argwhere(nan_mask)
        if len(fill_candidates):
            n_fill = min(64, len(fill_candidates))
            fpick = np.linspace(0, len(fill_candidates) - 1, n_fill).astype(int)
            fill_vals = [float(decoded[yy, xx]) for yy, xx in fill_candidates[fpick]]
            bad = [v for v in fill_vals if not math.isfinite(v) or v < -500 or v > 9000]
            if bad:
                fill_sanity_ok = False
                fill_note = (f" | void-fill sanity FAIL: {len(bad)}/{len(fill_vals)} "
                             f"filled sample(s) implausible, e.g. {bad[0]:.1f} m")
            else:
                fill_note = (f" | void-fill sanity OK ({len(fill_vals)} filled "
                             f"sample(s), range [{min(fill_vals):.1f}, "
                             f"{max(fill_vals):.1f}] m, not cross-checked "
                             f"against ground truth -- see docstring)")

    if not diffs and is_rewarped and not fill_voids:
        # This tile was built the exact same way this reference is
        # computed (one bilinear point-sample from source), so if that
        # reference found nothing, the archive should show nothing either.
        # (Only valid with fill_voids off: with it on, a wholly-void-per-
        # -GEM-15 tile is expected to be GLO-30-filled, not 0, and that
        # case is handled below instead.)
        max_diff = float(np.abs(decoded).max())
        note = " (this cell has no source data anywhere in this tile; checked 0-fill instead)"
        effective_tolerance = tolerance_m
        ok = max_diff <= effective_tolerance
        log(f"verify: z{z}/{x}/{y} (rewarped): max sampled "
            f"|decoded - direct-read| = {max_diff:.4f} m "
            f"(tolerance {effective_tolerance:.4f} m) -> "
            f"{'OK' if ok else 'FAIL'}{note}")
        return ok
    elif not diffs:
        # A derived tile can be legitimately non-zero even where a single
        # bilinear point-sample at this zoom finds nothing: box-averaging
        # up through the pyramid can carry a sub-pixel-sized feature (e.g.
        # a small island) as a diluted-but-nonzero contribution, which is
        # exactly the representation advantage box-mean has over bilinear
        # point-sampling at extreme decimation -- the whole reason average
        # mode exists. A fill_voids-on rewarped tile with no ref data
        # anywhere is the analogous case for void-filling: GLO-30 may
        # have real data this check's pure-GEM-15 reference can't see.
        # Either way, this check's only "ground truth" (a direct GEM-15
        # rewarp) cannot resolve it, so it is inconclusive, not a failure
        # -- except for the void-fill sanity check, which still applies.
        log(f"verify: z{z}/{x}/{y} ({'rewarped' if is_rewarped else 'derived'}): "
            f"inconclusive -- a direct single-cell point-sample found no "
            f"source data at this zoom, but the archive may still "
            f"correctly carry data from "
            f"{'GLO-30 void-filling' if is_rewarped else 'box-averaging deeper zooms'}"
            f"; not treated as a failure{fill_note}")
        return fill_sanity_ok
    else:
        max_diff = max(diffs)
        kind = "interior" if len(interior_valid) else "boundary-adjacent"
        note = f" ({len(diffs)} {kind} sample(s) out of {len(raw_valid)} real-data pixel(s))"
        tile_resolution_m = (right - left) / TILE_SIZE
        relative = RELATIVE_TOLERANCE if is_rewarped else RELATIVE_TOLERANCE_AVERAGED
        effective_tolerance = max(tolerance_m, tile_resolution_m * relative)

    ok = (max_diff <= effective_tolerance) and fill_sanity_ok
    log(f"verify: z{z}/{x}/{y} ({'rewarped' if is_rewarped else 'derived'}): "
        f"max sampled |decoded - direct-read| = {max_diff:.4f} m "
        f"(tolerance {effective_tolerance:.4f} m) -> "
        f"{'OK' if ok else 'FAIL'}{note}{fill_note}")
    return ok


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--band", choices=["dsm", "dtm"], default="dsm")
    p.add_argument("--minzoom", type=int, default=0)
    p.add_argument("--maxzoom", type=int, default=13)
    p.add_argument("--overview-mode", choices=["average", "rewarp"], default="average",
                   help="average (default): derive every zoom but --maxzoom "
                        "by box-averaging the zoom below, no further source "
                        "reads. rewarp: warp every zoom from source, as the "
                        "original design did.")
    p.add_argument("--bbox", type=str, default=None,
                   help="minlon,minlat,maxlon,maxlat; subset of cells. "
                        "Produces a standalone archive only -- see the "
                        "module docstring's sharded-runs warning.")
    p.add_argument("--cells", type=str, default=None,
                   help="comma-separated cell ids; for smoke tests. Same "
                        "sharded-runs caveat as --bbox.")
    p.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    p.add_argument("--mbtiles", type=Path, default=None,
                   help="intermediate MBTiles path (default: output with "
                        ".mbtiles suffix, kept for resumability)")
    p.add_argument("--db-path", type=Path, default=None,
                   help="directory for the working MBTiles (+ WAL/SHM), "
                        "e.g. node-local NVMe scratch. Overrides where the "
                        "MBTiles lives but not --output; move/rsync the "
                        "finished --output file yourself. Ignored if "
                        "--mbtiles is also given.")
    p.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--encode-workers", type=int, default=None,
                   help="default: same as --workers")
    p.add_argument("--window-multiplier", type=int, default=2,
                   help="in-flight cell-fetch cap = this x --workers")
    p.add_argument("--commit-every-tiles", type=int, default=20_000)
    p.add_argument("--commit-every-seconds", type=float, default=60.0)
    p.add_argument("--no-quantize", action="store_true",
                   help="disable per-zoom vertical quantization (for A/B comparison)")
    p.add_argument("--no-void-fill", action="store_true",
                   help="disable filling GEM-15 voids from Copernicus "
                        "GLO-30 (the default); voids stay a flat 0 m, as "
                        "before this feature existed")
    p.add_argument("--force", action="store_true",
                   help="rebuild tiles already present in the MBTiles")
    p.add_argument("--skip-convert", action="store_true",
                   help="only update the MBTiles; skip pmtiles convert")
    p.add_argument("--verify", action="store_true",
                   help="after building, decode one tile per zoom and "
                        "compare against a direct source read")
    p.add_argument("--verify-tolerance-m", type=float, default=1.0)
    args = p.parse_args()

    encode_workers = args.encode_workers or args.workers
    quantize = not args.no_quantize
    fill_voids = not args.no_void_fill

    if fill_voids:
        check_void_fill_prerequisites()

    if args.bbox or args.cells:
        log("WARNING: --bbox/--cells produces a standalone archive only. "
            "Adjacent cells can share up to 23 z13 tiles per 1-degree "
            "boundary; this run will finalize those boundary tiles using "
            "only the cells it was given, baking in 0 m for whatever "
            "neighbor was left out. A later unrestricted run will see "
            "those tiles as already done and skip them. Do not point "
            "--mbtiles/--db-path/--output at paths a global run will also "
            "use.")

    cells = discover_cells(args.cache_dir)
    cell_ids = args.cells.split(",") if args.cells else None
    bbox = tuple(float(x) for x in args.bbox.split(",")) if args.bbox else None
    cells = filter_cells(cells, cell_ids=cell_ids, bbox=bbox)
    if not cells:
        sys.exit("no cells matched --cells/--bbox")
    log(f"{len(cells)} cell(s) selected, band={args.band}, "
        f"overview-mode={args.overview_mode}, quantize={quantize}, "
        f"fill_voids={fill_voids}, z{args.minzoom}-z{args.maxzoom}")

    if args.mbtiles:
        mbtiles_path = args.mbtiles
    elif args.db_path:
        mbtiles_path = args.db_path / args.output.with_suffix(".mbtiles").name
    else:
        mbtiles_path = args.output.with_suffix(".mbtiles")
    conn = open_mbtiles(mbtiles_path)
    committer = CommitScheduler(conn, args.commit_every_tiles, args.commit_every_seconds)

    west = min(c.bbox[0] for c in cells)
    south = min(c.bbox[1] for c in cells)
    east = max(c.bbox[2] for c in cells)
    north = max(c.bbox[3] for c in cells)
    description = f"GEM-15 {args.band.upper()} elevation, terrarium-encoded"
    attribution = "This dataset is produced by Google"
    if fill_voids:
        description += "; voids filled from Copernicus GLO-30"
        # Exact wording required by the Copernicus WorldDEM-30 licence,
        # Article 6(b) (the notice for *modified* data -- this is resampled,
        # geoid-shifted, and feather-blended, not redistributed as-is).
        # Verified by reading the licence PDF directly, 2026-11-14. See the
        # module docstring's void-fill section and DESIGN.md.
        attribution += (
            "; produced using Copernicus WorldDEM-30 (c) DLR e.V. "
            "2010-2014 and (c) Airbus Defence and Space GmbH 2014-2018 "
            "provided under COPERNICUS by the European Union and ESA; "
            "all rights reserved"
        )
    set_mbtiles_metadata(
        conn,
        name=f"gem-15-{args.band}-terrarium",
        format="png",
        type="baselayer",
        version="1.0.0",
        description=description,
        attribution=attribution,
        bounds=f"{west},{south},{east},{north}",
        minzoom=args.minzoom,
        maxzoom=args.maxzoom,
        encoding="terrarium",
        overview_mode=args.overview_mode,
        void_fill="glo30" if fill_voids else "none",
    )

    # verify_tiles: (z, x, y, url, is_rewarped)
    verify_tiles: list[tuple[int, int, int, str, bool]] = []

    def maybe_verify(z: int, built_or_already: bool, is_rewarped: bool) -> None:
        if not (args.verify and built_or_already):
            return
        sample_cell = cells[len(cells) // 2]
        sample_tiles = list(mercantile.tiles(*sample_cell.bbox, [z]))
        if sample_tiles:
            t = sample_tiles[len(sample_tiles) // 2]
            url = sample_cell.dsm_url if args.band == "dsm" else sample_cell.dtm_url
            verify_tiles.append((z, t.x, t.y, url, is_rewarped))

    total_written = 0
    total_failed_cells = 0

    if args.overview_mode == "rewarp":
        for z in range(args.minzoom, args.maxzoom + 1):
            written, already, failed = build_zoom_rewarp(
                z, cells, args.band, conn, args.workers, encode_workers,
                args.window_multiplier, args.force, quantize, committer,
                fill_voids=fill_voids,
            )
            total_written += written
            total_failed_cells += failed
            maybe_verify(z, bool(written or already), is_rewarped=True)
    else:
        written, already, failed = build_zoom_rewarp(
            args.maxzoom, cells, args.band, conn, args.workers, encode_workers,
            args.window_multiplier, args.force, quantize, committer,
            fill_voids=fill_voids,
        )
        total_written += written
        total_failed_cells += failed
        maybe_verify(args.maxzoom, bool(written or already), is_rewarped=True)

        for z in range(args.maxzoom - 1, args.minzoom - 1, -1):
            written, already = derive_zoom_from_child(
                z, z + 1, conn, args.workers, args.force, quantize, committer,
            )
            total_written += written
            maybe_verify(z, bool(written or already), is_rewarped=False)

    log(f"wrote {total_written} new tile(s) to {mbtiles_path} "
        f"({total_failed_cells} cell failure(s) total)")

    all_ok = True
    for z, x, y, url, is_rewarped in verify_tiles:
        ok = verify_mbtiles_tile(
            conn, z, x, y, url, args.verify_tolerance_m, is_rewarped, fill_voids=fill_voids
        )
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
    if total_failed_cells:
        log(f"NOTE: {total_failed_cells} cell failure(s) occurred; some "
            f"tiles may be missing or incomplete. Rerun the same command "
            f"to retry -- it will only redo what is not yet in the MBTiles.")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
