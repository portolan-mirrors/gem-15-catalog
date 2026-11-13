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
  --workers 16 \
  --output staging/publish/gem-15/terrain/gem-15-dsm-terrarium.pmtiles
```

This is the expensive one. Read `tools/build_terrain.py`'s module docstring
for the algorithm (one warp per cell per zoom, snapped to the tile grid, not
one warp per tile; exact-completion compositing across cells sharing a
tile). The honest cost driver, worked out from GDAL's own overview-selection
rule (pick the finest overview whose resolution is still <= the zoom's
target resolution) against the DSM COGs' actual overview ladder
(15/30/60/120/240/480 m):

| zoom | target res (m/px) | overview GDAL reads | est. bytes/cell | est. total (x13,485 cells) |
|---|---|---|---|---|
| 13 | ~19.1 | **native (15 m)** | ~142 MB | **~1.9 TB** |
| 12 | ~38.2 | 30 m | ~36 MB | ~480 GB |
| 11 | ~76.4 | 60 m | ~9 MB | ~120 GB |
| 10 | ~152.9 | 120 m | ~2.2 MB | ~30 GB |
| 9 | ~305.7 | 240 m | ~0.56 MB | ~7.5 GB |
| 0-8 | >=~611 | 480 m (same level, read once per zoom) | ~0.14 MB | ~17 GB (9 zooms combined) |

**Total estimated egress: roughly 2.5-3 TB**, derived from the per-cell
`dsm/zall/mosaic.tif` `file:size` values already visible in the upstream
item JSON (e.g. 199,399,399 bytes for `wm91sm1`), apportioned across overview
levels by pixel count (native/30/60/120/240/480 m have 1, 1/4, 1/16, 1/64,
1/256, 1/1024 as many pixels respectively) since the DSM COG is
pixel-interleaved across its 5 bands, so reading "band 1 only" at a given
level still costs that level's full tile bytes. **z13 alone is most of the
cost** because 19.1 m/px falls between the 15 m native level and the 30 m
overview, so GDAL has no choice but to read full-resolution data for every
cell.

At a sustained 125-625 MB/s from GCS (typical range for a well-provisioned
server; validate against your actual link), that is very roughly
**1.5-6 hours of wall-clock time**, dominated by network I/O rather than
CPU. This has not been measured end to end -- only the mechanism (one cell
at a time, per zoom) and the per-level byte accounting above are verified.
Run `z13` on its own first and time it before committing to the full range.

**Known, deliberately accepted inefficiency:** zooms 0-8 all resolve to the
*same* 480 m overview per cell, but each zoom re-reads it independently
(the script does not share reads across zooms within a cell). At ~17 GB
total this is small next to z13's ~1.9 TB and was not worth the extra
complexity of a shared per-cell cache across zooms -- revisit if z0-z8 ever
becomes the bottleneck.

**Memory:** `build_terrain.py`'s compositing holds an upfront "tiles
remaining" count for every tile at the current zoom (see its docstring).
At z13 globally this is tens of millions of entries -- budget for it (a
machine with tens of GB of RAM handles this comfortably; a laptop does
not). Zooms are processed and freed one at a time, so this is the per-zoom
peak, not a running total across zooms.

**Resumability:** tile-level, via the intermediate MBTiles
(`staging/publish/gem-15/terrain/gem-15-dsm-terrarium.mbtiles`, kept after
the build). A tile is written to it only once every contributing cell has
reported in, so a crash never leaves a half-composited tile marked done;
rerunning the same command skips every zoom/tile already present and
redoes only the incomplete frontier. `pmtiles convert --force` at the end
always regenerates the final `.pmtiles` from the current MBTiles state, so
it is safe to re-run that whole command repeatedly as the MBTiles fills in.

**Smoke-tested:** `--cells wm91sm1 --minzoom 8 --maxzoom 11 --verify`
(`staging/smoke/terrain-sample.pmtiles`): 50 tiles built, all four
per-zoom `--verify` checks passed (max sampled decode-vs-direct-read
error 0.0038 m at z8, 0.4886 m at z9, 0.0038 m at z10, trivially 0 m at
z11 where the sampled tile was wholly void), `pmtiles show` round-tripped
the metadata including the non-standard `encoding: terrarium` key, and a
rerun of the identical command correctly reported "nothing to do" at every
zoom. Terrarium's own quantization step is 1/256 m (~0.0039 m); every
passing check above is consistent with that floor, not with noise. (A
first version of this check, before `errorThreshold=0.0` was added to the
warp calls and before sampling was restricted to source-data-backed
pixels, found differences up to ~2 m right at nodata/coastline boundaries
between two independently-shaped GDAL warp calls -- a real GDAL
bilinear-with-partial-nodata sensitivity, not a terrarium encoding bug; see
the comment in `verify_mbtiles_tile`.)

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
considered. Minutes, not hours -- this was effectively smoke-tested already
(the terrain smoke test above exercises the identical code path at a
similar scale, and `build_thumbnail.py`'s own run reads the same four
cells at native-ish resolution in well under a minute).

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
  --output staging/publish/gem-15/terrain/gem-15-dsm-terrarium.pmtiles
```

`build_items_parquet.py` has no `--bbox` (it always builds the full item
index -- a per-country subset of 13,485 small JSON rows is not worth adding
scope-selection logic for); use `--limit` for a quick sanity run instead.
There is no country filter for `build_thumbnail.py` either -- it always
renders the fixed Barcelona demo area.

A country-scale `--bbox` run shares the same resumable caches
(`staging/cache/`) as the global run: cells inside the bbox that a country
run already fetched/rendered are not re-fetched when a later, unrestricted
global run reaches them. Running a country first is a legitimate way to
warm the cache and sanity-check output before paying for the global
egress estimated in step 3.
