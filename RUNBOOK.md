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
  stac-geoparquet boto3 pyproj scipy -y
conda activate gem15
```

(`pyproj` and `scipy` added for `build_terrain.py`'s void-filling: the
EGM2008->EGM96 geoid conversion and the void-boundary feather,
respectively -- see step 3.)

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

**Void-filling (step 3, on by default) needs two PROJ geoid grids**
(`us_nga_egm08_25`, `us_nga_egm96_15` -- verified against `projsync
--list-files`, 2026-11-15; an earlier draft of this runbook named
`us_nga_egm2008_1`, which does not exist in PROJ's catalog at all and
would make `projsync` fail outright with a confusing "not found") that
conda-forge's `proj` package does not bundle. `build_terrain.py` fetches
them over the network the first time it runs, which needs
`PROJ_NETWORK=ON`:

```bash
export PROJ_NETWORK=ON
```

or, if the compute nodes have no outbound internet access (common on an
HPC cluster -- check before assuming either way), pre-fetch them once
while network access is available and point `PROJ_DATA` at a directory
that has them. `us_nga_egm08_25.tif` is 80.5 MB (world coverage, 2.5
arcmin); `us_nga_egm96_15.tif` is 2.7 MB:

```bash
projsync --file us_nga_egm08_25.tif --file us_nga_egm96_15.tif --target-dir "$CONDA_PREFIX/share/proj"
```

`build_terrain.py` checks this itself, once, before any build work
(`check_void_fill_prerequisites`), and exits with a clear message instead
of ever filling a void with an unconverted height if the grids are not
reachable either way. The check does not just ask PROJ for a number and
trust it: PROJ's default behavior when the real grid-based transformation
is unavailable is to silently substitute a "ballpark" (uncorrected,
literally `+proj=noop`) one instead of raising, which returns an exact,
finite 0.0 m everywhere -- confirmed by reproducing it directly (grids
hidden, `PROJ_NETWORK=OFF`): a naive "is the result a finite number" check
passes on exactly that bad case. The fix is `allow_ballpark=False` on the
`Transformer` (which turns that case into a loud `ProjError` instead),
plus two more belt-and-braces checks in case some other PROJ/pyproj
version ever lets a ballpark operation through anyway despite that flag:
inspecting the selected transformation's own `.description` for the
literal string "ballpark" (PROJ's own wording, confirmed with `projinfo -s
EPSG:3855 -t EPSG:5773`), and rejecting an exact `0.0` result at the
startup probe point specifically (the true offset at 0N,0E is +0.064 m,
confirmed below -- never exactly zero). `--no-void-fill` skips the check
and the whole feature, if that is ever the intent.

```bash
python3 -c "
from pyproj import Transformer
t = Transformer.from_crs('EPSG:4326+3855', 'EPSG:4326+5773', always_xy=True, allow_ballpark=False)
print(t.description)                  # must NOT contain 'ballpark'
print(t.transform(0.0, 0.0, 0.0))     # (0.0, 0.0, 0.0635...) -- +0.064 m, not exactly 0
print(t.transform(2.40, 41.97, 0.0))  # Sau reservoir: +0.124 m
"
```

**Landmine: conda-forge's `pyproj` wheel bundles its own `proj.db`, and it
wins over `PROJ_DATA`.** pyproj's internal data-directory lookup checks
its own bundled `proj_dir/share/proj` *before* `PROJ_DATA`/`PROJ_LIB`, so
setting either of those (the fix earlier in this section, for the
rasterio/GDAL mismatch) does not necessarily change what `pyproj`
specifically uses -- only what `osgeo.gdal`/`rasterio` use. If the
bundled `proj.db`'s schema version doesn't match the environment's own
`libproj` (measured on the build server: pyproj 3.8.0 bundling `proj.db`
9.8.1 against a `libproj` 9.9.0 from conda-forge's `proj` package), every
CRS lookup through pyproj breaks -- not just void-filling, since
`check_void_fill_prerequisites` is simply the first thing in this
pipeline that happens to touch a compound CRS construction, and a
version-mismatched `proj.db` throwing on that is the same failure family
documented for rasterio above, just inside a different package.
**Symptom**: a `pyproj.exceptions.CRSError` or a PROJ "database layout
version" error even with `PROJ_NETWORK=ON` and real grids reachable, that
does not go away when `PROJ_DATA` is set correctly. **A plain
`pip install --upgrade pyproj` or `conda install pyproj` re-pins the
bundled file and can silently restore the stale version** even after
you've "fixed" it once -- this is why the symptom can reappear after an
otherwise unrelated environment update. Fix: either move the bundled file
aside so pyproj falls through to `PROJ_DATA` --

```bash
PYPROJ_DATA_DIR=$(python3 -c "import pyproj; print(pyproj.datadir.get_data_dir())")
mv "$PYPROJ_DATA_DIR/proj.db" "$PYPROJ_DATA_DIR/proj.db.bundled-bak"
```

-- or align versions so there is nothing to shadow (reinstall `pyproj`
from conda-forge rather than PyPI, matching the environment's own `proj`
package version, so both read the same `proj.db` in the first place).
Confirm the fix with the same `allow_ballpark=False` snippet above.

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
DEST_DIR="staging/publish/gem-15/terrain"
DEST="$DEST_DIR/gem-15-dsm-terrarium.pmtiles"
mkdir -p "$SCRATCH" "$DEST_DIR"

# No EXIT trap here, on purpose -- see "Artifact integrity" below for why.
# Everything this build produces (the working MBTiles/WAL/SHM, and the
# --output .pmtiles itself) stays on node-local scratch until the copy at
# the bottom of this script has been independently verified.
#
# The status is captured rather than allowed to kill the script, because
# exit code 3 means "finished, but this archive does not cover the
# requested scope" (build_terrain.py's PARTIAL summary). Banking a
# verified fragment is the point of a resumable build, so the copy below
# still runs; the script then exits with the build's own status, so the
# job is not recorded as a success. Under `set -e` an uncaptured exit 3
# would skip the copy entirely and throw the banked progress away.
BUILD_STATUS=0
python3 tools/build_terrain.py --band dsm --minzoom 0 --maxzoom 14 \
  --workers 64 --db-path "$SCRATCH" \
  --output "$SCRATCH/gem-15-dsm-terrarium.pmtiles" || BUILD_STATUS=$?

# Exit 3 is PARTIAL. The script writes no .pmtiles in that case, on
# purpose, so there is nothing to promote and nothing to verify. The
# banked progress is the working MBTiles, which the next resume reads
# directly -- so leave $SCRATCH alone and let the next leg continue from
# it. Converting a fragment is pure waste: one leg spent 11 hours at 2%
# CPU doing exactly that over NFS, for an artifact that was discarded.
if [ "$BUILD_STATUS" -eq 3 ]; then
  echo "PARTIAL leg. No .pmtiles produced, nothing promoted." >&2
  echo "Working MBTiles kept at $SCRATCH for the next resume. NOT cleaned up." >&2
  exit "$BUILD_STATUS"
fi

SRC="$SCRATCH/gem-15-dsm-terrarium.pmtiles"
rsync -a "$SRC" "$DEST"
sync   # force the copy's bytes to actual storage, not just cached in RAM,
       # before the checksum below is allowed to trust them (NFS: this
       # still matters -- it is what turns a client-buffered write into a
       # server-committed one under close-to-open semantics)
SRC_SHA=$(sha256sum "$SRC" | cut -d' ' -f1)
DEST_SHA=$(sha256sum "$DEST" | cut -d' ' -f1)
if [ "$SRC_SHA" != "$DEST_SHA" ]; then
  echo "FATAL: $DEST (sha256 $DEST_SHA) does not match $SRC (sha256 $SRC_SHA)." >&2
  echo "Scratch at $SCRATCH left in place for inspection; not cleaned up." >&2
  exit 1
fi
echo "verified: $DEST matches $SRC (sha256 $DEST_SHA)"

# Only reached once the checksum above has already matched, AND only on a
# complete build: cleanup is a consequence of verified success, not of
# this script exiting one way or another (which is also why there is no
# `trap ... EXIT rm -rf` anywhere in this script -- see below). A
# --verify mismatch keeps the scratch too, because that is exactly the
# state worth inspecting.
if [ "$BUILD_STATUS" -eq 0 ]; then
  rm -rf "$SCRATCH"
else
  echo "Exit $BUILD_STATUS: keeping $SCRATCH for inspection." >&2
fi

# The artifact is banked and verified. Now report the build's own verdict:
# 0 complete, 1 a --verify mismatch. (3, PARTIAL, already exited above.)
exit "$BUILD_STATUS"
EOF
```

**Artifact integrity: why there is no `trap ... EXIT` here, and why "the
size looked right" is not a verification.** A real incident on this
cluster: an 11.8 GB MBTiles on NFS was found truncated to a 32 KB
empty-schema sparse file *minutes after* two independent `ls -la` size
checks had already confirmed the full 11.8 GB was there. Opening it
read-only and running a SQLite query against it over the WAL was tested
and exonerated (not the cause). The prime suspect, structurally: a
cleanup step (an `EXIT` trap deleting or truncating scratch, or a second,
overlapping copy) racing the first copy, triggered by the job ending (or
by the trap firing more than once -- an `EXIT` trap fires on *every* exit
path: normal completion, a later `exit` in the same script, even a
signal, and can legitimately run twice if more than one of those happens
in the same job). Checking a file's size twice, even "independently," does
not rule this out: both checks can land in the window after a truncating
write has started but before it reaches whatever offset the check reads,
on a filesystem (NFS) where metadata like size is not synchronized with
data the way local-disk intuition expects.

The fix is structural, not a better check: make the failure mode
impossible to reach, rather than detecting it after the fact.
1. **No `trap ... EXIT` that deletes or moves anything, anywhere in the
   job script.** A trap cannot be made to "only fire once at the real
   end" -- that is not what `EXIT` traps are for. Cleanup is instead the
   literal last line of the success path, nothing else.
2. **Copy, then force it to stable storage, then hash both sides, in that
   order, before anything is allowed to call the copy "done."** `rsync`
   finishing is not evidence the destination's bytes are durable, only
   that it issued the writes; `sync` (or an `fsync` targeted at the
   destination file specifically, equivalent here) is the step that
   actually waits for them. The sha256 comparison is the only thing in
   this script that may legitimately be trusted as "the destination is
   correct" -- a matching size is not that, for exactly the reason above.
3. **Cleanup only after a verified match, never otherwise.** A checksum
   mismatch exits non-zero with scratch left in place on purpose --
   exactly the forensic evidence the real incident above did not have,
   since whatever a trap deleted was gone by the time anyone looked.

64 cores comfortably covers `--workers 64` for the fetch stage plus
`--encode-workers 64` (the default, same as `--workers`) for the separate
encode pool running concurrently; 128 GB covers the bounded in-flight
window (`--window-multiplier`, default 2x workers -- see step 3) at the
139-199 MB per in-flight cell measured at z13. A cell's in-flight warp is
sized by the source window it reads, which is the same native level at z14,
so that figure still applies. Measured GCS throughput from a
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

## 3. Global terrain PMTiles (DSM, z0-z14)

```bash
python3 tools/build_terrain.py --band dsm --minzoom 0 --maxzoom 14 \
  --workers 64 --db-path "$SCRATCH" \
  --output "$SCRATCH/gem-15-dsm-terrarium.pmtiles"
```

(run inside the sbatch shape in "Compute" above, not interactively --
including its copy/verify/cleanup sequence after this command: `--output`
above is deliberately scratch-local too, not the final published path
directly, per that section's "Artifact integrity" note). This
is the expensive step, and the one every real attempt at it on the
cluster has found defects in -- all of them are fixed as of this revision
of `tools/build_terrain.py`; read its module docstring for the full
design, this section is the operational summary and the honest cost
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

The first **global z0-z14** run (13,485 cells, 32 workers, 200 GB) then
found three more, at a scale no earlier validation had reached:

5. **P0: the per-zoom setup cost scaled with the requested scope, not with
   concurrency.** `build_zoom_rewarp` materialized one `mercantile.Tile`
   object per cell per tile plus one contributor-count dict entry per
   tile, for the whole requested area, before any raster I/O. Measured
   against the real cell list at global z14 (13,485 cells, 42.138M
   cell-tile contributions over 40.753M distinct tiles): **213.7 bytes per
   contribution, so 9.0 GB**, held for the entire zoom. The resume skip
   list added 151.6 bytes per already-built tile (0.42 GB at the 2.78M
   tiles that run had banked, 6.2 GB for a complete zoom), and the first
   derive step's child set added about 6 GB more. Against a validated
   steady state of ~131 GiB at 32 workers this is what pushed the job over
   its 200 GB limit; it was invisible in every earlier test because those
   ran on 1-99 cells. **Fixed**: a cell's tile set is four integers
   (`cell_tile_rect`), the contributor count is derived one cell at a time
   from integer rect intersections as that cell is admitted, and every
   per-tile set is a packed-key numpy array at 8 bytes per tile.
   **Measured after**, at the same real global z14 scope, not
   extrapolated: **3.8 MB** of upfront structures (9.6 MB RSS), 0.2 s to
   build, 165.6 KB transient per in-flight cell (21.2 MB for a 128-cell
   window), and 22.2 MB of skip list at the banked resume point (326 MB
   for a complete zoom). The remaining in-flight cost is the one that was
   always meant to be there: the fetch window, plus the open
   shared-boundary tiles, which peak at **9,401 tiles (2.46 GB)** under
   the real admission order and drain to zero. The per-zoom progress line
   reports that backlog as `pending (peak N)`.
6. **An interrupted run's metadata claimed the whole requested range.**
   `minzoom`/`maxzoom` were written once, at the start, from
   `--minzoom`/`--maxzoom`, so the 9.6% fragment that survived the kill
   advertised z0-z14 while holding only z14. **Fixed**: both values are
   written from the zoom levels the `tiles` table actually holds, on every
   commit as well as at the end, so even a `kill -9` leaves a fragment
   that describes itself.
7. **The closing summary read as a clean completion.** Per-cell failures
   were warnings far up the log, the last lines were a tile count and a
   file size with nothing to compare them against, and the exit status was
   0 -- so the job script promoted a fragment as a finished archive.
   **Fixed**: every run ends with cells completed against cells requested,
   zoom levels wanted against zoom levels built, per-zoom tile counts read
   from the archive itself, and an explicit `PARTIAL` line with **exit code
   3** when the archive does not cover the request. Promoting a verified
   fragment anyway is still correct -- that is what banking progress means
   -- which is why the sbatch shape in "Compute" above captures the status
   and exits with it after the copy, rather than letting `set -e` skip the
   copy and throw the banked work away.

The patched run was then killed again at the same cap, at roughly the same
progress point, with the process using under 17 GiB. That found the last
one:

8. **The working database's page cache was being charged to the job.**
   Every tile carried a flat 8,192-byte `void_masks` row, so the working
   MBTiles grows 24.2 MiB per processed cell and would reach ~334 GB of
   masks at global z14, against ~40 GB of finished archive. Under cgroup
   v2 that file's page cache counts toward `memory.max`. **Fixed** (the
   file size, not the accounting): a uniform mask -- 96.4% of them,
   measured -- is now one byte, which takes the mask table to roughly
   12-15 GB. `unpack_mask` still reads the 8,192-byte form, so the banked
   archive resumes with no migration and ends up holding both forms.
   `--memory-log N` now reports the cgroup's anon/file/shmem split next to
   process RSS and the database size, so this is visible rather than
   inferred. See "The working database is the other 334 GB" below for the
   measured numbers and for the copy/checksum caveat on resume.
9. **The encode queue was unbounded, and at 24 workers it ran away.**
   Nothing capped how many tiles could sit waiting to be encoded. One
   cell's compositing submits every tile only that cell covers -- about
   2,900 of 3,022 at global z14 -- in a tight loop, and each queued tile
   pins a 256 KB elevation array in its encode closure plus a 64 KB void
   mask, so **320 KiB per queued tile**. At 32 workers this never
   appeared: the `encoding` field of the progress line stayed 0 for the
   whole run, with one blip to 35. At **24** workers in dense terrain the
   balance inverted and the real run went 0 (to about cell 200), then
   2,552 -> 8,428 -> 9,772 -> 16,640 -> **24,868 by cell 650 and still
   climbing** -- 7.6 GiB pinned at that depth, unbounded, and visible as
   violent anonymous-memory oscillation (a 79 GiB self-release in one
   step) as the pool drained bursts. The fetch window stayed bounded
   throughout, so this is the same defect class as defect 5 one stage
   downstream.

   **The condition that exposes it**: fetch outpacing encode. Fewer fetch
   workers means each warp lands sooner relative to the encode pool's
   throughput, and dense terrain means a high-entropy PNG takes far
   longer than the 5.3 ms a smooth smoke tile measured. Both push the
   same way, which is why a 32-worker run over ocean-heavy cells looked
   clean and a 24-worker run over mountains did not.

   **Fixed**: `--encode-backlog-tiles`, default `max(2 x
   --encode-workers, 32)`, blocks submission once the backlog reaches the
   cap. The cap is therefore also the memory this stage can hold: 320 KiB
   times the cap. `0` restores the old unbounded behavior for an A/B.
   The throttle sits inside `submit_encode`, the single choke point every
   `finalize()` passes through, because a first attempt placed it after
   the shared-tile branch in `on_result` and so missed the 96.7% fast
   path entirely -- the backlog still reached 649 against a cap of 64.

   **Interim tilt if an unpatched build has to run**: set
   `--encode-workers` above `--workers` (the default makes them equal) so
   the encode pool outpaces the fetch pool and the queue cannot build.

### `--overview-mode`: average (new default) vs. rewarp (the original design)

The original design re-warped every zoom independently from source --
correct, but it is why the egress estimate below is so large: the deepest
zooms sit at or below the 15 m native level (z13 is ~19.1 m/px at the
equator, z14 ~9.55 m/px), so *every* zoom down to z9 still reads a large
fraction of full-resolution data per cell (see the table). `--overview-mode average` (the default now)
warps only `--maxzoom` from source, then derives every shallower zoom by
NaN-aware 2x2 box-averaging the zoom one level deeper, already sitting in
the working MBTiles -- no further source reads. `--overview-mode rewarp`
keeps the original behavior as an A/B option.

Box-averaging needs each tile's real pre-fill void mask (not just its
already-0-filled elevation) to average correctly across a coastline or a
masked-water edge without a void pixel incorrectly dragging a real one
toward 0; this is stored per-tile as a packed-bit mask in a new
`void_masks` table, written for *every* tile regardless of mode. A
uniform mask (96.4% of them, measured) is a single byte; a mixed one is
8192 packed bytes. See "The working database is the other 334 GB" below
for why that distinction turned out to matter a great deal.

**Per-zoom vertical quantization** (Mapterhorn's technique,
`step(z) = min(2^(19-z) / 256, 1.0)` m: 1 m at z0-z11, 0.5 m at z12, 0.25 m
at z13, **0.125 m at z14**) runs in both modes before encoding,
`--no-quantize` to disable. The module docstring's own table stops at z13,
from when that was the global target; the formula it quotes is what runs, and
it yields the z14 step above. **Measured** (historical, at the z13 target) on
a single cell (w2s41) at z10-z13: 26.27 MB quantized vs. 54.45 MB
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
item JSON, e.g. 199,399,399 bytes for `wm91sm1`), the max-zoom pass alone is
**~1.9 TB** of the previous ~2.5-3 TB rewarp-mode total -- average mode's
total egress *is* that ~1.9 TB, since nothing else reads source. (This is a
~35-45% egress reduction over the old all-zooms design, concentrated
entirely in not re-reading the shallower zooms; it is not the "~4x" figure
floated before any of this was measured -- correcting that here rather than
repeating it.)

**Moving the target from z13 to z14 does not change that ~1.9 TB.** GDAL
picks the finest overview whose resolution is still <= the zoom's target,
and at both z13 and z14 that is already the 15 m native level, so the same
source bytes are read either way. What z14 does change is the output: the
max-zoom tile count goes up 4x, from ~10.2M tiles at z13 to **~41M at
z14**, and the finished archive should be budgeted at roughly **2.5-4x the
z13 estimate** (the max-zoom level dominates the total, and the per-zoom
quantization step halves to 0.125 m there, so the tiles compress a little
less well than a straight 4x would suggest). Scratch space for the working
MBTiles has to grow with it.

At the cluster's measured ~325 MB/s over 4 streams (see "Compute" above),
that is very roughly **1.6-2 hours** for the max-zoom fetch+warp stage
alone, unchanged by the z14 move since the egress is unchanged. Deriving
the shallower zooms is local-disk-bound, not network-bound (see the
derivation timings in the smoke test below: hundreds of tiles/second on a
laptop), but there are now 4x as many max-zoom tiles to derive from, so
budget tens of minutes rather than minutes on a 64-core node. **Overall
estimate: ~2.5-4 hours for the full z0-z14 global build on one
`cpu_amd` node** -- still an estimate, now a better-grounded one than the
pre-patch 1.5-6 hours, and still pending the operator's own timing pass on
the real cluster with the patched code.

### The working database is the other 334 GB

**Measured on the real cluster, and it overturns the memory model this
section used to carry.** At the moment the second global z14 run was
killed against a 200 GiB cap, the cgroup held:

| | |
|---|---|
| `memory.current` | ~200.0 GiB (the cap) |
| `anon` | 16.3 GiB |
| **`file`** | **183.0 GiB** |
| `shmem` | 0 |
| python `VmRSS` | **7.65 GiB** |

So the process's own memory was never the problem. Under cgroup v2,
**file-backed page cache counts toward `memory.max`**, and the climbing
memory was the page cache of the working MBTiles. `/tmp` is genuinely
real NVMe here (xfs on `nvme0n1`; tmpfs was ruled out), which is why this
presents as reclaim thrash (pgscan in the tens of billions at ~14%
efficiency) before the kill rather than as a clean allocation failure.

What makes the file that large is the `void_masks` table. Before the
sentinel format, every tile's mask was a flat 8,192 bytes, which at
global z14 (40.753M tiles) is **334 GB against roughly 40 GB of finished
archive**, and grows the database **24.2 MiB per processed cell**,
independent of terrain. Measured per-cell database growth at global tile
sizes is 26.5-29.4 MiB, which is exactly the 19-27 MiB per cell the
cluster's trough-RSS fit found. The sentinel format takes the mask table
to roughly 12-15 GB.

Two further consequences the operator should plan for:

- **A resume charges the whole inherited file at once.** The job script's
  `cp` plus `sha256sum` of a 176.5 GB working database populated ~176 GB
  of charged page cache within seconds of starting, before a single tile
  was built. That, not the build, is what made the patched run die
  earlier than the pre-patch one. Use direct I/O for the copy and
  checksum steps, or trim the cache afterwards.
- **`--mem` has to cover the working database, not just the workers.**
  Sizing it from `--workers` alone is what the pre-patch version of this
  paragraph advised and it is wrong: at 32 workers the process wanted
  under 17 GiB.

**Memory that is genuinely the process's:** the fetch window (2x
`--workers` in-flight cell results, 139-199 MB each at z13), the open
shared-boundary tiles (measured peak 5,547-6,462 tiles on the real run,
against a 9,401-tile simulated ceiling), and the encode backlog (measured
at 0 for nearly the whole run, one blip to 35, so it is not a concern in
practice and `--encode-backlog-tiles` stays off by default). The per-zoom
setup structures that used to charge 9.0 GB for a global z14 scope now
charge 3.8 MB (defect 5). Derivation reads small (256x256) tiles from
local SQLite and is not a memory concern.

**Resumability**, now accurate post-patch: every `--commit-every-tiles`/
`--commit-every-seconds` commit is durable (not just once per zoom), a
failed cell's tiles are never falsely finalized (see defect 1 above), and
`existing_tiles_for_zoom` means a rerun only ever redoes what is actually
missing -- at the tile level for the rewarp stage, and at the derived-tile
level for every shallower zoom in average mode. `pmtiles convert --force`
at the end always regenerates the final `.pmtiles` from the MBTiles'
current state.

An MBTiles written by the pre-defect-5 code resumes under the current code
unchanged, which matters because the global run's banked progress is
exactly such a file. **Verified**, not argued: an unpatched build was
`kill -9`'d at 930 of 1,457 z13 tiles, then resumed with the patched
script, which reported the 930 as already built, completed the remaining
527, derived z12-z10, and finished `COMPLETE` with `minzoom`/`maxzoom`
from built content. The finished archive is byte-identical to one built
from scratch in one pass.

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

### Void-filling from Copernicus GLO-30 (terrain tiles only, on by default)

User decision, 2026-11-14 (see DESIGN.md's "Known issues" entry for the
full writeup and the rejected alternatives): a GEM-15 masked void (water +
buffer, snow/ice, a GLO-30-outlier exclusion) rendered as a 0 m pit under
3D terrain. **Terrain tiles only** -- never the item COGs/STAC, never the
overview COG, which keeps its alpha-transparent voids -- now fill those
pixels from Copernicus GLO-30 instead, on by `--maxzoom` only (every
shallower zoom inherits already-filled pixels through `--overview-mode
average`'s box-averaging, with no extra source reads). `--no-void-fill`
restores the original 0-fill-everything behavior.

**Extra egress: small, not "nil."** GLO-30 tiles are ~22 MB each (raw;
actual bytes transferred for a bilinear-warped read at 15-19 m/px
resolution will be less, similar to how GEM-15's own overview-level reads
cost less than their file's full size). Fetched once per cell that has
*any* interior NaN at `--maxzoom` -- more cells than just the ~2.5% GEM-15
masks outright, since any coastline cell also qualifies, though a cell
that is pure open ocean has no GLO-30 tile to fetch at all ("ocean areas
do not have tiles" per AWS's own dataset page) and the fetch is a cheap
404, not a wasted download. Not measured at global scale (out of scope for
a laptop smoke test); the real run should watch this if it turns out to
matter, but even a generous estimate (a few thousand coastline-adjacent
cells x ~22 MB) is two orders of magnitude under the max-zoom pass's ~1.9 TB.

**Measured, not assumed:**
- EGM2008->EGM96 offset at the Sau reservoir (2.40E, 41.97N): **+0.124 m**.
- GLO-30 URL for the Sau cell (`w2s41`, SW corner 2E/41N):
  `Copernicus_DSM_COG_10_N41_00_E002_00_DEM/...tif`, confirmed with a real
  fetch (200, bounds exactly [2,41]x[3,42] -- the same 1x1-degree footprint
  as the GEM-15 cell it fills).
- Built `--cells w2s41 --minzoom 10 --maxzoom 13` (fill on, the default)
  and decoded the resulting PMTiles' z13 tile over Sau directly: 0 zero-
  valued pixels left (was 917 before filling), values 356-741 m (mean
  ~415 m) -- squarely inside the ~360-425 m the operator expected from a
  direct GLO-30 read there. A cross-section through the void boundary
  shows a smooth feather (435.8 m at the last real GEM-15 pixel -> 429.5
  -> 417.5 -> 406.0 -> a ~403.5 m GLO-30 plateau in the middle -> climbing
  back up to 434.8 m at the other real edge), not a hard lip.
- The archive's own `attribution`/`description` metadata round-trips
  through `pmtiles convert` and `pmtiles show` correctly, including the
  licence-required modified-data notice (see DESIGN.md for the exact
  wording and where it came from).
- The same single-cell build's z10 tile still fails `--verify` by 683 m --
  this is the *pre-existing*, already-documented single-cell sharding
  artifact (a missing-neighbor z13 tile cascading void up through
  derivation; see the "average vs. rewarp agreement" measurement above),
  reproduced at the exact same (z,x,y) as before void-filling existed.
  Confirmed unrelated to filling, not a new defect.

**`--verify`'s policy for filled pixels**, since GEM-15 (hence this
check's own reference, always a pure GEM-15 rewarp) has no data to compare
against where a pixel was filled: those pixels are excluded from the
normal decoded-vs-reference diff (already true, incidentally, since the
candidate selection is driven by where the reference has data) and
instead get a separate, looser sanity pass -- finite, and within
[-500, 9000] m -- over a sample of them, logged as "void-fill sanity
OK/FAIL" alongside the normal result. This does not independently verify
the *value* GLO-30 produced (that would need re-deriving the whole fill
pipeline a second time -- a deliberate scope cut, not an oversight); it
still catches a gross encoding bug, just not a subtly-wrong fill value.

### DTM terrain (optional, not in the published asset contract yet)

```bash
python3 tools/build_terrain.py --band dtm --minzoom 0 --maxzoom 14 \
  --output staging/publish/gem-15/terrain/gem-15-dtm-terrarium.pmtiles
```

Same pipeline, same cost profile (DTM mosaics are single-band but the same
size class). Not currently referenced by `collection.json`'s asset
contract -- add it there first if you publish it.

## 4. Barcelona demo PMTiles (z0-z14)

```bash
python3 tools/build_terrain.py --band dsm --minzoom 0 --maxzoom 14 \
  --bbox=1.0,40.8,3.3,42.0 \
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

**That measured run predates this revision of `build_terrain.py`**, and a
rebuild is now required, not optional: the live file is the one with the
user-reported Sau reservoir "hole" (see DESIGN.md's "Known issues" entry),
and void-filling (this script's current default) is the fix. Rebuild with
the exact command above -- `--no-void-fill` is not needed, filling is on
by default -- and republish over the existing object at the same key (the
RUNBOOK's step 7/8 upload-then-publish order already handles this; no
other file or URL changes). While rebuilding, this version also applies
quantization (measured -51.75% on a single cell in step 3; expect a
similarly large reduction here) and defaults to `--overview-mode average`,
reading source only at `--maxzoom` (14 for this file, not 13) instead of
at every zoom from 0 to 14 -- both are incidental improvements that come
along for free, not the reason for the rebuild.

After rebuilding, confirm the fix the same way the QC investigation did:
decode the new file's tile over Sau (z13: `mercantile.tile(2.40, 41.965,
13)` -> x=4150, y=3042) and check it has no 0 m pixels where the old one
did.

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

# Always the --flag=value form for --bbox, not --flag value: a bbox this
# far south/west starts with a literal "-", and argparse's space form
# reads the next token as if it might be another option rather than
# --bbox's value ("error: argument --bbox: expected one argument"),
# confirmed with this exact BBOX -- verified, not assumed. The problem is
# argparse's own tokenizing, so quoting "$BBOX" does not help; only the
# --bbox=$BBOX form does.
python3 tools/build_overview.py --bbox="$BBOX" --workers 16
python3 tools/build_terrain.py --band dsm --minzoom 0 --maxzoom 14 --bbox="$BBOX" \
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
