#!/usr/bin/env python3
"""Build terrarium-encoded raster PMTiles of the GEM-15 DSM (or DTM) for
MapLibre's ``raster-dem`` source.

    python3 tools/build_terrain.py                     # global, z0-13, dsm
    python3 tools/build_terrain.py --band dtm \\
        --output staging/publish/gem-15/terrain/gem-15-dtm-terrarium.pmtiles
    python3 tools/build_terrain.py --bbox=1.5,41.0,2.5,41.9 --minzoom 0 \\
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

``build_zoom_rewarp`` knows, for every tile, how many cells intersect it
(pure integer rect math, see ``_CellTileIndex``). As cells are processed
that count is decremented **on success only**; a tile is terrarium-encoded
and written the moment its count reaches zero, then dropped from memory.

#### Upfront cost scales with concurrency, not scope

That counting used to happen for the *entire* requested scope before any
raster I/O: one ``mercantile.Tile`` object per cell per tile
(``cell_tiles``) plus one dict entry per tile (``remaining``). Measured
against this catalog's real cell list at global z14 -- 13,485 cells,
42.138M cell-tile contributions over 40.753M distinct tiles -- that is
213.7 bytes per contribution, so **9.0 GB**, retained for the whole zoom,
before a single byte was read from GCS. The resume skip list cost another
151.6 bytes per already-built tile (6.2 GB for a complete zoom), and the
first derive step's child set cost the same again. None of it was visible
at the 99-cell slice scale every earlier validation of this script ran at,
because all of it is proportional to the *requested scope* rather than to
the concurrency the memory budget was sized for. The real z0-z14 global
run was OOM-killed at 1,300 of 13,485 cells inside a 200 GB limit whose
validated steady-state need was ~131 GiB.

So nothing proportional to the scope is materialized up front any more:

- a cell's tile set is four integers (``cell_tile_rect``), because
  ``mercantile.tiles`` over a bbox is exactly a rectangle;
- the per-tile contributor count is derived one cell at a time, as that
  cell is admitted, by intersecting integer rects
  (``_CellTileIndex.contributor_counts``) -- a few KB per cell, discarded
  with it;
- the resume skip list is a sorted numpy array of packed ``(x, y)`` keys,
  8 bytes per tile instead of 151.6 (``existing_tiles_for_zoom``), tested
  vectorized over one cell's tiles at a time;
- the derive stage turns each chunk of child rows straight into parent
  keys and never holds the child set at all.

Measured replacement cost for the same global z14 scope, not
extrapolated: **3.8 MB** of upfront structures (1.5 MB of it the cell
rects themselves), built in 0.2 s, plus 165.6 KB transient per in-flight
cell, plus the skip list -- 22 MB at the 2.78M tiles the killed run had
banked, 326 MB for a complete zoom.

What that leaves is the genuinely concurrency-bounded cost: the in-flight
cell window (below), and the open shared-boundary tiles. A tile only one
selected cell covers -- 96.7% of them at global z14, measured -- is
encoded straight off the arriving cell's array and never enters a dict at
all. The remaining 1.362M shared tiles are held only while some
contributing cell has delivered and another has not, which under the real
admission order peaks at **9,401 open tiles (2.46 GB)** and drains to
zero. That is bounded and reported in the per-zoom progress line
(``pending (peak N)``), not fixed by scope.

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

That pool's *input queue* is bounded too (``--encode-backlog-tiles``,
default ``max(2 x --encode-workers, 32)``), and it has to be: bounding
the fetch window only moves the problem one stage downstream. Each queued
tile pins a 256 KB quantized elevation array in its encode closure plus a
64 KB void mask, so **320 KiB per queued tile**, and a single cell's
compositing submits every tile only that cell covers -- about 2,900 of
3,022 at global z14 -- in one tight loop. At 32 workers the queue stayed
empty all run (one blip to 35). At **24** workers in dense terrain it
reached **24,868 tiles by cell 650 and was still climbing**: 7.6 GiB
pinned, unbounded, and visible as violent anonymous-memory oscillation as
the pool drained in bursts. Fewer fetch workers brings each warp in
sooner relative to encode throughput, and a high-entropy mountain PNG
takes far longer than the 5.3 ms a smooth smoke tile measured; both tip
the balance the same way.

The bound lives in ``submit_encode``, the one choke point every
``finalize()`` passes through, rather than at the call sites: a first
attempt placed it after the shared-tile branch and so missed the
``owed <= 1`` fast path carrying 96.7% of submissions, and the backlog
still reached 649 against a cap of 64. Measured with a deliberately slow
encoder over two adjacent cells at z13: the backlog holds at exactly the
cap where the unbounded path reaches 731, the output is byte-identical,
and the wall time is if anything slightly better, because this
back-pressure costs nothing a bounded fetch window was not already
paying.

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
   ``pyproj.Transformer.from_crs("EPSG:4326+3855", "EPSG:4326+5773",
   allow_ballpark=False)``, which needs PROJ's ``us_nga_egm08_25``/
   ``us_nga_egm96_15`` grids (fetched over ``PROJ_NETWORK`` if not already
   cached locally; verified against ``projsync --list-files``, 2026-11-15
   -- NOT ``us_nga_egm2008_1``, an earlier draft of this code guessed that
   name and it does not exist). ``allow_ballpark=False`` matters: without
   it, PROJ silently substitutes a "ballpark" (uncorrected, literal
   ``+proj=noop``) transformation when the real grids are unreachable,
   returning an exact, finite 0.0 m everywhere -- reproduced directly
   (grids hidden, ``PROJ_NETWORK=OFF``) and confirmed this would have
   passed the once-weaker "is it a finite number" check and shipped a void
   fill with silently zero geoid correction.
   ``check_void_fill_prerequisites`` runs this once, at startup, before
   any build work, and exits with a clear message if it fails -- checking
   the selected transformation itself (its ``description`` naming
   "ballpark", confirmed via ``projinfo -s EPSG:3855 -t EPSG:5773`` to be
   PROJ's own exact wording for this failure) and rejecting an exact 0.0 m
   result at the startup probe point as a second, belt-and-braces check,
   rather than trusting ``allow_ballpark=False`` alone -- deliberately
   never falls back to filling with an unconverted height.
   Measured at the Sau reservoir (2.40E, 41.97N): **+0.124 m**
   (EGM96 = EGM2008 + 0.124 m there; also measured at 0N,0E: **+0.064 m**,
   matching the server operator's own figure). This is a per-cell constant
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

## A fragment says that it is a fragment

Two things used to let an interrupted run pass for a finished one, and the
first global run hit both: the ``metadata`` table's ``minzoom``/``maxzoom``
were written once, at the start, from the *requested* ``--minzoom``/
``--maxzoom``, so the surviving 9.6% fragment advertised z0-z14 while
holding only z14 tiles; and the closing summary was a tile count and a
file size with nothing to compare them against, plus exit status 0, so the
job script promoted it as a completed archive.

Now ``minzoom``/``maxzoom`` always come from the zoom levels the ``tiles``
table actually holds (``sync_zoom_metadata``, run on every commit as well
as at the end, so a ``kill -9`` cannot stand a stale claim up), and
``log_build_summary`` closes every run with cells completed against cells
requested, zoom levels wanted against zoom levels built, per-zoom tile
counts read from the archive, and an explicit ``PARTIAL`` line plus exit
code ``EXIT_PARTIAL`` whenever the archive does not cover the request.
Promoting a fragment anyway is correct -- banking progress is the whole
point of a resumable build -- so the fix is that the artifact and the exit
status describe themselves, not that the run refuses to hand anything
over. A job script that promotes on purpose should capture the status
rather than die on it: RUNBOOK.md's sbatch shape shows the pattern.

## Output format

## The working database's own size

The ``void_masks`` row for a tile used to be a flat 8,192 packed bytes,
written for every tile in every mode. Measured on a real two-cell
z10-z13 build, 96.4% of masks (1,894 of 1,965) have no void pixel at
all, so nearly all of that was 8 KB spent to say "nothing here". At
global z14 the packed form costs **334 GB** of working database against
roughly 40 GB of finished archive, and it grows the database by
**24.2 MiB per processed cell**, terrain-independent. A uniform mask is
now a single byte (``pack_mask``), which takes that to about 12-15 GB.
``unpack_mask`` still reads the 8,192-byte form, so a database written by
an earlier build resumes with no migration and ends up holding both
forms.

This matters well beyond disk, and it is what killed the second global
run. Measured on the cluster at the moment of the kill, against a
200 GiB cap: cgroup ``memory.current`` ~200.0 GiB, of which ``file``
183.0 GiB, ``anon`` 16.3 GiB, ``shmem`` 0, with the process's own
``VmRSS`` at **7.65 GiB**. Under cgroup v2 a file's page cache counts
toward ``memory.max``, so the working database's cache was being charged
to the job while the process itself used under 17 GiB. The run's
trough-RSS fit climbed 19-27 MiB per processed cell; the database's own
growth at global tile sizes is 26.5-29.4 MiB per cell. Same number.

``--memory-log N`` exists so this is visible rather than inferred: it
reports the cgroup's anon/file/shmem split next to process RSS, the
database and WAL sizes, and the real ``.nbytes`` of the in-flight
containers. A resume also charges the inherited file all at once -- a
``cp`` plus ``sha256sum`` of a 176.5 GB database populated ~176 GB of
charged cache before a single tile was built -- so a job script should
use direct I/O for those steps or trim the cache after them.

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
import resource
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import NamedTuple

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
    # 416 Range Not Satisfiable is in this list because GCS started
    # answering it transiently for cells whose mosaic.tif serves ranges
    # perfectly well the rest of the time. Four Patagonian cells hit it on
    # one leg and two of them lost their tiles for that leg.
    #
    # GDAL does not retry 416 by default, and the message it raises
    # ("Range downloading not supported by this server") reads like a
    # permanent property of the server rather than a transient answer.
    # Verified by probe, not assumed, against GDAL 3.10.3: a server that
    # answers every ranged GET with 416 receives exactly 1 GET with the
    # default settings and 4 with this list.
    #
    # Setting this option REPLACES GDAL's default list rather than adding
    # to it, so the codes that already worked are restated here. Verified
    # the same way: 429, 500, 502, 503 and 504 each get the same number of
    # attempts with this list as with the default, and 404 still fails
    # fast, which matters because a missing GLO-30 tile is an ordinary
    # 404 and must stay cheap.
    "GDAL_HTTP_RETRY_CODES": "429,500,502,503,504,416",
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


#: The three ways to make the real geoid grids reachable, named in every
#: void-fill-prerequisite failure message so the operator does not have to
#: dig through this module to find them. Verified against the real grid
#: catalog with `projsync --list-files`, 2026-11-15: the world-coverage
#: EGM2008 grid is named ``us_nga_egm08_25.tif`` (80.5 MB) -- NOT
#: ``us_nga_egm2008_1.tif``, which does not exist and would make a fix
#: attempt based on it fail with a confusing "file not found".
VOID_FILL_GEOID_FIX_HINT = (
    "Fix one of: (1) PROJ_NETWORK=ON (streams grids over HTTPS on demand, "
    "no local copy needed); (2) pre-fetch them once with `projsync "
    "--file us_nga_egm08_25.tif --file us_nga_egm96_15.tif`; (3) "
    "PROJ_DATA pointed at a directory that already has them. Or pass "
    "--no-void-fill to keep the original 0-fill-everything behavior this "
    "run."
)


def _get_egm_transformer():
    global _egm_transformer
    if _egm_transformer is None:
        from pyproj import Transformer

        # allow_ballpark=False is the real fix: without it, PROJ silently
        # falls back to a "ballpark vertical transformation" (PROJ string
        # literally +proj=noop) whenever the real grid-based operation is
        # unavailable (grids missing, PROJ_NETWORK off) -- reproduced and
        # confirmed, 2026-11-15: with the grids hidden, the *default*
        # (allow_ballpark unset) transformer still constructs successfully
        # and returns an exact, finite 0.0 m everywhere, which passed the
        # old isfinite-only check below and would have shipped a void fill
        # with silently zero geoid correction. With allow_ballpark=False,
        # the same scenario instead raises ProjError at construction time
        # ("Bad step definition: proj=vgridshift (File not found or
        # invalid)"), which propagates out of here and is exactly the
        # loud, early failure check_void_fill_prerequisites wants.
        _egm_transformer = Transformer.from_crs(
            "EPSG:4326+3855", "EPSG:4326+5773", always_xy=True,
            allow_ballpark=False,
        )
        # Belt-and-braces, not the primary defense: if some other PROJ/
        # pyproj version ever lets construction succeed anyway with a
        # ballpark operation selected, catch it by inspecting the
        # transformation PROJ actually picked, not by a magic-value check
        # on its output. `projinfo -s EPSG:3855 -t EPSG:5773` confirms the
        # exact string this grids-unavailable case uses: "Transformation
        # from EGM2008 height to EGM96 height (ballpark vertical
        # transformation)".
        description = (_egm_transformer.description or "").lower()
        if "ballpark" in description:
            raise RuntimeError(
                "EGM2008->EGM96 geoid conversion selected a ballpark "
                "(uncorrected, approximate) transformation despite "
                f"allow_ballpark=False: {_egm_transformer.description!r}. "
                + VOID_FILL_GEOID_FIX_HINT
            )
    return _egm_transformer


def egm2008_to_egm96_offset(lon: float, lat: float) -> float:
    """Meters to ADD to a GLO-30 (EGM2008, EPSG:3855) orthometric height to
    get a GEM-15-comparable (EGM96, EPSG:5773) orthometric height, at
    (lon, lat). Raises if the PROJ geoid grids this needs
    (us_nga_egm08_25, us_nga_egm96_15) cannot be loaded, or if PROJ can
    only offer a ballpark (uncorrected) transformation for them --
    check_void_fill_prerequisites calls this once at startup specifically
    so either failure is loud before any build work, never silent per-cell
    deep inside render_cell_block."""
    transformer = _get_egm_transformer()
    _, _, offset = transformer.transform(lon, lat, 0.0)
    if offset is None or not math.isfinite(offset):
        raise RuntimeError(
            f"EGM2008->EGM96 geoid conversion returned no usable result at "
            f"({lon}, {lat}) -- PROJ grids us_nga_egm08_25/us_nga_egm96_15 "
            f"are unavailable. " + VOID_FILL_GEOID_FIX_HINT
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
            f"geoid conversion, and it failed: {exc}\n" + VOID_FILL_GEOID_FIX_HINT
        )
    # Belt-and-braces, specifically at this probe point: the measured true
    # offset at 0N,0E is +0.064 m (confirmed with working grids, 2026-11-15
    # -- matches the figure the server operator reported), never exactly
    # 0.0. An exact 0.0 here, even if `offset` is finite and even if
    # _get_egm_transformer's own ballpark check somehow missed it, is only
    # ever the ballpark fallback's signature (its PROJ string is a literal
    # no-op), not a genuine result.
    if offset == 0.0:
        sys.exit(
            "--void-fill (the default) needs a working EGM2008->EGM96 "
            "geoid conversion; the probe at 0N,0E returned exactly 0.0 m, "
            "which is not a real result (the true offset there is "
            "+0.064 m) -- this is the signature of PROJ's ballpark "
            "fallback slipping through. " + VOID_FILL_GEOID_FIX_HINT
        )
    log(f"void-fill: EGM2008->EGM96 geoid conversion OK "
        f"(offset at 0N,0E = {offset:+.3f} m)")


#: Max side of a window handed to a single distance_transform_edt(...,
#: return_indices=True) call inside _feather_toward_nearest_valid. Bounds
#: that call's transient allocation to roughly
#: (EDT_CHUNK_PX + 2*margin)^2 * (8 + 2*8) bytes (one float64 distance
#: array + two index arrays) regardless of how large or how sparse a
#: cell's void regions are -- see that function's docstring for the
#: memory-blowup history this replaced.
EDT_CHUNK_PX = 1024


def _feather_toward_nearest_valid(
    arr: np.ndarray, void: np.ndarray, fill: np.ndarray, feather_px: int
) -> np.ndarray:
    """Blend of `fill` and `arr`'s nearest valid (non-void) neighbor, for
    every void pixel: pure `fill` once `feather_px` pixels from the
    nearest valid pixel, ramping linearly toward that neighbor's own
    value closer in. Non-void pixels of the result are meaningless (the
    caller only ever reads this at void locations) and are not computed
    precisely by the padded-window remainder logic below; do not rely on
    them.

    The original implementation called
    ``scipy.ndimage.distance_transform_edt(void, return_indices=True)``
    once, over the *entire* cell block. Measured on the real z14 build:
    that one call allocates one float64 distance array plus two index
    arrays the same shape as `void` -- at z14's ~11,520x11,520 cell block,
    several GB transient, per filled cell, concurrently across every
    worker. 32 workers under a 96-150 GB cap OOM'd; only 8 workers were
    safe, at 0.0437 cells/s (85.7 h for the full z14 global build).

    Fix: tile the whole block into ``EDT_CHUNK_PX``-square chunks and run
    that same call once per chunk that actually has any void in it,
    reading a window padded by ``feather_px + 1`` pixels on every side (a
    valid pixel further than ``feather_px`` away only ever contributes a
    zero weight anyway, so the padding only needs to be wide enough to not
    miss one that is closer than that, including one belonging to a
    *different* chunk). Chunks with no void at all -- most of them, for a
    typical cell -- cost one cheap ``.any()`` check and nothing else, so a
    handful of small inland voids (the common case this fix targets) pay
    only for the few chunks they actually touch; a single huge void
    region (a mostly-ocean coastal cell, the degenerate case this also
    has to survive) still costs one bounded-size call per chunk rather
    than one call sized to the whole block, capping it the same way
    either way.

    An earlier version of this fix found void connected components first
    (``scipy.ndimage.label``) and chunked each component's own bounding
    box, on the reasoning that it would let a handful of small voids skip
    the empty area between them for free. Measured, that reasoning did
    not hold: ``ndimage.label`` itself allocates one full-block-sized
    output array no matter how little of the block is actually void,
    which measured as the dominant remaining cost for a sparse synthetic
    case (20 small 50x50 voids in an 11,520x11,520 block: labeling's own
    array is ~530 MB at that size, swamping the now-tiny per-chunk EDT
    cost it was supposed to help avoid). Plain chunking over the whole
    block, skipping empty chunks via ``.any()`` on an existing slice
    (no new array), has no analogous fixed cost and was simpler besides;
    this is why this function does not call ``ndimage.label``.

    Mathematically identical to the original whole-array call for every
    pixel that call's result could ever actually affect (anywhere the
    true nearest-valid distance is <= feather_px, the padded window is
    guaranteed to contain that exact nearest pixel, by construction of
    the padding); pixels whose true nearest-valid distance exceeds
    feather_px get weight 1.0 (pure `fill`) either way, whether or not the
    locally-visible "nearest" pixel this chunked version finds is the
    real global one -- confirmed bit-identical (down to the final
    float32 rounding both implementations apply) against the original on
    the real Sau reservoir cell, not just reasoned about.
    """
    blended = fill.copy()
    if not void.any():
        return blended
    if not (~void).any():
        return blended  # no valid pixel anywhere in this block to feather toward

    from scipy import ndimage

    margin = feather_px + 1
    H, W = void.shape

    for cr0 in range(0, H, EDT_CHUNK_PX):
        cr1 = min(cr0 + EDT_CHUNK_PX, H)
        for cc0 in range(0, W, EDT_CHUNK_PX):
            cc1 = min(cc0 + EDT_CHUNK_PX, W)
            chunk_void = void[cr0:cr1, cc0:cc1]
            if not chunk_void.any():
                continue  # cheapest possible skip: no new array, just a reduction

            # Padded *read* window: wide enough that any valid pixel
            # within feather_px of a void pixel in this chunk is visible
            # to this call's EDT, even one that belongs to a neighboring
            # chunk.
            pr0, pr1 = max(0, cr0 - margin), min(H, cr1 + margin)
            pc0, pc1 = max(0, cc0 - margin), min(W, cc1 + margin)
            win_void = void[pr0:pr1, pc0:pc1]
            if not (~win_void).any():
                continue  # window is void everywhere; nothing local to feather toward

            dist, (iy, ix) = ndimage.distance_transform_edt(win_void, return_indices=True)
            win_arr = arr[pr0:pr1, pc0:pc1]
            nearest_valid = win_arr[iy, ix]
            weight = np.clip(dist / feather_px, 0.0, 1.0)
            win_fill = fill[pr0:pr1, pc0:pc1]
            win_blend = weight * win_fill + (1.0 - weight) * nearest_valid

            # Write back only this chunk's own (unpadded) region.
            lr0, lr1 = cr0 - pr0, cr1 - pr0
            lc0, lc1 = cc0 - pc0, cc1 - pc0
            out_region = blended[cr0:cr1, cc0:cc1]
            out_region[chunk_void] = win_blend[lr0:lr1, lc0:lc1][chunk_void]

    return blended


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

    blended = _feather_toward_nearest_valid(arr, void, fill, VOID_FILL_FEATHER_PX)

    out = arr.copy()
    out[usable_fill] = blended[usable_fill]
    # Pixels where neither GEM-15 nor GLO-30 has data (fill_void & void)
    # are left as NaN, same as before this feature existed.
    return out


def log(msg: str) -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------------
# memory sampling (--memory-log)
# --------------------------------------------------------------------------

class MemorySampler:
    """Periodic, greppable memory telemetry for a long build.

    Exists because the global z14 run was killed twice at the same
    progress point with a floor that climbed 19-27 MiB per processed
    cell, and the tools usually reached for cannot see why:

    - ``tracemalloc`` does not account numpy buffers in this environment
      (measured: it reported under 2 MB while hundreds of MB of arrays
      were live), so it is the wrong instrument for the cell and tile
      arrays, though it is the right one for dict/list/bytes growth.
    - ``sys.getsizeof`` returns about 112 bytes for any ndarray, so a
      size sweep built on it reports nothing.

    What does work, and is what this samples: the process's own RSS, the
    cgroup's split of anonymous against file and shmem pages (the
    difference between "my heap grew" and "my files are being charged to
    me"), the working database's own size, explicit ``.nbytes`` sums over
    the containers that could retain arrays, the mapping count as an
    allocator-arena fingerprint, and a periodic ``malloc_trim`` probe
    whose RSS drop is a definitive verdict on allocator retention.

    **Every field carries its unit in its name.** A field ending
    ``_mib`` is mebibytes; one ending ``_tiles`` is a count of tiles;
    anything else is named in full. An earlier version emitted bare
    ``pending``, ``encoding`` and ``void_cache``, which are counts, next
    to ``*_mib`` fields, and they were read as sizes. The fields are:

    ``rss`` / ``rss_peak``
        Process resident size. ``rss_peak`` appears instead off Linux,
        where only a high-water mark is available.
    ``hwm``
        The process's own high-water RSS.
    ``cg_current`` / ``cg_anon`` / ``cg_file`` / ``cg_shmem``
        The cgroup's total charge and its split. ``cg_file`` climbing
        while ``cg_anon`` stays flat means the working database's page
        cache is being charged to the job, not the process's heap.
    ``db`` / ``wal`` / ``shm``
        The working database and its sidecars, on disk.
    ``warp_cell_mib``
        The block the cell just processed occupies. Rises with latitude.
    ``warp_window_mib``
        What every in-flight cell's block adds up to: the dominant
        allocation, and the one no container holds, so it is computed
        from the in-flight cells' own rects rather than measured. A cell
        that is void-filling holds several multiples of its block at
        once, so treat this as the floor of that term, not the ceiling.
    ``pending_tiles`` / ``pending_mib``
        Open shared boundary tiles and their real bytes.
    ``encoding_tiles`` / ``encoding_mib``
        Tiles queued for the encoder and the elevation arrays they pin.
        Bounded by ``--encode-backlog-tiles``.
    ``void_cache_tiles`` / ``void_cache_mib``
        Void masks held for queued tiles, and their real bytes.
    ``maps``
        VM mapping count. A count that climbs with throughput and never
        falls is the fingerprint of allocator arena growth.
    ``malloc_trim_freed``
        Every tenth sample. A large value means the allocator was
        holding the memory, not live objects.

    Every field is optional and every read is guarded: a platform without
    ``/proc`` (a laptop smoke test) still gets RSS through
    ``resource.getrusage`` and simply omits the rest.
    """

    #: One in this many samples also runs malloc_trim and reports the RSS
    #: it returned to the OS. The trim itself can take order 100 ms on a
    #: large heap, which is why it is not every sample.
    TRIM_EVERY = 10

    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path
        self.n = 0
        self._cgroup_stat = self._find_cgroup_stat()
        self._libc = None
        if sys.platform.startswith("linux"):
            try:
                import ctypes

                self._libc = ctypes.CDLL("libc.so.6", use_errno=True)
            except Exception:  # noqa: BLE001 - telemetry must never break a build
                self._libc = None

    # -- platform probes ---------------------------------------------------

    @staticmethod
    def _find_cgroup_stat() -> Path | None:
        """memory.stat for this process, cgroup v2 first, then v1."""
        try:
            for line in Path("/proc/self/cgroup").read_text().splitlines():
                parts = line.split(":", 2)
                if len(parts) == 3 and parts[0] == "0":
                    p = Path("/sys/fs/cgroup") / parts[2].lstrip("/") / "memory.stat"
                    if p.exists():
                        return p
        except Exception:  # noqa: BLE001
            pass
        for p in (Path("/sys/fs/cgroup/memory.stat"),
                  Path("/sys/fs/cgroup/memory/memory.stat")):
            if p.exists():
                return p
        return None

    @staticmethod
    def _proc_status_kb() -> dict[str, int]:
        out: dict[str, int] = {}
        try:
            for line in Path("/proc/self/status").read_text().splitlines():
                if line.startswith(("VmRSS:", "VmHWM:")):
                    k, v = line.split(":", 1)
                    out[k] = int(v.split()[0])
        except Exception:  # noqa: BLE001
            pass
        return out

    def _rss_bytes(self) -> tuple[int | None, bool]:
        """(resident bytes, is_current). Off Linux there is no /proc, and
        ``ru_maxrss`` is a high-water mark that never falls, so the caller
        labels it differently rather than passing a peak off as a current
        reading."""
        kb = self._proc_status_kb().get("VmRSS")
        if kb is not None:
            return kb * 1024, True
        try:
            v = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            # macOS reports bytes, Linux kilobytes; only reached off Linux.
            return (int(v) if sys.platform == "darwin" else int(v) * 1024), False
        except Exception:  # noqa: BLE001
            return None, False

    def _cgroup_fields(self) -> dict[str, int]:
        if self._cgroup_stat is None:
            return {}
        wanted = {"anon", "file", "shmem",          # v2
                  "total_rss", "total_cache", "total_shmem"}  # v1
        out: dict[str, int] = {}
        try:
            for line in self._cgroup_stat.read_text().splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[0] in wanted:
                    out[parts[0]] = int(parts[1])
            current = self._cgroup_stat.parent / "memory.current"
            if current.exists():
                out["cgroup_current"] = int(current.read_text().strip())
            else:
                usage = self._cgroup_stat.parent / "memory.usage_in_bytes"
                if usage.exists():
                    out["cgroup_current"] = int(usage.read_text().strip())
        except Exception:  # noqa: BLE001
            pass
        return out

    @staticmethod
    def _maps_count() -> int | None:
        """Number of VM mappings. A count that climbs with throughput is
        the fingerprint of glibc arena growth."""
        try:
            with open("/proc/self/maps") as fh:
                return sum(1 for _ in fh)
        except Exception:  # noqa: BLE001
            return None

    def _db_sizes(self) -> dict[str, int]:
        if self.db_path is None:
            return {}
        out: dict[str, int] = {}
        for suffix, key in (("", "db"), ("-wal", "wal"), ("-shm", "shm")):
            try:
                out[key] = (self.db_path.parent /
                            (self.db_path.name + suffix)).stat().st_size
            except OSError:
                pass
        return out

    def _malloc_trim(self) -> tuple[int | None, int | None]:
        """RSS before and after returning free heap to the OS. A large drop
        means the memory was held by the allocator, not by live objects."""
        if self._libc is None or not hasattr(self._libc, "malloc_trim"):
            return None, None
        before, _ = self._rss_bytes()
        try:
            self._libc.malloc_trim(0)
        except Exception:  # noqa: BLE001
            return before, None
        return before, self._rss_bytes()[0]

    # -- the sample itself -------------------------------------------------

    def sample(self, label: str, **extra) -> None:
        """Emit one ``memlog`` line. `extra` carries the caller's own
        container sizes, already summed (see rewarp_nbytes below)."""
        self.n += 1
        fields: list[str] = [label]

        rss, is_current = self._rss_bytes()
        if rss is not None:
            fields.append(f"{'rss' if is_current else 'rss_peak'}="
                          f"{rss / 2**20:.0f}MiB")
        hwm = self._proc_status_kb().get("VmHWM")
        if hwm is not None:
            fields.append(f"hwm={hwm / 1024:.0f}MiB")

        cg = self._cgroup_fields()
        for key, name in (("cgroup_current", "cg_current"),
                          ("anon", "cg_anon"), ("file", "cg_file"),
                          ("shmem", "cg_shmem"), ("total_rss", "cg_rss"),
                          ("total_cache", "cg_cache"),
                          ("total_shmem", "cg_shmem")):
            if key in cg:
                fields.append(f"{name}={cg[key] / 2**20:.0f}MiB")

        for key, value in self._db_sizes().items():
            fields.append(f"{key}={value / 2**20:.0f}MiB")

        for key, value in extra.items():
            if isinstance(value, float):
                fields.append(f"{key}={value:.1f}")
            else:
                fields.append(f"{key}={value}")

        maps = self._maps_count()
        if maps is not None:
            fields.append(f"maps={maps}")

        if self.n % self.TRIM_EVERY == 0:
            before, after = self._malloc_trim()
            if before is not None and after is not None:
                fields.append(
                    f"malloc_trim_freed={(before - after) / 2**20:.0f}MiB")

        log("memlog " + " ".join(fields))


def arrays_nbytes(values) -> int:
    """Total buffer bytes of the numpy arrays in `values`, skipping
    anything that is not an array. The only reliable way to size numpy
    retention: ``sys.getsizeof`` reports about 112 bytes per array
    regardless of its buffer."""
    total = 0
    for v in values:
        arr = getattr(v, "nbytes", None)
        if isinstance(arr, int):
            total += arr
    return total


# --------------------------------------------------------------------------
# terrarium codec
# --------------------------------------------------------------------------

def quantization_step_m(z: int) -> float:
    """This zoom's vertical quantization step in meters, per Mapterhorn's
    technique (see the module docstring's step table): 1 m at z0-z11,
    0.5 m at z12, 0.25 m at z13, 0.125 m at z14."""
    return min(2.0 ** (FULL_RESOLUTION_ZOOM - z) / 256.0, 1.0)


def quantize_elevation(elevation_m: np.ndarray, z: int) -> np.ndarray:
    """Round elevation to this zoom's step, per Mapterhorn's technique (see
    module docstring). A no-op is also fine: pass --no-quantize through, or
    call with elevation already at the right precision."""
    factor = quantization_step_m(z)
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


# WebP's "method" (0-6, slower=more effort) tuned against the smoke cell's
# own Sau tile, measured 2026-11-16 (lossless, quality=100 either way --
# quality only affects *lossy* WebP):
#   format   method   bytes    vs PNG    encode time
#   PNG      --       61,470   --        5.3 ms
#   WebP     0       102,188   +66.2%    11.7 ms  (worse than PNG outright)
#   WebP     1        45,092   -26.6%    26.0 ms
#   WebP     2        44,862   -27.0%    32.9 ms
#   WebP     3        44,484   -27.6%    48.4 ms
#   WebP     4        43,944   -28.5%    204.6 ms
#   WebP     6        43,944   -28.5%    1854.5 ms (max effort buys nothing
#                                         4 and 6 didn't already have)
# method=1 captures 93% of the max available size reduction (26.6/28.5) at
# 1/8 method=4's encode time -- picked as the default specifically because
# encode time was the other half of this same report's memory/concurrency
# fix: slower encodes mean a deeper encode-pool backlog (more
# `(xy, future)` pairs and their referenced arrays held in
# `pending_encode`/`void_cache` at once) for the same throughput, the
# opposite of what that fix was for.
WEBP_LOSSLESS_METHOD = 1


def encode_tile_bytes(rgb: np.ndarray, tile_format: str) -> bytes:
    buf = io.BytesIO()
    if tile_format == "webp":
        Image.fromarray(rgb, mode="RGB").save(
            buf, format="WEBP", lossless=True, quality=100,
            method=WEBP_LOSSLESS_METHOD,
        )
    else:
        Image.fromarray(rgb, mode="RGB").save(buf, format="PNG")
    return buf.getvalue()


#: Exact length of a fully-packed 256x256 mask: the legacy (and still the
#: mixed-case) representation. No other length is ever written as a packed
#: mask, which is what makes the one-byte sentinels below unambiguous
#: without a version field.
MASK_PACKED_BYTES = TILE_SIZE * TILE_SIZE // 8  # 8192

#: One-byte stand-ins for the two uniform masks, which is nearly every
#: mask. Measured on a real two-cell z10-z13 build: 1,894 of 1,965 tiles
#: (96.4%) have no void pixel at all, and a packed mask spends 8,192 bytes
#: to say so. At global z14 (40.753M tiles) the packed form costs
#: **334 GB** of working database against roughly 40 GB of actual tiles,
#: and it grows the database by 24.2 MiB per processed cell independently
#: of terrain. The sentinels take that to about 12-15 GB.
#:
#: The byte values are not arbitrary: b"\x00" is what the first byte of a
#: packed no-void mask already is, and b"\xff" likewise for all-void, so a
#: reader that mistook a sentinel for a truncated packed mask would still
#: read the right bit pattern for the part it saw.
MASK_SENTINEL_NO_VOID = b"\x00"
MASK_SENTINEL_ALL_VOID = b"\xff"


def pack_mask(void_mask: np.ndarray) -> bytes:
    """(256, 256) bool -> the void_masks blob: one byte for a uniform mask,
    otherwise 8192 packed bytes."""
    if not void_mask.any():
        return MASK_SENTINEL_NO_VOID
    if void_mask.all():
        return MASK_SENTINEL_ALL_VOID
    return np.packbits(void_mask).tobytes()


def unpack_mask(data: bytes) -> np.ndarray:
    """Inverse of pack_mask, and of every earlier version of pack_mask.

    A blob written by an older build of this script is always exactly
    ``MASK_PACKED_BYTES`` long, so length alone distinguishes the two
    forms and a mixed database (legacy rows plus sentinel rows, which is
    exactly what resuming the banked global archive produces) reads
    correctly with no migration and no rewrite of the rows already there.
    """
    if len(data) == 1:
        if data == MASK_SENTINEL_NO_VOID:
            return np.zeros((TILE_SIZE, TILE_SIZE), dtype=bool)
        if data == MASK_SENTINEL_ALL_VOID:
            return np.ones((TILE_SIZE, TILE_SIZE), dtype=bool)
        raise ValueError(f"unknown one-byte void mask sentinel {data!r}")
    if len(data) != MASK_PACKED_BYTES:
        raise ValueError(
            f"void mask blob is {len(data)} bytes; expected 1 (sentinel) "
            f"or {MASK_PACKED_BYTES} (packed)"
        )
    return np.unpackbits(np.frombuffer(data, dtype=np.uint8)).astype(bool).reshape(TILE_SIZE, TILE_SIZE)


# --------------------------------------------------------------------------
# tile keys and cell tile rects
#
# The compact replacements for the per-tile Python objects the per-zoom
# setup used to materialize for the whole requested scope. See the module
# docstring's "Upfront cost scales with concurrency, not scope" section for
# the measured numbers these exist for.
# --------------------------------------------------------------------------

#: Packed (x, y) tile key: x in the high 32 bits of an int64. Tile indices
#: reach 2^14 at z14 and 2^30 at z30, so 32 bits per axis is ample. Every
#: structure that used to hold one (x, y) tuple per tile holds one of these
#: in a numpy array instead.
_XY_SHIFT = 32
_XY_MASK = (1 << _XY_SHIFT) - 1

#: Rows pulled from the `tiles` table per fetchmany while packing keys.
#: Bounds the transient list-of-tuples each step allocates to roughly
#: 28 MB regardless of how many tiles the zoom holds.
_TILE_SCAN_CHUNK = 250_000


def pack_xy(x: int, y: int) -> int:
    return (int(x) << _XY_SHIFT) | int(y)


def unpack_xy(key: int) -> tuple[int, int]:
    return int(key) >> _XY_SHIFT, int(key) & _XY_MASK


def cell_tile_rect(bbox, z: int) -> tuple[int, int, int, int]:
    """``(xmin, xmax, ymin, ymax)`` of ``mercantile.tiles(*bbox, [z])``.

    ``mercantile.tiles`` yields the *complete* rectangle
    ``range(ul.x, lr.x + 1) x range(ul.y, lr.y + 1)`` for one bbox (read
    from mercantile 1.2.1's own source), so these four integers carry
    exactly the same information as the materialized tile list -- 4 ints
    per cell instead of one ``Tile`` object per tile, which is the whole
    point: at global z14 the materialized form measured 213.7 bytes per
    cell-tile contribution, 9.0 GB over the 42.1M contributions of the
    full scope.

    This reproduces mercantile's own computation -- its clamps, and its
    ``LL_EPSILON`` nudge on the south/east edges -- rather than
    re-deriving the tile/degree relationship independently. That matters
    more than it looks: an off-by-one against mercantile here either
    leaves a tile's contributor count permanently above zero (the tile is
    then never written at all) or drops a contributor (the shared-boundary
    seam bug this catalog has already paid for once -- see the module
    docstring's sharded-runs warning). Verified equal to ``min``/``max``
    over the real ``mercantile.tiles`` output for 409 real cells x
    z{0,5,9,13,14}, 0 mismatches, and ``tests`` in this repo keep that
    check.

    A bbox with ``west > east`` (antimeridian-crossing, which
    ``mercantile.tiles`` handles by splitting into two bboxes) is not a
    rectangle in tile space and is rejected rather than silently
    mis-counted. No GEM-15 cell crosses the antimeridian: a cell id
    always names its own west edge and the east edge is west + 1.
    """
    west, south, east, north = bbox
    if west > east:
        raise ValueError(
            f"cell_tile_rect does not support an antimeridian-crossing bbox "
            f"{bbox!r}: its tiles are not one rectangle"
        )
    w = max(-180.0, west)
    s = max(-85.051129, south)
    e = min(180.0, east)
    n = min(85.051129, north)
    ul = mercantile.tile(w, n, z)
    lr = mercantile.tile(e - mercantile.LL_EPSILON, s + mercantile.LL_EPSILON, z)
    return ul.x, lr.x, ul.y, lr.y


def cell_block_bytes(rect: tuple[int, int, int, int]) -> int:
    """Bytes in one cell's warped float32 block at this rect.

    This is the single largest allocation in the build and the one the
    memory log could not see: it lives in a fetch worker's frame, not in
    any container the compositing thread holds. It grows sharply with
    latitude, because Web Mercator packs more tile rows into a degree the
    further from the equator you go -- about 0.55 GiB per cell at the
    equator against 4.64 GiB at 83N.
    """
    x0, x1, y0, y1 = rect
    return (x1 - x0 + 1) * (y1 - y0 + 1) * TILE_SIZE * TILE_SIZE * 4


def rect_keys(rect: tuple[int, int, int, int]) -> np.ndarray:
    """Packed tile keys for every tile in `rect`, shaped (nx, ny) so a
    caller can index it by ``(x - xmin, y - ymin)``."""
    x0, x1, y0, y1 = rect
    xs = np.arange(x0, x1 + 1, dtype=np.int64)
    ys = np.arange(y0, y1 + 1, dtype=np.int64)
    return (xs[:, None] << _XY_SHIFT) | ys[None, :]


def keys_present(sorted_keys: np.ndarray, keys: np.ndarray) -> np.ndarray:
    """Boolean mask, shaped like `keys`: which of `keys` appear in
    `sorted_keys` (which must be sorted and unique, as
    ``existing_tiles_for_zoom`` returns)."""
    if sorted_keys.size == 0:
        return np.zeros(keys.shape, dtype=bool)
    pos = np.searchsorted(sorted_keys, keys)
    np.clip(pos, 0, sorted_keys.size - 1, out=pos)
    return sorted_keys[pos] == keys


# --------------------------------------------------------------------------
# MBTiles
# --------------------------------------------------------------------------

def open_mbtiles(path: Path) -> sqlite3.Connection:
    """The single WRITER connection for `path`. Every write in this script
    (write_tile, the metadata/migration calls, commits, and the
    existing_tiles_for_zoom/child-row reads that happen before any worker
    pool starts) runs on this connection, and only ever from the one
    thread that drives main()'s/build_zoom_rewarp's/
    derive_zoom_from_child's top-level control flow -- never from inside
    a ThreadPoolExecutor worker. Worker threads that need to *read* this
    same database (derive_zoom_from_child's derive_one, running in
    run_bounded's pool) must use get_read_connection(path) instead of this
    connection -- see that function for why a previous version of this
    comment's claim ("check_same_thread=False plus WAL makes concurrent
    use of one Connection safe") was wrong, confirmed by a real
    sqlite3.InterfaceError ("bad parameter or other API misuse") on the
    real build once derive mode's worker threads actually exercised it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
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
    # script wrote (one byte for a uniform mask, else 8192 packed bytes
    # -- see pack_mask), so --overview-mode
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
    # PRIMARY KEY(name) is what makes set_mbtiles_metadata's "INSERT OR
    # REPLACE" replace rather than append. _migrate_metadata_table adds it
    # to a database that predates it; declaring it here means a database
    # this version creates does not need migrating at all, which is what
    # that function's docstring has always said and what this CREATE did
    # not actually do (every fresh build logged a migration it should not
    # have needed).
    conn.execute(
        "CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, value TEXT)"
    )
    conn.commit()
    _migrate_metadata_table(conn)
    return conn


# One sqlite3.Connection object must never be called concurrently from more
# than one thread -- not even with check_same_thread=False, which only
# lifts Python's own "created on thread X, used on thread X" restriction,
# and not even in WAL mode, which is about multiple *separate* connections
# not blocking each other, not about one connection's internal cursor/
# statement state being safe to touch from two threads at once. A previous
# version of this script got this specific thing wrong (see open_mbtiles's
# docstring) and derive_zoom_from_child's worker threads, all calling
# read_tile_and_mask on the one shared writer connection, hit exactly the
# failure this causes on the real build: sqlite3.InterfaceError("bad
# parameter or other API misuse"), nondeterministically, only once derive
# mode's worker pool actually ran (never exercised before -- the only
# previous builds were either --overview-mode rewarp, which has no derive
# step, or predated average mode entirely).
#
# Fix: every *read* a worker thread issues goes through its own private,
# read-only connection instead, opened lazily per (thread, path) here. A
# read-only connection can never be the source of a write/read race in the
# first place (SQLite refuses writes on it outright), and giving each
# thread its own object sidesteps the single-connection-concurrency rule
# entirely rather than trying to work around it. The one WRITER connection
# (open_mbtiles's return value) is untouched by this and remains exactly
# as before: used only by the single thread driving each zoom's top-level
# control flow, never from inside a worker.
_read_connections = threading.local()


def get_read_connection(path: Path) -> sqlite3.Connection:
    """A read-only sqlite3 connection to `path`, private to the calling
    thread, created on first use and reused for the rest of that thread's
    life. See the module-level note above this function for why a
    dedicated read-only connection per thread, not the shared writer
    connection, is required here."""
    cache = getattr(_read_connections, "by_path", None)
    if cache is None:
        cache = {}
        _read_connections.by_path = cache
    conn = cache.get(path)
    if conn is None:
        # uri=True + "mode=ro": SQLite enforces this at the OS/VFS level
        # (the file is opened without write access), not just by
        # convention -- an accidental write attempt on this connection
        # raises rather than silently mutating the database a worker
        # thread should never be touching.
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60.0)
        cache[path] = conn
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


def check_tile_format_resume_guard(
    conn: sqlite3.Connection, requested_format: str, path: Path
) -> None:
    """Refuse to resume into an existing MBTiles built with a different
    --tile-format. The `tiles` table's BLOB column carries no per-row tag
    saying which codec its bytes are in -- only the one archive-wide
    `format` metadata value does -- so mixing formats across a resumed
    build would produce an archive that silently has some rows a reader
    cannot actually decode as whatever `format` claims for the whole
    thing. Checked before set_mbtiles_metadata would otherwise overwrite
    the old `format` value with the new one and hide the mismatch
    entirely.
    """
    row = conn.execute("SELECT value FROM metadata WHERE name='format'").fetchone()
    if row is None:
        return  # a brand-new database; nothing to conflict with
    existing_format = row[0]
    if existing_format != requested_format:
        sys.exit(
            f"{path} already has tiles built with --tile-format "
            f"{existing_format}, but this run asked for "
            f"{requested_format}. Refusing to resume: one archive cannot "
            f"mix tile formats. Use --tile-format {existing_format} to "
            f"keep resuming this file, or point --output/--mbtiles/"
            f"--db-path at a fresh path to build {requested_format} "
            f"from scratch."
        )


def set_mbtiles_metadata(conn: sqlite3.Connection, **kv) -> None:
    for k, v in kv.items():
        conn.execute(
            "INSERT OR REPLACE INTO metadata (name, value) VALUES (?, ?)",
            (k, str(v)),
        )
    conn.commit()


def existing_tiles_for_zoom(conn: sqlite3.Connection, z: int) -> np.ndarray:
    """Sorted, unique packed (``pack_xy``) keys of every tile already
    written at zoom z -- the resume skip list.

    Returns a numpy int64 array, not a ``set`` of ``(x, y)`` tuples.
    Measured at global z14 scale, a set of tuples costs 151.6 bytes per
    tile (6.2 GB for a complete 40.75M-tile zoom, 0.42 GB at the 2.78M
    tiles the real run had banked when it was OOM-killed) against 8 bytes
    per tile here; and the old ``.fetchall()`` materialized a second list
    of 40.75M tuples before the set even started. The row count is read
    first so the array is allocated once at its final size rather than
    grown by concatenation, which would double the peak.

    Callers test membership with ``keys_present``, vectorized over one
    cell's (or one parent tile's) keys at a time.
    """
    total = conn.execute(
        "SELECT count(*) FROM tiles WHERE zoom_level = ?", (z,)
    ).fetchone()[0]
    if not total:
        return np.empty(0, dtype=np.int64)
    flip = (1 << z) - 1  # TMS row -> XYZ y
    keys = np.empty(int(total), dtype=np.int64)
    cur = conn.execute(
        "SELECT tile_column, tile_row FROM tiles WHERE zoom_level = ?", (z,)
    )
    filled = 0
    while True:
        rows = cur.fetchmany(_TILE_SCAN_CHUNK)
        if not rows:
            break
        a = np.array(rows, dtype=np.int64)
        packed = (a[:, 0] << _XY_SHIFT) | (flip - a[:, 1])
        if filled + packed.size > keys.size:
            # The table grew between the count and the scan. It cannot in
            # this script (both run single-threaded, before any worker
            # pool starts), but truncating the resume skip list would
            # silently rebuild and overwrite finished tiles, so handle it
            # rather than assume it.
            keys = np.concatenate([keys[:filled], packed])
            filled = keys.size
            continue
        keys[filled:filled + packed.size] = packed
        filled += packed.size
    keys = keys[:filled]
    keys.sort()
    return keys


def tile_counts_by_zoom(conn: sqlite3.Connection) -> dict[int, int]:
    """``{zoom: tile count}`` for everything actually in the archive.

    The authoritative answer to "what did this build produce", read from
    the tiles table rather than from what the run was asked for -- see
    ``log_build_summary``.
    """
    return {
        int(z): int(n)
        for z, n in conn.execute(
            "SELECT zoom_level, count(*) FROM tiles "
            "GROUP BY zoom_level ORDER BY zoom_level"
        )
    }


def sync_zoom_metadata(conn: sqlite3.Connection) -> tuple[int | None, int | None]:
    """Set the ``metadata`` table's ``minzoom``/``maxzoom`` from the zoom
    levels the ``tiles`` table actually holds. Returns them, or
    ``(None, None)`` if there are no tiles yet.

    These two values used to be written once, at the start, from the
    *requested* ``--minzoom``/``--maxzoom``. On the real global run that
    made the surviving fragment claim z0-z14 while holding only z14
    tiles: a consumer reading the metadata (or a ``pmtiles show``) was
    told about 15 zoom levels that did not exist. Writing them from built
    content instead means the claim is true whenever it is read --
    including after a kill, because this runs on every commit (see
    ``CommitScheduler.flush``), so the fragment on disk is never more
    than one commit interval stale.

    ``min()`` and ``max()`` are issued as two separate statements on
    purpose: SQLite optimizes a lone ``min``/``max`` over an indexed
    column into an index seek, but not two aggregates in one query, which
    would scan all 40M+ rows on every commit.
    """
    lo = conn.execute("SELECT min(zoom_level) FROM tiles").fetchone()[0]
    if lo is None:
        return None, None
    hi = conn.execute("SELECT max(zoom_level) FROM tiles").fetchone()[0]
    conn.execute(
        "INSERT OR REPLACE INTO metadata (name, value) VALUES ('minzoom', ?)",
        (str(int(lo)),),
    )
    conn.execute(
        "INSERT OR REPLACE INTO metadata (name, value) VALUES ('maxzoom', ?)",
        (str(int(hi)),),
    )
    return int(lo), int(hi)


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


def read_tile_and_mask(path: Path, z: int, x: int, y: int):
    """(elevation float32 array, void_mask bool array), or None if absent.

    Takes the MBTiles *path*, not a Connection: this is called from
    worker threads (derive_zoom_from_child's derive_one) as well as the
    main thread (--verify), and must use get_read_connection's per-thread
    read-only connection rather than any single shared Connection object
    -- see that function's docstring.
    """
    conn = get_read_connection(path)
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

    Every commit also refreshes the archive's ``minzoom``/``maxzoom``
    metadata from the tiles actually present (``sync_zoom_metadata``), in
    the same transaction as the tiles it is committing. That is what keeps
    a killed run's fragment self-describing instead of claiming the whole
    requested zoom range; the two index seeks it costs are nothing against
    a 20,000-tile commit.
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
            sync_zoom_metadata(self.conn)
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

    # One rect, from the one definition build_zoom_rewarp's contributor
    # counting also uses (cell_tile_rect): the warp grid below and that
    # count must describe the same tile set or a tile is either never
    # finalized or finalized a contributor short.
    txmin, txmax, tymin, tymax = cell_tile_rect(cell.bbox, z)
    if txmin > txmax or tymin > tymax:
        return {}
    left, _, _, top = mercantile.xy_bounds(txmin, tymin, z)
    _, bottom, right, _ = mercantile.xy_bounds(txmax, tymax, z)
    width = (txmax - txmin + 1) * TILE_SIZE
    height = (tymax - tymin + 1) * TILE_SIZE

    def attempt_once() -> dict[tuple[int, int], np.ndarray]:
        """One open, warp, read and slice pass over this cell.

        A separate frame on purpose. When this raises, its frame unwinds
        and everything it holds goes with it: the source dataset, the
        VRT, the warped MEM dataset, the block array, and -- the part
        that matters for the future -- any large local a later change
        adds here, without anyone having to remember to release it.

        That last point is why this is a nested function rather than a
        list of names set back to None before the sleep. Both free the
        same memory today. Only this one keeps freeing it after someone
        adds another intermediate array.
        """
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
        for tx in range(txmin, txmax + 1):
            col0 = (tx - txmin) * TILE_SIZE
            for ty in range(tymin, tymax + 1):
                row0 = (ty - tymin) * TILE_SIZE
                out[(tx, ty)] = arr[row0: row0 + TILE_SIZE,
                                    col0: col0 + TILE_SIZE].copy()
        return out

    last_error: str | None = None
    for attempt in range(1, CELL_READ_ATTEMPTS + 1):
        try:
            return attempt_once()
        except Exception as exc:  # noqa: BLE001 - any failure here is retryable
            # Keep the TEXT, never the exception object. Holding the
            # exception holds its traceback, which holds the failed
            # attempt's frame and every frame below it -- and those own
            # the warp array and the void-fill working set, hundreds of
            # MB to several GB per cell.
            #
            # Two things together make the backoff hold nothing. Python
            # drops `exc` when this clause ends, which releases the
            # traceback and with it attempt_once's frame; and the sleep
            # sits outside the clause, so it runs after that. An earlier
            # version stored the exception and slept inside the handler,
            # which pinned the lot for the whole backoff across every
            # cell retrying at once. A later version fixed the traceback
            # but still ran the loop body in this frame, so the failed
            # attempt's own dataset and block array -- about twice the
            # cell block -- stayed bound until the next attempt rebound
            # them. On the high-latitude cells, where a block reaches
            # 4.6 GiB, a handful of cells retrying together was tens of
            # GiB of burst on top of the window.
            last_error = f"{type(exc).__name__}: {exc}"

        # Deliberately outside the except clause: see above.
        if attempt < CELL_READ_ATTEMPTS:
            # Drop this URL's cached file properties before retrying.
            # /vsicurl caches a file's size, and a transient bad answer
            # can leave a wrong one cached, which would make every
            # in-process retry fail identically no matter how healthy the
            # object became. Partial, not VSICurlClearCache(), so one
            # failing cell does not make the other workers re-fetch
            # headers for every object they have open.
            try:
                gdal.VSICurlPartialClearCache(f"/vsicurl/{url}")
            except Exception:  # noqa: BLE001 - a cache hint must never fail a build
                pass
            time.sleep(CELL_READ_BACKOFF_S * attempt)
    raise CellReadError(
        f"{cell.cell_id} z{z}: failed after {CELL_READ_ATTEMPTS} "
        f"attempt(s): {last_error}"
    )


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

class RewarpResult(NamedTuple):
    written: int
    already: int
    #: The cells whose source read failed this run, by id -- not just a
    #: count, so --overview-mode rewarp (which fetches every cell again at
    #: every zoom) can report how many DISTINCT cells failed overall
    #: instead of summing the same cell once per zoom.
    failed_cell_ids: tuple[str, ...]
    cells_admitted: int
    unfinished_shared: int
    #: Tiles that composited correctly but could not be encoded. They are
    #: absent from the archive, never partial, so they make the run
    #: PARTIAL and a rerun redoes exactly them.
    failed_encodes: int = 0

    @property
    def failed_cells(self) -> int:
        return len(self.failed_cell_ids)


class _CellTileIndex:
    """Per-zoom, per-cell tile bookkeeping, computed one cell at a time.

    This replaces the two structures the per-zoom setup used to build for
    the entire requested scope before any raster I/O: ``cell_tiles``
    (one ``mercantile.Tile`` object per cell per tile) and ``remaining``
    (one dict entry per tile). Measured at real global z14 scope -- 13,485
    cells, 42.138M cell-tile contributions over 40.753M distinct tiles --
    those two cost 213.7 bytes per contribution, so 9.0 GB, retained for
    the whole zoom and invisible at the 99-cell scale every earlier
    validation ran at. This index holds 4 integers per cell instead:
    1.5 MB of rects for the same global scope, 3.8 MB including the
    admission pass, built in 0.2 s.

    What it keeps:

    ``rect`` per cell
        ``cell_tile_rect``, so a cell's tile set is a rectangle, not a
        list.
    ``done_keys``
        one sorted packed-key array for the whole zoom (8 bytes/tile, the
        only genuinely scope-proportional structure left; 326 MB at a
        complete global z14, 22 MB at the 2.78M tiles the OOM-killed run
        had banked).

    What it computes on demand, per cell, as that cell is admitted:

    ``done_mask(i)``
        which of cell i's tiles an earlier run already wrote.
    ``contributor_counts(i)``
        how many selected cells cover each of cell i's tiles -- the
        number ``remaining`` used to hold per tile. Derived by
        intersecting integer rects, not by any lon/lat test: a float
        comparison at a cell boundary that disagreed with
        ``mercantile.tiles`` by one tile would reintroduce the
        shared-boundary seam bug (23 shared z13 tiles per 1-degree
        boundary) or strand a tile at a count that never reaches zero.

    Both are a few KB per cell and are discarded with it.
    """

    def __init__(self, cells, z: int, done_keys: np.ndarray):
        self.cells = cells
        self.z = z
        self.done_keys = done_keys
        self.rects = [cell_tile_rect(c.bbox, z) for c in cells]
        n = len(self.rects)
        self._x0 = np.fromiter((r[0] for r in self.rects), dtype=np.int64, count=n)
        self._x1 = np.fromiter((r[1] for r in self.rects), dtype=np.int64, count=n)
        self._y0 = np.fromiter((r[2] for r in self.rects), dtype=np.int64, count=n)
        self._y1 = np.fromiter((r[3] for r in self.rects), dtype=np.int64, count=n)

    def done_mask(self, i: int) -> np.ndarray:
        """(nx, ny) bool: cell i's tiles that are already in the MBTiles."""
        return keys_present(self.done_keys, rect_keys(self.rects[i]))

    def todo_count(self, i: int) -> int:
        x0, x1, y0, y1 = self.rects[i]
        area = (x1 - x0 + 1) * (y1 - y0 + 1)
        if self.done_keys.size == 0:
            return int(area)
        return int(area - np.count_nonzero(self.done_mask(i)))

    def contributor_counts(self, i: int) -> np.ndarray:
        """(nx, ny) int32: how many selected cells cover each tile of cell
        i's rect, counting cell i itself. Exactly the count the old
        ``remaining`` dict held for those tiles."""
        x0, x1, y0, y1 = self.rects[i]
        nx, ny = x1 - x0 + 1, y1 - y0 + 1
        ox0 = np.maximum(self._x0, x0)
        ox1 = np.minimum(self._x1, x1)
        oy0 = np.maximum(self._y0, y0)
        oy1 = np.minimum(self._y1, y1)
        hit = np.nonzero((ox0 <= ox1) & (oy0 <= oy1))[0]
        counts = np.zeros((nx, ny), dtype=np.int32)
        # Two ways to accumulate the same thing. Adding 1 over each
        # overlapping cell's intersection sub-block costs one numpy slice
        # per overlapping cell, which is what deep zooms want (at z14 a
        # cell overlaps at most its 8 neighbours). At a zoom shallow
        # enough that one tile spans many degrees, every cell overlaps
        # every other one but the rect itself is tiny, so testing each of
        # this rect's few tiles against all the intersections at once is
        # the cheap direction instead. Picking by size keeps either
        # regime from degenerating.
        if nx * ny <= hit.size:
            for ix in range(nx):
                x = x0 + ix
                in_x = (ox0 <= x) & (x <= ox1)
                for iy in range(ny):
                    y = y0 + iy
                    counts[ix, iy] = np.count_nonzero(
                        in_x & (oy0 <= y) & (y <= oy1)
                    )
        else:
            for j in hit:
                counts[int(ox0[j]) - x0: int(ox1[j]) - x0 + 1,
                       int(oy0[j]) - y0: int(oy1[j]) - y0 + 1] += 1
        return counts


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
    tile_format: str = "png",
    encode_backlog: int = 0,
    sampler: MemorySampler | None = None,
    sampler_every_cells: int = 50,
) -> RewarpResult:
    """Build zoom z by warping every selected cell from source.

    Nothing proportional to the requested scope is materialized before the
    raster I/O starts: see ``_CellTileIndex`` for what replaced the old
    per-zoom ``cell_tiles``/``remaining`` dicts and why.
    """
    done_keys = np.empty(0, dtype=np.int64) if force else existing_tiles_for_zoom(conn, z)
    n_done = int(done_keys.size)
    index = _CellTileIndex(cells, z, done_keys)

    # Admission list: the cells with at least one tile still to build, in
    # the order `cells` gives them -- the same cells, in the same order,
    # the old `cell_tiles` dict yielded (a dict preserves insertion
    # order, and it was filled by iterating `cells`). Ordering is
    # load-bearing: it decides which cell's value lands first in a shared
    # boundary tile.
    admitted: list[int] = []
    contributions = 0
    for i in range(len(cells)):
        n = index.todo_count(i)
        if n:
            admitted.append(i)
            contributions += n

    if not admitted:
        log(f"z{z}: nothing to do ({n_done} tile(s) already built)")
        return RewarpResult(0, n_done, (), 0, 0)

    log(f"z{z}: {len(admitted)} cell(s) to fetch, up to {contributions} "
        f"cell-tile contribution(s) to build ({n_done} tile(s) already "
        f"built). A tile shared by adjacent cells is counted once per "
        f"contributing cell here; the build summary reports the true "
        f"tile counts from the archive.")

    # One entry per *shared* boundary tile that is open right now: a tile
    # at least one contributing cell has delivered and at least one has
    # not. A tile only one selected cell covers -- 96.7% of them at
    # global z14, measured -- is encoded straight off the arriving cell's
    # own array and never enters this dict. Each entry is
    # [contributions still owed, accumulated array].
    pending: dict[tuple[int, int], list] = {}
    pending_high = 0
    written = 0
    failed_encodes = 0
    failed_cell_ids: list[str] = []
    pending_encode: list = []
    sampler_every = max(sampler_every_cells, 1)
    t0 = time.time()
    done_n = 0

    void_cache: dict[tuple[int, int], np.ndarray] = {}

    with ThreadPoolExecutor(max_workers=encode_workers) as encode_pool:

        def drain_ready_encodes() -> None:
            nonlocal written, failed_encodes
            still = []
            for ef, xy in pending_encode:
                if not ef.done():
                    still.append((ef, xy))
                    continue
                try:
                    data = ef.result()
                except Exception as exc:  # noqa: BLE001 - degrade per tile, never kill the zoom
                    # Same policy as a failed cell fetch: the tile is
                    # simply never written, so a rerun redoes exactly it.
                    # Before this guard an encode-time failure (a
                    # MemoryError in Pillow being the realistic one on a
                    # build that has already been OOM-killed twice)
                    # propagated out of here and took the whole zoom down.
                    failed_encodes += 1
                    void_cache.pop(xy, None)
                    log(f"  WARNING: z{z} tile {xy} failed to encode, "
                        f"leaving it unfinished this run: {exc}")
                    continue
                write_tile(conn, z, xy[0], xy[1], data, void_cache.pop(xy))
                written += 1
                committer.tick()
            pending_encode[:] = still

        def await_encode_capacity() -> None:
            """Block until the encode backlog is under `encode_backlog`.

            Deadlock cannot happen: the encode pool's threads are not this
            thread, so blocking here never stops an encode from finishing.
            Progress is guaranteed because ``wait(..., FIRST_COMPLETED)``
            returns only once at least one queued future is done, and the
            drain that follows removes every done entry, so the backlog
            strictly shrinks on every iteration.
            """
            if not encode_backlog or len(pending_encode) < encode_backlog:
                return
            drain_ready_encodes()
            while len(pending_encode) >= encode_backlog:
                wait([ef for ef, _ in pending_encode],
                     return_when=FIRST_COMPLETED)
                drain_ready_encodes()

        def submit_encode(xy: tuple[int, int], elev: np.ndarray, void: np.ndarray) -> None:
            if quantize:
                elev = quantize_elevation(elev, z)
            # Back-pressure lives here, at the one choke point every
            # finalize() goes through, rather than at the call sites. A
            # first attempt at this put the throttle after the shared-tile
            # branch in on_result, which left the `owed <= 1` fast path --
            # 96.7% of all submissions at global z14 -- completely
            # unbounded, and the backlog still reached 649 against a cap
            # of 64. Here no submission path can bypass it, and a future
            # one cannot be added that does.
            await_encode_capacity()
            void_cache[xy] = void
            ef = encode_pool.submit(lambda e=elev: encode_tile_bytes(encode_terrarium(e), tile_format))
            pending_encode.append((ef, xy))

        # Why the encode backlog is bounded at all. Nothing used to bound
        # encode submission: one cell's on_result submits every tile only
        # that cell covers -- about 2,900 of 3,022 at global z14 -- in a
        # tight loop, and each queued tile pins a 256 KB quantized
        # elevation array in its lambda plus a 64 KB void mask in
        # `void_cache`, so 320 KiB per queued tile.
        #
        # At 32 workers the queue stayed empty (`encoding` was 0 for the
        # whole run with one blip to 35), because the encode pool finished
        # a cell's tiles long before the next warp landed. At **24**
        # workers in dense terrain the balance inverted: the real run's
        # `encoding` field went 0 until about cell 200, then 2,552 ->
        # 8,428 -> 9,772 -> 16,640 -> 24,868 by cell 650 and still
        # climbing. 24,868 queued tiles is 7.6 GiB pinned, and the growth
        # is unbounded, which is what produced the violent anon
        # oscillation: the compositing thread kept submitting while the
        # pool fell behind, then a drain wrote tens of thousands of tiles
        # at once and released the lot in a batch.
        #
        # Fewer fetch workers means each cell's warp arrives sooner
        # relative to the encode pool's throughput, and dense terrain
        # means a high-entropy PNG takes far longer than the 5.3 ms a
        # smooth smoke tile measured. Both push the same way.
        #
        # A bound also fixes a second-order problem it would be easy to
        # miss: drain_ready_encodes rescans the whole backlog on every
        # call, so a 25,000-entry queue cost the compositing thread a
        # linear sweep per cell. Bounded, that sweep is trivial.

        def finalize(xy: tuple[int, int], arr: np.ndarray | None) -> None:
            if arr is None:
                arr = np.full((TILE_SIZE, TILE_SIZE), np.nan, dtype=np.float32)
            void = np.isnan(arr)
            elev = np.where(void, 0.0, arr).astype(np.float32)
            submit_encode(xy, elev, void)

        def submit_fetch(i: int):
            cell = cells[i]
            url = cell.dsm_url if band == "dsm" else cell.dtm_url
            return _fetch_cell_safe(cell, z, url, fill_voids=fill_voids)

        def on_result(i: int, fut) -> None:
            nonlocal done_n, pending_high
            cell = cells[i]
            done_n += 1
            try:
                _, blocks = fut.result()
            except Exception as exc:  # noqa: BLE001 - a future must never kill the run
                log(f"  WARNING: {cell.cell_id} z{z} future raised, leaving "
                    f"its tiles unfinished this run: {exc}")
                blocks = None

            if blocks is None:
                failed_cell_ids.append(cell.cell_id)
                # Deliberately do NOT decrement anything for this cell's
                # tiles: a shared tile keeps owing this cell's
                # contribution and is simply never finalized this run,
                # and a tile only this cell covers is never written at
                # all. See "Fetch failures never bake in ocean" in the
                # module docstring.
            else:
                x0, _x1, y0, _y1 = index.rects[i]
                counts = index.contributor_counts(i)
                already = index.done_mask(i)
                for xy, arr in blocks.items():
                    ix, iy = xy[0] - x0, xy[1] - y0
                    if already[ix, iy]:
                        continue  # an earlier run already wrote this tile
                    owed = int(counts[ix, iy])
                    if owed <= 1:
                        # The only selected cell covering this tile is
                        # this one, so it is complete on arrival. The old
                        # code merged it into a fresh all-NaN array
                        # first, which produces exactly these bytes: the
                        # merge is "take arr wherever arr is not NaN",
                        # and the NaN it leaves behind becomes 0 in
                        # finalize either way.
                        finalize(xy, arr)
                        continue
                    ent = pending.get(xy)
                    if ent is None:
                        # First contribution to a shared tile. Taking
                        # this array as the accumulator is identical to
                        # merging it into an all-NaN one, and saves the
                        # allocation.
                        pending[xy] = [owed - 1, arr]
                        continue
                    cur = ent[1]
                    fill = np.isnan(cur) & ~np.isnan(arr)
                    cur[fill] = arr[fill]
                    ent[0] -= 1
                    if ent[0] <= 0:
                        del pending[xy]
                        finalize(xy, cur)
                if len(pending) > pending_high:
                    pending_high = len(pending)

            drain_ready_encodes()
            if done_n % 50 == 0 or done_n == len(admitted):
                rate = done_n / max(time.time() - t0, 1e-6)
                log(f"  z{z}: {done_n}/{len(admitted)} cells ({rate:.2f}/s), "
                    f"{written} tile(s) written, {len(pending)} pending "
                    f"(peak {pending_high}), {len(pending_encode)} encoding, "
                    f"{len(failed_cell_ids)} cell failure(s)"
                    + (f", {failed_encodes} encode failure(s)"
                       if failed_encodes else ""))
            if sampler is not None and done_n % sampler_every == 0:
                # The warp blocks the fetch workers hold are the dominant
                # allocation and the one nothing else itemizes. They are
                # not reachable from this thread, so they are reported
                # from each in-flight cell's own rect: the window is
                # topped up to `window` entries, so the cells that can be
                # holding a block right now are the next `window`
                # admitted cells that have not come back yet.
                win_hi = min(done_n + window, len(admitted))
                warp_window = sum(cell_block_bytes(index.rects[admitted[k]])
                                  for k in range(done_n, win_hi))
                sampler.sample(
                    f"z{z} cell {done_n}/{len(admitted)}",
                    written=written,
                    warp_cell_mib=cell_block_bytes(index.rects[i]) / 2**20,
                    warp_window_mib=warp_window / 2**20,
                    pending_tiles=len(pending),
                    pending_mib=arrays_nbytes(e[1] for e in pending.values()) / 2**20,
                    encoding_tiles=len(pending_encode),
                    encoding_mib=len(pending_encode) * TILE_SIZE * TILE_SIZE * 4 / 2**20,
                    void_cache_tiles=len(void_cache),
                    void_cache_mib=arrays_nbytes(void_cache.values()) / 2**20,
                )

        window = max(window_multiplier * workers, 1)
        run_bounded(admitted, submit_fetch, on_result, workers, window)

        # Drain whatever encodes are still outstanding, blocking this time.
        # Same per-tile degradation as drain_ready_encodes: one tile that
        # cannot be encoded must not discard the rest of the zoom's work.
        for ef, xy in pending_encode:
            try:
                data = ef.result()
            except Exception as exc:  # noqa: BLE001 - degrade per tile
                failed_encodes += 1
                void_cache.pop(xy, None)
                log(f"  WARNING: z{z} tile {xy} failed to encode, leaving "
                    f"it unfinished this run: {exc}")
                continue
            write_tile(conn, z, xy[0], xy[1], data, void_cache.pop(xy))
            written += 1
            committer.tick()
        pending_encode.clear()

    committer.flush()
    if pending or failed_cell_ids:
        log(f"  z{z}: {len(failed_cell_ids)} cell(s) failed and {len(pending)} "
            f"shared boundary tile(s) are still waiting on one; neither is "
            f"written. Rerun to retry -- it only redoes what is missing.")
    if failed_encodes:
        log(f"  z{z}: {failed_encodes} tile(s) could not be encoded this "
            f"run; they are absent, not partial. Rerun to retry.")
    if sampler is not None:
        sampler.sample(f"z{z} end", written=written,
                       pending_tiles=len(pending),
                       pending_high_tiles=pending_high)
    return RewarpResult(
        written=written,
        already=n_done,
        failed_cell_ids=tuple(failed_cell_ids),
        cells_admitted=len(admitted),
        unfinished_shared=len(pending),
        failed_encodes=failed_encodes,
    )


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
    path: Path,
    workers: int,
    force: bool,
    quantize: bool,
    committer: CommitScheduler,
    tile_format: str = "png",
    sampler: MemorySampler | None = None,
    sampler_every_tiles: int = 5000,
) -> tuple[int, int, int]:
    """Build zoom z purely from the already-built zoom child_z (= z+1) in
    the same MBTiles -- no source reads, no network. Returns (written,
    already_done, failed_parents).

    The parent list is derived through packed-key numpy arrays rather than
    sets of ``(x, y)`` tuples, for the same reason
    ``existing_tiles_for_zoom`` is: the first derive step of a global
    build reads 40.75M z14 rows, and the old ``.fetchall()`` plus
    ``child_xy`` set measured ~151 bytes per child tile -- about 6 GB of
    scope-proportional cost, on top of the ~9 GB the z14 rewarp stage had
    already been charging. Deduplicating each chunk of children to its
    parents as it arrives keeps the peak at the parent count (82 MB at
    z13, 8 bytes/tile) plus one chunk.
    """
    done_keys = np.empty(0, dtype=np.int64) if force else existing_tiles_for_zoom(conn, z)
    n_done = int(done_keys.size)

    flip = (1 << child_z) - 1  # TMS row -> XYZ y
    cur = conn.execute(
        "SELECT tile_column, tile_row FROM tiles WHERE zoom_level = ?", (child_z,)
    )
    chunks: list[np.ndarray] = []
    while True:
        rows = cur.fetchmany(_TILE_SCAN_CHUNK)
        if not rows:
            break
        a = np.array(rows, dtype=np.int64)
        # The standard XYZ 2x2 pyramid relationship: a child's parent is
        # (x // 2, y // 2). Both are non-negative, so >> 1 is // 2.
        chunks.append(np.unique(
            ((a[:, 0] >> 1) << _XY_SHIFT) | ((flip - a[:, 1]) >> 1)
        ))
    parent_keys = (
        np.unique(np.concatenate(chunks)) if chunks else np.empty(0, dtype=np.int64)
    )
    if parent_keys.size and n_done:
        parent_keys = parent_keys[~keys_present(done_keys, parent_keys)]
    n_parents = int(parent_keys.size)

    if not n_parents:
        log(f"z{z}: nothing to derive from z{child_z} "
            f"({n_done} tile(s) already built)")
        return 0, n_done, 0

    log(f"z{z}: deriving {n_parents} tile(s) from z{child_z} "
        f"({n_done} already built)")

    written = 0
    t0 = time.time()

    def read_child(cx: int, cy: int):
        # Runs inside derive_one, which run_bounded submits to a worker
        # pool -- read_tile_and_mask must use its own per-thread read-only
        # connection here, never the shared writer `conn` above (which is
        # only ever touched by this function's own top-level, single-
        # -threaded code, e.g. the existing_tiles_for_zoom/child_rows
        # queries just above). See get_read_connection's docstring.
        return read_tile_and_mask(path, child_z, cx, cy)

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

    failed_parents = 0

    def on_result(parent, fut) -> None:
        nonlocal written, done_n, failed_parents
        done_n += 1
        try:
            parent_xy, elev, void = fut.result()
        except Exception as exc:  # noqa: BLE001 - a future must never kill the run
            # Same policy as build_zoom_rewarp's on_result: a tile that
            # failed to derive is simply never written -- absent from the
            # MBTiles, not finalized with wrong/partial data -- and a
            # rerun redoes exactly it (and only it), since
            # existing_tiles_for_zoom only ever sees tiles that were
            # actually committed. Resume state is never poisoned by this,
            # because nothing below (write_tile, committer.tick) runs for
            # a failed parent; this except clause exists so one bad tile
            # does not take the rest of the zoom's derivation down with
            # it, which -- before this fix -- it did (fut.result() ran
            # unguarded here, unlike the already-hardened rewarp path).
            failed_parents += 1
            log(f"  WARNING: z{z} tile {parent} failed to derive, leaving "
                f"it unfinished this run: {exc}")
            return
        write_tile(conn, z, parent_xy[0], parent_xy[1],
                   encode_tile_bytes(encode_terrarium(elev), tile_format), void)
        written += 1
        committer.tick()
        if done_n % 500 == 0 or done_n == n_parents:
            rate = done_n / max(time.time() - t0, 1e-6)
            log(f"  z{z}: {done_n}/{n_parents} tile(s) derived ({rate:.1f}/s)")
        if sampler is not None and done_n % max(sampler_every_tiles, 1) == 0:
            sampler.sample(f"z{z} derive {done_n}/{n_parents}", written=written)

    # derive_one runs in run_bounded's own worker threads and only *reads*
    # -- via read_child/read_tile_and_mask's own private, per-thread,
    # read-only connection (get_read_connection), never the shared writer
    # `conn` above. on_result, which does the only writing, runs on the
    # thread driving run_bounded's wait() loop.
    # The parent tiles are handed to run_bounded as a generator over the
    # packed-key array, so only the in-flight window's worth of (x, y)
    # tuples exists at a time instead of a 10.2M-element list. Each
    # parent tile is independent (it reads 4 children and writes only
    # itself), so this iterates in packed-key order where the old code
    # iterated a set's hash order -- a different order, identical output.
    def parents_in_order():
        for key in parent_keys:
            yield unpack_xy(key)

    window = max(4 * workers, 1)
    run_bounded(parents_in_order(), derive_one, on_result, workers, window)

    committer.flush()
    if failed_parents:
        log(f"  z{z}: {failed_parents} tile(s) could not be derived this "
            f"run; rerun to retry")
    return written, n_done, failed_parents


# --------------------------------------------------------------------------
# verify
# --------------------------------------------------------------------------

def _webp_roundtrip_check(path: Path, z: int, x: int, y: int) -> tuple[bool, str]:
    """WebP is only useful here if it is actually lossless: re-encode the
    exact RGB this tile decoded to, through the same encode_tile_bytes
    path the real build used, and decode that back -- every byte of every
    channel must come back identical, since any difference would be a
    terrarium-precision error (a wrong meter, not just a compression
    artifact) once decode_terrarium runs on it. Returns (ok, note).

    Takes `path`, not a Connection (consistent with read_tile_and_mask,
    and for the same reason: nothing here should ever touch the shared
    writer connection, even though this particular call only ever runs
    single-threaded from --verify's own loop today)."""
    conn = get_read_connection(path)
    tile_row = ((1 << z) - 1) - y
    row = conn.execute(
        "SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
        (z, x, tile_row),
    ).fetchone()
    if row is None:
        return False, "tile not found for webp round-trip check"
    original_rgb = np.array(Image.open(io.BytesIO(row[0])).convert("RGB"))
    roundtrip_bytes = encode_tile_bytes(original_rgb, "webp")
    roundtrip_rgb = np.array(Image.open(io.BytesIO(roundtrip_bytes)).convert("RGB"))
    if np.array_equal(original_rgb, roundtrip_rgb):
        return True, "webp round-trip: bit-identical RGB, confirmed lossless"
    diff = int(np.abs(original_rgb.astype(np.int16) - roundtrip_rgb.astype(np.int16)).max())
    return False, f"webp round-trip FAILED: max channel diff {diff} (should be 0, lossless)"


def verify_mbtiles_tile(
    path: Path,
    z: int,
    x: int,
    y: int,
    url: str,
    tolerance_m: float,
    is_rewarped: bool,
    fill_voids: bool = False,
    tile_format: str = "png",
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

    found = read_tile_and_mask(path, z, x, y)
    if found is None:
        log(f"verify: tile z{z}/{x}/{y} not found in MBTiles")
        return False
    decoded, _decoded_void = found

    webp_ok, webp_note = True, ""
    if tile_format == "webp":
        webp_ok, webp_msg = _webp_roundtrip_check(path, z, x, y)
        webp_note = f" | {webp_msg}"

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
            elif max(fill_vals) == 0.0 and min(fill_vals) == 0.0:
                # Every sampled pixel decoded to exactly 0 m. This reads
                # like a failed fill and is not one: it is the ocean
                # convention (module docstring, "NaN, nodata, and the
                # ocean convention") still applying to these pixels,
                # because neither GEM-15 nor GLO-30 covers them -- open
                # ocean, or a pixel that stayed void in all four
                # contributing children of a derived tile. Said plainly,
                # with this zoom's own quantization step, rather than
                # printed as a bare "[0.0, 0.0]" range that an operator
                # has to go and disprove.
                fill_note = (f" | void-fill sanity OK ({len(fill_vals)} filled "
                             f"sample(s) all exactly 0 m: no fill source "
                             f"covers them, so the 0 m ocean convention "
                             f"applies -- this zoom quantizes to "
                             f"{quantization_step_m(z):g} m)")
            else:
                fill_note = (f" | void-fill sanity OK ({len(fill_vals)} filled "
                             f"sample(s), range [{min(fill_vals):.1f}, "
                             f"{max(fill_vals):.1f}] m, quantization step "
                             f"{quantization_step_m(z):g} m, not cross-checked "
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
        ok = (max_diff <= effective_tolerance) and webp_ok
        log(f"verify: z{z}/{x}/{y} (rewarped): max sampled "
            f"|decoded - direct-read| = {max_diff:.4f} m "
            f"(tolerance {effective_tolerance:.4f} m) -> "
            f"{'OK' if ok else 'FAIL'}{note}{webp_note}")
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
            f"; not treated as a failure{fill_note}{webp_note}")
        return fill_sanity_ok and webp_ok
    else:
        max_diff = max(diffs)
        kind = "interior" if len(interior_valid) else "boundary-adjacent"
        note = f" ({len(diffs)} {kind} sample(s) out of {len(raw_valid)} real-data pixel(s))"
        tile_resolution_m = (right - left) / TILE_SIZE
        relative = RELATIVE_TOLERANCE if is_rewarped else RELATIVE_TOLERANCE_AVERAGED
        effective_tolerance = max(tolerance_m, tile_resolution_m * relative)

    ok = (max_diff <= effective_tolerance) and fill_sanity_ok and webp_ok
    log(f"verify: z{z}/{x}/{y} ({'rewarped' if is_rewarped else 'derived'}): "
        f"max sampled |decoded - direct-read| = {max_diff:.4f} m "
        f"(tolerance {effective_tolerance:.4f} m) -> "
        f"{'OK' if ok else 'FAIL'}{note}{fill_note}{webp_note}")
    return ok


# --------------------------------------------------------------------------
# build summary
# --------------------------------------------------------------------------

#: Exit codes. 0 and 1 keep their old meanings; PARTIAL is a third code so
#: a caller can tell "the archive does not cover the requested scope" from
#: "--verify found a bad tile" without parsing the log.
EXIT_OK = 0
EXIT_VERIFY_FAILED = 1
EXIT_PARTIAL = 3


def _fmt_zooms(zooms) -> str:
    """"z0-z14 (15 level(s))" for a contiguous run, otherwise an explicit
    list -- never a range that implies levels the archive does not have."""
    zs = sorted(zooms)
    if not zs:
        return "none"
    if zs == list(range(zs[0], zs[-1] + 1)):
        if len(zs) == 1:
            return f"z{zs[0]} (1 level)"
        return f"z{zs[0]}-z{zs[-1]} ({len(zs)} levels)"
    return ", ".join(f"z{z}" for z in zs) + f" ({len(zs)} levels)"


def log_build_summary(
    conn: sqlite3.Connection,
    *,
    requested_zooms,
    cells_requested: int,
    failed_cells: int,
    unfinished_shared: int,
    failed_derived: int,
    failed_encodes: int = 0,
) -> bool:
    """Print what the archive actually holds against what was asked for.
    Returns True only if it covers the requested scope.

    The first global run ended with a summary that read as a clean
    completion over a 9.6% fragment: the per-cell failures were warnings
    far up the log, the closing lines were a tile count and a file size
    with nothing to compare them against, and the exit status was 0, so
    the job script promoted the fragment as a finished archive. Every
    number below is measured from the ``tiles`` table rather than taken
    from the request, and an incomplete archive says so in one line and
    exits non-zero.
    """
    counts = tile_counts_by_zoom(conn)
    present = sorted(counts)
    requested = sorted(requested_zooms)
    missing = [z for z in requested if not counts.get(z)]
    cells_completed = cells_requested - failed_cells

    log("build summary (measured from the archive, not from the request):")
    log(f"  cells:        {cells_completed}/{cells_requested} completed"
        + (f", {failed_cells} failed this run" if failed_cells else ""))
    log(f"  zooms wanted: {_fmt_zooms(requested)}")
    log(f"  zooms built:  {_fmt_zooms(present)}")
    for z in present:
        log(f"    z{z}: {counts[z]} tile(s)")
    log(f"  total tiles:  {sum(counts.values())}")

    reasons = []
    if failed_cells:
        reasons.append(f"{failed_cells} of {cells_requested} cell(s) failed")
    if missing:
        reasons.append(f"no tiles at all at {_fmt_zooms(missing)}")
    if unfinished_shared:
        reasons.append(
            f"{unfinished_shared} shared boundary tile(s) still waiting on a "
            f"failed neighbouring cell"
        )
    if failed_encodes:
        reasons.append(f"{failed_encodes} tile(s) failed to encode")
    if failed_derived:
        reasons.append(f"{failed_derived} tile(s) failed to derive")
    if reasons:
        log("  PARTIAL: " + "; ".join(reasons) + ".")
        log("  This archive does not cover the requested scope. Its own "
            "minzoom/maxzoom metadata describes what it really contains, "
            "so it is an honest fragment, but it is a fragment: no "
            ".pmtiles is produced from it, and there is nothing to "
            "promote. The working MBTiles is the banked progress. Rerun "
            "the same command to continue: it only redoes what is "
            "missing.")
        return False
    log("  COMPLETE: every requested zoom level is present and every cell "
        "completed.")
    return True


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
    p.add_argument("--encode-backlog-tiles", type=int, default=-1, metavar="N",
                   help="cap on tiles queued for encoding. -1 (default) "
                        "means auto: max(2 x --encode-workers, 32). 0 means "
                        "unbounded, which is what every run before this flag "
                        "did and what let the backlog reach 24,868 tiles "
                        "(7.6 GiB pinned) at 24 workers in dense terrain. "
                        "Each queued tile pins 320 KiB, so the cap is also "
                        "the memory this stage can hold.")
    p.add_argument("--memory-log", type=int, default=0, metavar="N",
                   help="emit a `memlog` telemetry line every N cells at "
                        "--maxzoom (and every 100xN derived tiles): process "
                        "RSS, the cgroup's anon/file/shmem split, working "
                        "database and WAL sizes, the in-flight warp blocks "
                        "(the dominant allocation), the real .nbytes of the "
                        "in-flight containers, the VM mapping count, and a "
                        "malloc_trim probe every 10th sample. Every field "
                        "names its own unit: `_mib` is mebibytes, `_tiles` "
                        "is a count. 0 disables.")
    p.add_argument("--no-quantize", action="store_true",
                   help="disable per-zoom vertical quantization (for A/B comparison)")
    p.add_argument("--no-void-fill", action="store_true",
                   help="disable filling GEM-15 voids from Copernicus "
                        "GLO-30 (the default); voids stay a flat 0 m, as "
                        "before this feature existed")
    p.add_argument("--tile-format", choices=["png", "webp"], default="png",
                   help="png (default): universally supported. webp: "
                        "lossless (bit-identical elevation, verified by "
                        "--verify), measured ~27%% smaller than quantized "
                        "PNG on the smoke cell -- see encode_tile_bytes "
                        "for the method-vs-size/speed measurements this "
                        "default was picked from. Not yet the default "
                        "here: a MapLibre-side decision, pending.")
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
        f"fill_voids={fill_voids}, tile_format={args.tile_format}, "
        f"z{args.minzoom}-z{args.maxzoom}")

    if args.mbtiles:
        mbtiles_path = args.mbtiles
    elif args.db_path:
        mbtiles_path = args.db_path / args.output.with_suffix(".mbtiles").name
    else:
        mbtiles_path = args.output.with_suffix(".mbtiles")
    conn = open_mbtiles(mbtiles_path)
    check_tile_format_resume_guard(conn, args.tile_format, mbtiles_path)
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
        format=args.tile_format,
        type="baselayer",
        version="1.0.0",
        description=description,
        attribution=attribution,
        bounds=f"{west},{south},{east},{north}",
        encoding="terrarium",
        overview_mode=args.overview_mode,
        void_fill="glo30" if fill_voids else "none",
    )
    # minzoom/maxzoom are deliberately NOT written from args.minzoom/
    # args.maxzoom here. They used to be, which is how the OOM-killed
    # global run left a fragment whose metadata advertised z0-z14 while
    # the tiles table held only z14. They now always come from the tiles
    # actually present -- here (so a resumed fragment is honest from the
    # first second), on every commit (CommitScheduler.flush, so a kill
    # cannot strand a stale claim), and once more before the convert.
    lo, hi = sync_zoom_metadata(conn)
    conn.commit()
    if lo is None:
        log("archive currently holds no tiles; minzoom/maxzoom will be "
            "written from built content as zoom levels complete")
    else:
        log(f"archive currently holds z{lo}-z{hi}; minzoom/maxzoom metadata "
            f"tracks built content, not the requested z{args.minzoom}-"
            f"z{args.maxzoom}")

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
    failed_cells: set[str] = set()
    total_unfinished_shared = 0
    total_failed_derived = 0
    total_failed_encodes = 0

    sampler = MemorySampler(mbtiles_path) if args.memory_log else None
    if sampler is not None:
        sampler.sample("start")
    if args.encode_backlog_tiles < 0:
        encode_backlog = max(2 * encode_workers, 32)
    else:
        encode_backlog = args.encode_backlog_tiles
    log("encode backlog cap: "
        + (f"{encode_backlog} tile(s) (~{encode_backlog * 320 / 1024:.1f} MiB "
           f"pinned at most)" if encode_backlog else "unbounded"))
    rewarp_kw = dict(
        fill_voids=fill_voids, tile_format=args.tile_format,
        encode_backlog=encode_backlog,
        sampler=sampler, sampler_every_cells=args.memory_log or 50,
    )
    derive_kw = dict(
        tile_format=args.tile_format, sampler=sampler,
        sampler_every_tiles=(args.memory_log or 50) * 100,
    )

    if args.overview_mode == "rewarp":
        for z in range(args.minzoom, args.maxzoom + 1):
            r = build_zoom_rewarp(
                z, cells, args.band, conn, args.workers, encode_workers,
                args.window_multiplier, args.force, quantize, committer,
                **rewarp_kw,
            )
            total_written += r.written
            failed_cells.update(r.failed_cell_ids)
            total_unfinished_shared += r.unfinished_shared
            total_failed_encodes += r.failed_encodes
            maybe_verify(z, bool(r.written or r.already), is_rewarped=True)
    else:
        r = build_zoom_rewarp(
            args.maxzoom, cells, args.band, conn, args.workers, encode_workers,
            args.window_multiplier, args.force, quantize, committer,
            **rewarp_kw,
        )
        total_written += r.written
        failed_cells.update(r.failed_cell_ids)
        total_unfinished_shared += r.unfinished_shared
        total_failed_encodes += r.failed_encodes
        maybe_verify(args.maxzoom, bool(r.written or r.already), is_rewarped=True)

        for z in range(args.maxzoom - 1, args.minzoom - 1, -1):
            written, already, failed_derived = derive_zoom_from_child(
                z, z + 1, conn, mbtiles_path, args.workers, args.force, quantize, committer,
                **derive_kw,
            )
            total_written += written
            total_failed_derived += failed_derived
            maybe_verify(z, bool(written or already), is_rewarped=False)

    log(f"wrote {total_written} new tile(s) to {mbtiles_path} "
        f"({len(failed_cells)} cell failure(s) total)")

    sync_zoom_metadata(conn)
    conn.commit()
    complete = log_build_summary(
        conn,
        requested_zooms=range(args.minzoom, args.maxzoom + 1),
        cells_requested=len(cells),
        failed_cells=len(failed_cells),
        unfinished_shared=total_unfinished_shared,
        failed_derived=total_failed_derived,
        failed_encodes=total_failed_encodes,
    )
    if sampler is not None:
        sampler.sample("end")

    all_ok = True
    for z, x, y, url, is_rewarped in verify_tiles:
        ok = verify_mbtiles_tile(
            mbtiles_path, z, x, y, url, args.verify_tolerance_m, is_rewarped,
            fill_voids=fill_voids, tile_format=args.tile_format,
        )
        all_ok = all_ok and ok

    conn.close()

    def exit_code() -> int:
        # Scope first: a fragment is the more consequential thing to
        # report, and a --verify failure inside a fragment is not news.
        if not complete:
            return EXIT_PARTIAL
        return EXIT_OK if all_ok else EXIT_VERIFY_FAILED

    if args.skip_convert:
        log("--skip-convert set; leaving MBTiles in place")
        return exit_code()

    if not complete:
        # A partial archive is never worth converting. Nothing consumes
        # one: the banked progress that matters is the working MBTiles,
        # which the next resume reads directly, and the published
        # .pmtiles is only ever made from a finished build. The
        # conversion is not free either -- one run spent 11 hours at 2%
        # CPU converting a fragment over NFS -- so this is time taken
        # from the rebuild for an artifact that would be thrown away.
        log(f"PARTIAL ARCHIVE: skipping `pmtiles convert` entirely. The "
            f"working MBTiles at {mbtiles_path} holds the banked progress "
            f"and is what the next run resumes from; keep it. No .pmtiles "
            f"was written, so there is nothing to promote. Exit code "
            f"{EXIT_PARTIAL}.")
        return EXIT_PARTIAL

    args.output.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["pmtiles", "convert", "--force", str(mbtiles_path), str(args.output)]
    log("running: " + " ".join(cmd))
    subprocess.run(cmd, check=True)
    log(f"wrote {args.output} ({args.output.stat().st_size / 1e6:.2f} MB)")
    if not all_ok:
        log(f"VERIFY FAILED: the archive covers the requested scope, but at "
            f"least one sampled tile did not match a direct source read. "
            f"Exit code {EXIT_VERIFY_FAILED}.")
    return exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
