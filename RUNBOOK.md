# RUNBOOK: building and publishing the GEM-15 derivatives

This is the procedure for the heavy builds AGENTS.md says do not belong on a
laptop: `items.parquet`, the overview COG, and the terrain PMTiles. Read
`DESIGN.md` first if you have not -- it is the contract these commands
produce artifacts against. Everything here was smoke-tested on one or a few
cells before being written down (see "Smoke-tested vs. estimated" at the end
of each section); the full-scale numbers are estimates extrapolated from
those smoke tests, not measurements, and are flagged as such throughout.

## 0. Environment ("rails")

Conda/mamba, not pip/uv, because GDAL is a compiled C library with a large
dependency tree and conda-forge is the reliable source for a working GDAL +
rasterio + PROJ pairing. Pin GDAL explicitly; rasterio wheels from PyPI
bundle their own GDAL/PROJ and the two can disagree about where `proj.db`
lives in the same process (we hit exactly this during development -- see
`tools/build_terrain.py`'s `verify_mbtiles_tile`, which uses the `osgeo.gdal`
bindings rather than rasterio for that reason).

```bash
conda create -n gem15 -c conda-forge python=3.11 \
  gdal=3.10 rasterio pyarrow duckdb mercantile pillow requests \
  stac-geoparquet boto3 -y
conda activate gem15
```

Verify:

```bash
python3 -c "from osgeo import gdal; print(gdal.VersionInfo())"   # 3100300 or similar
python3 -c "import stac_geoparquet, duckdb, mercantile, rasterio, PIL"
```

**If GDAL raises `RuntimeError: Invalid SRS for -t_srs` or similar** (e.g.
reprojecting to EPSG:3857 fails outright) **in a fresh micromamba/conda
env**, GDAL cannot find a `proj.db` that matches its own PROJ version --
typically because a `PROJ_DATA` or `PROJ_LIB` left over from a different
environment (system PROJ, another conda env, Homebrew) is still set and
shadows this env's own copy. Point it at the activated env explicitly:

```bash
export PROJ_DATA=$CONDA_PREFIX/share/proj
# older PROJ/GDAL builds read PROJ_LIB instead of PROJ_DATA; harmless to set both
export PROJ_LIB=$CONDA_PREFIX/share/proj
```

This is the same family of issue as the rasterio/GDAL PROJ mismatch noted
above (two PROJ installations disagreeing about `proj.db`'s schema version)
but surfaces differently: that one raised inside `rasterio.crs.CRS.from_epsg`
when a second, incompatible PROJ stack loaded into the same process; this one
is GDAL's own CLI/bindings failing outright because `PROJ_DATA`/`PROJ_LIB`
points at the wrong (or a stale) `proj.db` entirely. Confirm the fix with:

```bash
python3 -c "from osgeo import gdal, osr; print(osr.SpatialReference().ImportFromEPSG(3857))"  # 0 = success
```

`pmtiles` (the go-pmtiles CLI, used by `build_terrain.py` and for the
post-publish checks below) is a separate static binary, not a Python
package:

```bash
# Linux x86_64 (check https://github.com/protomaps/go-pmtiles/releases
# for the current version and your architecture; verified asset names as
# of this writing: go-pmtiles_<version>_Linux_x86_64.tar.gz,
# go-pmtiles_<version>_Linux_arm64.tar.gz, *_Darwin_*.zip for macOS)
curl -L -o /tmp/go-pmtiles.tar.gz \
  https://github.com/protomaps/go-pmtiles/releases/latest/download/go-pmtiles_1.31.2_Linux_x86_64.tar.gz
tar -xzf /tmp/go-pmtiles.tar.gz -C /usr/local/bin pmtiles
pmtiles version
```

Conformance gate (`tests/test_conformance.py`) additionally needs `rashid`:

```bash
python3 -m pip install 'rashid>=0.1.8,<0.2.0'
```

### Credentials

`catalog.publish.yaml` names AWS profile `portolan-mirrors` for
`write_prefix: s3://us-west-2.opendata.source.coop/portolan-mirrors/gem-15-catalog`.
The orchestrator provides the actual credentials for that profile (or sets
`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`/`AWS_SESSION_TOKEN` directly, which
`publish.py`/`upload_data.py` prefer over a named profile) out of band --
**do not put them in this file, in a commit, or in `catalog.publish.yaml`.**
Nothing in this runbook needs credentials until the `upload_data.py --confirm`
and `publish.py --confirm` steps.

**By-hand gateway publishing** (a run from a server with a personal Source
Cooperative login, not the CI role): edit `catalog.publish.yaml` locally and
do not commit the edit — set `write_prefix: s3://portolan-mirrors/gem-15-catalog`,
add `endpoint_url: https://data.source.coop`, and set `profile: source-coop`
(the profile whose `credential_process` calls `source-coop creds`, after an
interactive `source-coop login`). Revert the file before pulling or pushing.

**Stale static keys shadow the login.** The AWS SDK prefers static keys in
`~/.aws/credentials` over a `credential_process` in `~/.aws/config` for the
same profile name. A leftover `[source-coop]` section with old keys makes
every call fail with AccessDenied even after a fresh login. Delete the stale
section, or run every aws/boto3 invocation (including `publish.py` and
`upload_data.py`) with `AWS_SHARED_CREDENTIALS_FILE=/dev/null` in the
environment.

### Compute: this is a Slurm cluster, not a login-node shell

"rails" is a Slurm cluster. Every heavy step (steps 1-4 below) runs via
`sbatch` on a compute node, not interactively on the login node: the login
node has a 64 GiB cgroup `memory.max` (confirmed the hard way -- this is
exactly the kind of cap the pre-patch `build_terrain.py` hit at 60 GiB,
before the memory fixes in step 3 below), and its job is submitting jobs,
not running them.

Partition `cpu_amd`: 112-128 cores, 251-257 GB RAM per node, 1.5 TB
node-local NVMe at `/tmp`, `MaxTime` 7 days. Recommended shape for the
terrain build (step 3), which is both the most CPU-parallel step and the
one with a real local-disk I/O argument for node-local scratch:

```bash
sbatch --partition=cpu_amd --nodes=1 --cpus-per-task=64 --mem=128G \
  --time=2-00:00:00 --job-name=gem15-terrain <<'EOF'
#!/bin/bash
set -euo pipefail
SCRATCH="/tmp/gem15-build-${SLURM_JOB_ID}"
trap 'rm -rf "$SCRATCH"' EXIT
mkdir -p "$SCRATCH"

python3 tools/build_terrain.py --band dsm --minzoom 0 --maxzoom 13 \
  --workers 64 --db-path "$SCRATCH" \
  --output staging/publish/gem-15/terrain/gem-15-dsm-terrarium.pmtiles

# --output above already wrote the final .pmtiles to shared/network
# storage directly (that write is a single sequential pass at the very
# end, not the random-access inserts --db-path was for); only the
# intermediate MBTiles + WAL/SHM lived in $SCRATCH, and the trap above
# cleans them up whether the build succeeds or fails. If the operator's
# site instead wants the MBTiles itself preserved (e.g. to resume a run
# across jobs without redoing a partial zoom), rsync it off $SCRATCH
# before this script exits and point the next job's --db-path at a
# restored copy, not at a fresh empty one.
EOF
```

64 cores comfortably covers `--workers 64` for the fetch stage plus
`--encode-workers 64` (the default, same as `--workers`) for the separate
encode pool running concurrently; 128 GB covers the bounded in-flight
window (`--window-multiplier`, default 2x workers -- see step 3) at z13's
measured 139-199 MB per in-flight cell. Measured GCS throughput from a
compute node: **~325 MB/s over 4 concurrent streams** -- scale
`--workers` down from 64 if this saturates before the CPU does; the fetch
stage is network-bound, not CPU-bound, so more workers past that point
buys nothing.

## 1. `items.parquet`

```bash
python3 tools/build_items_parquet.py --workers 32
```

What it does: crawls `collection.json` -> 297 group catalogs -> 13,485 item
JSONs (all cached under `staging/cache/`, so a rerun only fetches what's
missing), upgrades each to STAC 1.1, sorts by quadkey, writes one row group
to `staging/publish/gem-15/items.parquet`, then validates the output with
DuckDB (count, bbox extent, a sample row -- printed to stdout; read it).

**Smoke-tested:** `--limit 40` (`staging/smoke/items-sample.parquet`), full
pipeline including the DuckDB validation. 40 items -> 0.04 MB; network fetch
of 40 cold-cache items ran at ~50 items/s with 16 workers.

**Estimated, not measured, for the full 13,485:**
- Group-catalog crawl (297 small JSON files): seconds, regardless of worker
  count -- it already ran in full during every smoke test above.
- Item fetch: ~5 KB/item JSON * 13,485 ≈ 65-70 MB total download. At the
  observed 50 items/s (16 workers, a home connection) that's ~4.5 minutes;
  with `--workers 32-64` on a well-connected server, expect roughly
  1-2 minutes.
- Output size: the smoke file's 1 KB/item does not extrapolate linearly --
  at 40 rows almost nothing is dictionary-compressible, but at 13,485 rows
  in one row group, the identical per-band `description`/`title` strings and
  repeated `stac_extensions` entries should compress hard. Expect roughly
  5-20 MB; the DuckDB validation step at the end of the real run will give
  the true number.

## 2. Overview COG

```bash
python3 tools/build_overview.py --workers 16
```

Renders every cell's DSM band 1 at the 480 m internal overview, warped to
EPSG:3857 at 960 m/px, through the cyclic-palette-times-hillshade look
(`docs/research/gem15-example.js`), caches each cell's RGBA tile under
`staging/cache/overview/` (resumable -- rerun skips cached cells), then
mosaics everything into `staging/publish/gem-15/gem-15_overview.tif` with:

```
gdal_translate <mosaic.vrt> gem-15_overview.tif -of COG -co COMPRESS=ZSTD \
  -co BLOCKSIZE=512 -co OVERVIEW_RESAMPLING=AVERAGE -stats
```

(printed by the script itself, and written verbatim, with the rest of the
render pipeline described in prose, to `gem-15_overview.lineage.txt` next to
the output -- `update_asset_facts.py` picks this sidecar up automatically.)

**Smoke-tested:** `--cells wm91sm1` and `--cells wm91sm1,w2s41`
(`staging/smoke/overview-sample*.tif`), including the full mosaic-to-COG
step and a visual/statistical check of the alpha mask over a mostly-ocean
cell.

**Estimated, not measured:**
- Per-cell render reads the small 480 m overview over `/vsicurl/`
  (tens of KB to low MB per cell) -- latency-bound, not bandwidth-bound, so
  parallelism matters more than bandwidth here. At the observed
  ~0.6-1.1 s/cell on a single worker, 13,485 cells at `--workers 16-32`
  is roughly 20-45 minutes of fetching + rendering.
- The final `gdal_translate -of COG` pass re-reads all ~13,485 cached
  tiles sequentially to build the VRT and the output's own overviews. This
  is effectively a local-disk, mostly single-threaded step; budget another
  15-60 minutes depending on disk speed.
- Output size: full global coverage at 960 m/px is roughly 42,000 x
  21,000-30,000 px (84N-61S is not symmetric around the equator in Web
  Mercator), 4-band uint8. Raw, that is several GB; ZSTD on a cyclic,
  high-frequency palette image compresses much less than it would on
  smooth photographic imagery. Expect several hundred MB to ~2 GB --
  validate against the real file once built, and if it is materially
  larger, consider a coarser `--resolution-m` for the collection asset
  (the brief's own target was ~960 m; this is not a hard requirement).

## 3. Global terrain PMTiles (DSM, z0-z13)

```bash
python3 tools/build_terrain.py --band dsm --minzoom 0 --maxzoom 13 \
  --workers 64 --db-path "$SCRATCH" \
  --output staging/publish/gem-15/terrain/gem-15-dsm-terrarium.pmtiles
```

(run inside the sbatch shape in "Compute" above, not interactively). This
is the expensive step, and the one the first attempt at it on the real
cluster found three serious defects in -- all three are fixed as of this
revision of `tools/build_terrain.py`; read its module docstring for the
full design, this section is the operational summary and the honest cost
estimate.

### What the real run found, and what changed

1. **P0: a transient fetch failure permanently baked in false ocean.**
   The real run hit transient GCS failures (`HTTP response code: 0`). The
   original code treated a failed cell open/warp/read exactly like a cell
   with no data at all -- its tiles got finalized as 0 m and marked done,
   and a rerun would never revisit them (they were, as far as the MBTiles
   was concerned, finished). **Fixed**: a cell's tiles are only ever
   decremented toward "done" on a *successful* fetch; a failed cell (after
   `CELL_READ_ATTEMPTS` in-process retries, on top of GDAL's own HTTP-layer
   retry -- see `VSICURL_ENV`'s `GDAL_HTTP_MAX_RETRY`/`RETRY_DELAY`, which
   alone eliminated every transient failure on the real restart) simply
   leaves its tiles unfinished; there is no more "finalize whatever's left
   over" sweep at the end of a zoom, which is exactly what used to turn a
   transient failure into permanent wrong data. **Verified** (not just
   argued): a cell pointed at a nonexistent host left its 36 tiles
   completely absent from the MBTiles; rerunning with the real URL
   completed all 36 without redoing anything else.
2. **Memory blowup.** All of a zoom's cell fetches were submitted to the
   thread pool at once; each result held 139-199 MB (measured at z13), and
   the single thread doing all the compositing+encoding ran 3-7x behind the
   producers, growing RSS ~23 MB/s until a 60 GiB cgroup cap killed the
   job (measured; this is also why the login node's 64 GiB cap in
   "Compute" above is worth knowing about independently). **Fixed**: a
   bounded in-flight window (`--window-multiplier`, default 2x
   `--workers`) caps how many cell results can be waiting at once, and PNG
   /terrarium encoding moved to its own thread pool (`--encode-workers`,
   default = `--workers`) instead of running inline on the thread also
   doing compositing and submitting fetches -- Pillow's PNG encoder
   releases the GIL during the actual compression, so a thread pool (not a
   process pool, which would add numpy-array pickling for no benefit)
   genuinely parallelizes it.
3. **A kill lost everything since the last zoom boundary.** The only
   `conn.commit()` was once per zoom; two separate kills on the real run
   rolled back 2.8 GB and 927 MB respectively. **Fixed**: WAL journal mode
   + `synchronous=NORMAL`, and a commit every `--commit-every-tiles`
   (default 20,000) or `--commit-every-seconds` (default 60), whichever
   comes first. **Verified**: built z12 across the Barcelona bbox with
   `--commit-every-tiles 20`, `kill -9`'d the process mid-zoom, and found
   301 of 740 tiles already durable in the MBTiles; a rerun resumed from
   exactly that point and reached the full 740 without redoing them.
4. **Duplicate metadata rows on every rerun.** The `metadata` table had no
   `PRIMARY KEY`, so `INSERT OR REPLACE` had nothing to conflict on.
   **Fixed**: the table is now created with `PRIMARY KEY(name)`; an
   existing database from an older version of this script is migrated
   (deduplicated, keeping the latest value per name) the first time it is
   opened by this version.

### `--overview-mode`: average (new default) vs. rewarp (the original design)

The original design re-warped every zoom independently from source --
correct, but it is why the egress estimate below is so large: z13's
~19.1 m/px falls between the 15 m native level and the 30 m overview, so
*every* zoom down to z9 still reads a large fraction of full-resolution
data per cell (see the table). `--overview-mode average` (the default now)
warps only `--maxzoom` from source, then derives every shallower zoom by
NaN-aware 2x2 box-averaging the zoom one level deeper, already sitting in
the working MBTiles -- no further source reads. `--overview-mode rewarp`
keeps the original behavior as an A/B option.

Box-averaging needs each tile's real pre-fill void mask (not just its
already-0-filled elevation) to average correctly across a coastline or a
masked-water edge without a void pixel incorrectly dragging a real one
toward 0; this is stored per-tile as a packed-bit mask in a new
`void_masks` table (8192 bytes/tile), written for *every* tile regardless
of mode.

**Per-zoom vertical quantization** (Mapterhorn's technique, see the module
docstring's step table: 1 m at z0-z11, 0.5 m at z12, 0.25 m at z13) runs in
both modes before encoding, `--no-quantize` to disable. **Measured** on a
single cell (w2s41) at z10-z13: 26.27 MB quantized vs. 54.45 MB
unquantized, **-51.75%** -- close to the -54.7% the operator measured on
the actual Barcelona demo archive; the gap is just that this was one
cell, not the whole demo bbox.

**Average vs. rewarp agreement, measured** (not assumed): built both modes
over the same single cell (w2s41) at z10-z13 and diffed every tile.
Edge tiles (a single-cell build's own boundary, where a neighboring cell's
contribution is simply absent -- see "Sharded runs" below) disagree far
more than interior ones and are not representative of a real, unrestricted
build; filtering to interior tiles only, max |average - rewarp| as a
fraction of each zoom's resolution: z12 (one level below maxzoom) up to
69% (mean ~25%), z11 up to 18% (mean ~9%), z10 up to 11% (mean ~9%).
Disagreement is largest immediately below `--maxzoom`, where the box mean
is averaging full-detail z13 data against a single bilinear point-sample
of that same area; it shrinks at shallower zooms because both methods are
by then looking at data the average chain has already smoothed once. This
is why `--verify`'s tolerance for a derived tile
(`RELATIVE_TOLERANCE_AVERAGED` in `tools/build_terrain.py`, 15%) is wider
than for a rewarped one (`RELATIVE_TOLERANCE`, 2%) -- it is a different,
legitimate resampling philosophy, not a bug, and the full numbers above are
in that constant's own comment. z13 itself is bit-identical between modes
in this test (0.000 m), as it should be: both modes build it the same way.

### Egress and time estimate (average mode; still an estimate)

Average mode reads source only at `--maxzoom`. Reusing the original
rewarp-mode accounting (worked out from GDAL's overview-selection rule --
pick the finest overview whose resolution is still <= the zoom's target --
against the DSM COGs' 15/30/60/120/240/480 m overview ladder, apportioned
by pixel count from the real per-cell `file:size` values in the upstream
item JSON, e.g. 199,399,399 bytes for `wm91sm1`), z13 alone is **~1.9 TB**
of the previous ~2.5-3 TB rewarp-mode total -- average mode's total egress
*is* that ~1.9 TB, since nothing else reads source. (This is a ~35-45%
egress reduction over the old all-zooms design, concentrated entirely in
not re-reading zooms 0-12; it is not the "~4x" figure floated before any
of this was measured -- correcting that here rather than repeating it.)

At the cluster's measured ~325 MB/s over 4 streams (see "Compute" above),
that is very roughly **1.6-2 hours** for the z13 fetch+warp stage alone.
Deriving z0-z12 from z13 is local-disk-bound, not network-bound (see the
derivation timings in the smoke test below: hundreds of tiles/second on a
laptop), so it adds minutes, not hours, on a 64-core node. **Overall
estimate: ~2-3 hours for the full z0-z13 global build on one
`cpu_amd` node** -- still an estimate, now a better-grounded one than the
pre-patch 1.5-6 hours, and still pending the operator's own timing pass on
the real cluster with the patched code.

**Memory:** the per-zoom "tiles remaining" count (`--maxzoom`'s rewarp
stage) and the fetch window (2x `--workers` in-flight cell results, each
139-199 MB at z13) are the two real costs; derivation reads small (256x256)
tiles from local SQLite and is not a memory concern. 128 GB (the sbatch
shape above) is generous headroom for both at `--workers 64`.

**Resumability**, now accurate post-patch: every `--commit-every-tiles`/
`--commit-every-seconds` commit is durable (not just once per zoom), a
failed cell's tiles are never falsely finalized (see defect 1 above), and
`existing_tiles_for_zoom` means a rerun only ever redoes what is actually
missing -- at the tile level for the rewarp stage, and at the derived-tile
level for every shallower zoom in average mode. `pmtiles convert --force`
at the end always regenerates the final `.pmtiles` from the MBTiles'
current state.

### Smoke-tested

`--cells wm91sm1 --minzoom 0 --maxzoom 11 --verify`
(`staging/smoke/terrain-sample.pmtiles`): 58 tiles built (average mode,
quantized), every zoom's `--verify` check passed, `pmtiles show`
round-tripped metadata including `encoding: terrarium` and the new
`overview_mode` key, and a rerun correctly reported "nothing to do" (the
rewarp zoom) / "nothing to derive" (every shallower zoom) everywhere.

`verify_mbtiles_tile` also needed two fixes of its own during development
(both on the original rewarp-only design, both still in effect): a sampling
bug where a fixed grid of points could miss a cell's tiny footprint inside
a much larger shallow-zoom tile and wrongly conclude "no data anywhere,
must be void" (fixed: enumerate every real-data pixel the reference warp
actually has, instead of guessing where they are), and a GDAL
warp-chunking sensitivity where two independently-issued bilinear warps of
the same steep-terrain pixel can legitimately disagree by a few meters with
neither one wrong (fixed: scale the tolerance to each zoom's own
resolution, `RELATIVE_TOLERANCE`). Both are documented at length in
`verify_mbtiles_tile`'s own docstring and comments.

**This still catches real mismatches**, verified directly: a tile's
elevation was decoded, perturbed by a deliberate +500 m offset (simulating
e.g. a dropped `+32768` or wrong band), re-encoded, and written back;
`verify_mbtiles_tile` correctly reported `FAIL` against the same tile
passing before the perturbation. The scaled tolerances above only ever
reach a few meters to tens of meters even at the shallowest/coarsest
zooms -- nowhere near large enough to also pass a genuine encoding bug,
which misses by orders of magnitude more.

### DTM terrain (optional, not in the published asset contract yet)

```bash
python3 tools/build_terrain.py --band dtm --minzoom 0 --maxzoom 13 \
  --output staging/publish/gem-15/terrain/gem-15-dtm-terrarium.pmtiles
```

Same pipeline, same cost profile (DTM mosaics are single-band but the same
size class). Not currently referenced by `collection.json`'s asset
contract -- add it there first if you publish it.

## 4. Barcelona demo PMTiles (z0-z14)

```bash
python3 tools/build_terrain.py --band dsm --minzoom 0 --maxzoom 14 \
  --bbox 1.0,40.8,3.3,42.0 \
  --output staging/publish/gem-15/terrain/gem-15-dsm-terrarium-demo-barcelona.pmtiles
```

The bbox is generous around the `w1s41`/`w2s41`/`w3s40`/`w3s41` cells (the
same four `build_thumbnail.py` selects); at z0-z7 this really is "a handful
of tiles," per DESIGN.md, since only cells intersecting the bbox are ever
considered.

**Measured on the rails server** (not just smoke-tested): 15,148 tiles,
547.5 MB, finished in minutes. `--verify` decode-vs-direct-read errors:
0.0686 m at z7, 0.0017 m at z8 -- both comfortably inside even the
unscaled 1 m default tolerance, consistent with this being a land-rich
area with plenty of interior, non-steep pixels at every zoom (unlike the
wm91sm1 smoke-test cell, which is why wm91sm1 found the sampling and
warp-chunking issues described in step 3 and this real-world run did not).

**That measured run predates this revision of `build_terrain.py`** (it
used the original rewarp-only design, no quantization, no `void_masks`,
none of step 3's P0 fixes). It is still live and still correct -- nothing
above suggests its bytes are wrong -- but a rebuild with the current
script would both apply quantization (measured -51.75% on a single cell in
step 3; expect a similarly large reduction here) and default to
`--overview-mode average`, reading source only at `--maxzoom` (14 for this
file, not 13) instead of at every zoom from 0 to 14. Worth doing before
the global build if this demo's size/egress matters on its own, but not
required: this is a judgment call for whoever runs it, not a correction to
anything already published.

## 5. Thumbnail (committed to git, not staged)

```bash
python3 tools/build_thumbnail.py
```

Writes `catalog/gem-15/thumbnail.png` directly (not `staging/`) -- this is
the one derived file the project commits, since it is a small PNG, not a
GeoParquet/COG/PMTiles. **Already run for real** as part of this build (not
just smoke-tested): see the file in `catalog/gem-15/thumbnail.png`.

## 6. Asset facts

```bash
python3 tools/update_asset_facts.py --dry-run   # review the diff first
python3 tools/update_asset_facts.py             # apply
```

Updates `file:size`/`file:checksum` (multihash `1220<sha256 hex>`) on every
asset above that exists under `staging/publish/gem-15/` (plus the
thumbnail, under `catalog/gem-15/`), `proj:shape`/`proj:transform`/`bands`
statistics on the overview asset specifically, and adds the `thumbnail`
asset entry to `collection.json` if it is not already there. Idempotent --
rerun it after any rebuild and only genuinely changed facts are touched;
`processing:lineage` and every hand-written field are left alone unless a
lineage field is missing, in which case it is filled from the matching
`.lineage.txt` sidecar (currently only `build_overview.py` writes one).
Prints a diff summary either way.

**Smoke-tested** against a scratch copy of `collection.json` with the
smoke-scale artifacts copied into the expected `staging/publish/gem-15/`
paths: 14 changes on first run (including adding the `thumbnail` asset),
zero changes on a second run against the same files (idempotency
confirmed). Not run against the real `collection.json` with real
full-scale artifacts, since those do not exist yet on a laptop -- run it
for real right after step 3/4 produce the real files.

## 7. Upload the data files

```bash
python3 tools/upload_data.py                # dry run: review the file list
python3 tools/upload_data.py --confirm
```

`PUBLISHABLE_SUFFIXES` in `tools/upload_data.py` already allows
`.parquet`/`.pmtiles`/`.tif`/`.tiff`/`.laz`, which covers every file this
pipeline stages. **No change was needed or made.** Two things this
allow-list correctly keeps out of the bucket, by design: the
`gem-15_overview.lineage.txt` sidecar (step 2) and the per-cell tile caches
under `staging/cache/` -- neither should ever be uploaded, and the
suffix gate excludes both without any special-casing.

## 8. Publish the catalog metadata

```bash
python3 tools/publish.py                    # dry run
python3 tools/publish.py --confirm
```

Run this *after* step 7, not before: `collection.json`'s asset `file:size`/
`file:checksum` (from step 6) should describe bytes that are already at
their public URLs by the time the metadata referencing them goes live.

## 9. Post-publish verification

```bash
# 1. Full conformance check, byte checks AND a live server probe.
#    tests/test_conformance.py (the CI gate) passes --no-data for speed and
#    never passes --live at all; this is the full local+remote check,
#    confirmed against `rashid check --help` (rashid 0.1.8):
#    --data/--no-data verifies asset bytes (checksum/size/format/extent,
#    on by default); --live additionally probes the hosting servers
#    (HTTP range + CORS) and needs --live-base-url to resolve this
#    catalog's relative hrefs against the published location.
rashid check catalog/ --live --live-base-url https://data.source.coop/portolan-mirrors/gem-15-catalog

# 2. Re-run the query recipes in catalog/gem-15/AGENTS.md's "Query
#    recipes" and "Runnable access patterns" sections verbatim, but
#    against the now-published URL (they already use it):
#    https://data.source.coop/portolan-mirrors/gem-15-catalog/gem-15/items.parquet

# 3. pmtiles show against the published archive (works on a remote URL
#    directly, per `pmtiles show --help`):
pmtiles show https://data.source.coop/portolan-mirrors/gem-15-catalog/gem-15/terrain/gem-15-dsm-terrarium.pmtiles
pmtiles show https://data.source.coop/portolan-mirrors/gem-15-catalog/gem-15/terrain/gem-15-dsm-terrarium-demo-barcelona.pmtiles

# 4. MapLibre smoke check: pmtiles.io is a protomaps-hosted viewer that
#    renders a remote PMTiles archive with no setup --
#      https://pmtiles.io/#url=https://data.source.coop/portolan-mirrors/gem-15-catalog/gem-15/terrain/gem-15-dsm-terrarium.pmtiles
#    For the actual styles, open each of catalog/gem-15/styles/*.json in a
#    minimal MapLibre GL JS page (`map.setStyle('<published style URL>')`)
#    and confirm hillshade/hypsometric tint/3D terrain render over
#    Barcelona without console errors -- particularly that the PMTiles
#    protocol is registered (`maplibregl.addProtocol('pmtiles', ...)`)
#    before `setStyle` runs, since the styles reference `pmtiles://` URLs.
```

## Appendix: scale-down for a single country

Every heavy script takes `--bbox minlon,minlat,maxlon,maxlat` (or
`--cells id1,id2,...`). For a country-scale dry run before committing to
the global build, pick a bbox and reuse every command above unchanged
except for that one flag, e.g. mainland Spain plus the Balearics and
Canaries excluded (roughly):

```bash
BBOX=-9.5,36.0,3.5,43.8

python3 tools/build_overview.py --bbox $BBOX --workers 16
python3 tools/build_terrain.py --band dsm --minzoom 0 --maxzoom 13 --bbox $BBOX \
  --output /tmp/country-dryrun-dsm-terrarium.pmtiles
```

`build_items_parquet.py` has no `--bbox` (it always builds the full item
index -- a per-country subset of 13,485 small JSON rows is not worth adding
scope-selection logic for); use `--limit` for a quick sanity run instead.
There is no country filter for `build_thumbnail.py` either -- it always
renders the fixed Barcelona demo area.

**For `build_terrain.py` specifically, a `--bbox`/`--cells` run is a
standalone dry run only -- never point it at the paths a global run will
also use.** (An earlier version of this appendix said the opposite --
that a country run was "a legitimate way to warm the cache" for the global
one -- which is wrong and has been corrected here.) Adjacent cells can
share up to 23 z13 tiles across a single 1-degree boundary; a sharded
run's own boundary tiles get finalized using only the cells it was given,
permanently baking in 0 m for a neighbor that a global run would have
supplied, and a later global run sees those tiles as already done and
skips them. This was found by direct measurement while validating this
revision of the script (see step 3's "average vs. rewarp agreement"): a
single-cell build's edge tiles disagreed with an independent reference by
far more than its interior tiles, for exactly this reason. `--output`
above is deliberately `/tmp`, not `staging/publish/...`, to make that
boundary obvious; the example command's own `$BBOX` run is fine to repeat
or discard freely, just never reuse its `.mbtiles`/`.pmtiles` as a
starting point for the real build.

`build_overview.py` and `build_items_parquet.py` have no such hazard (an
overview tile's cells mosaic independently with no cross-cell
compositing, and the item index has no spatial compositing at all), and
both still share the resumable `staging/cache/` item/group-catalog cache
with a later global run regardless of `--bbox`.
