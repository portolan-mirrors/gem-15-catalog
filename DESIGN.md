# gem-15-catalog — design contract

This file is the coordination contract for building this catalog. It records
the decisions; the published documentation under `catalog/` is written against
it. Keep it current when a decision changes.

## What this catalog is

A **metadata-first Portolan mirror** of GEM-15, Google Research's experimental
near-global elevation dataset (15 m DSM + DTM from sub-meter optical stereo).
The 26,970 COGs (13,485 one-degree cells × dsm/dtm, 12.3 TB) stay in Google's
public bucket (`gs://climate-and-sustainability/gem-15`) under a permanent
no-deletion retention policy. We copy **no** COGs. This catalog adds:

1. Portolan-conformant collection metadata and exemplary documentation mined
   from the GEM-15 paper (see `docs/research/gem15-paper-notes.md`).
2. `items.parquet` — a STAC-GeoParquet item mirror of all 13,485 items, built
   from Google's own STAC, upgraded to STAC 1.1.
3. A global **overview COG** (PORTO-FMT-067/068: 8-bit RGB+alpha, EPSG:3857,
   roles `visual`+`overview`), rendered with Google's own cyclic-palette
   hillshade visualization from the Earth Engine example script.
4. **Terrarium-encoded raster PMTiles** of the DSM, so MapLibre can use GEM-15
   as an elevation layer (`raster-dem`) — hillshade, color-relief, 3D terrain.
5. MapLibre **styles** and a thumbnail.

A second collection, `mapterhorn-gem15`, is published here too. It contains
three small terrain archives from an **experiment**. See "The
mapterhorn-gem15 experiment" below for what it is and what it must never
claim to be.

Item links go **down into Google's catalog**: our `collection.json` carries
297 absolute `child` links to Google's 10°×10° group catalogs at
`https://storage.googleapis.com/climate-and-sustainability/gem-15/stac/gem-15/`,
which link their items. We publish no per-item JSON. (Generated links list:
`docs/research/child_links.json`.)

## Why the derivatives exist

- **Terrain PMTiles**: MapLibre's `raster-dem` source consumes only
  terrarium/terrain-RGB-encoded tiles, never Float32 COGs. Also Google's
  bucket sends **no CORS headers** (verified 2026-11-13: range requests work,
  `Access-Control-Allow-Origin` absent), so browsers cannot read the COGs
  directly at all. Everything browser-facing must be bytes we host on Source
  Cooperative. PMTiles over flat tiles: one file, range requests, no server.
- **Overview COG**: PORTO-FMT-067 recommends one collection-level overview so
  clients show the whole mosaic without touching 13,485 items.
- **items.parquet**: PORTO-FMT-040; one range request instead of 13,485 JSON
  fetches; required registration as asset role `collection-mirror`.

## Repository vs staging vs upstream

| Where | What |
|---|---|
| git (`catalog/`) | catalog.json, collection.json, README/AGENTS/llms.txt, styles/*.json, thumbnail.png |
| `staging/publish/` (gitignored, uploaded by `tools/upload_data.py`) | items.parquet, overview COG, terrain PMTiles |
| the bucket, written by hand | the three `mapterhorn-gem15` PMTiles archives, uploaded once by the cluster and never rebuilt (see below) |
| Google's bucket (referenced only) | all COGs, all item JSON, group catalogs |

Heavy builds run on the big server ("rails"), not laptops: see `RUNBOOK.md`.

## Published layout and asset contract

Public base: `https://data.source.coop/portolan-mirrors/gem-15-catalog`

```
catalog.json
README.md  AGENTS.md
gem-15/
├── collection.json         # id: gem-15
├── README.md  AGENTS.md  llms.txt
├── items.parquet           (staged)
├── gem-15_overview.tif     (staged)
├── thumbnail.png           (staged)
├── terrain/
│   ├── gem-15-dsm-terrarium.pmtiles          (staged; global)
│   └── gem-15-dsm-terrarium-demo-barcelona.pmtiles  (staged; z0–14 demo)
├── styles/
│   ├── hypsometric.json    # default: color-relief tint + hillshade
│   ├── hillshade.json      # hillshade only, neutral
│   └── terrain-3d.json     # tint + hillshade + `terrain` + sky
mapterhorn-gem15/
├── collection.json         # id: mapterhorn-gem15
├── README.md  AGENTS.md  llms.txt
├── thumbnail.png           # in git, like gem-15's
├── mapterhorn-gem15-barcelona.pmtiles  (uploaded by hand; see below)
├── mapterhorn-gem15-cusco.pmtiles      (uploaded by hand)
├── mapterhorn-gem15-atlas.pmtiles      (uploaded by hand)
└── styles/
    ├── atlas.json          # default: color-relief tint + hillshade
    ├── barcelona.json      # same, maxzoom 12
    └── cusco.json          # same, maxzoom 13
```

`collection.json` assets (keys → roles):

| key | href | roles | type |
|---|---|---|---|
| `geoparquet-items` | `./items.parquet` | `collection-mirror` | `application/vnd.apache.parquet` |
| `overview` | `./gem-15_overview.tif` | `visual`, `overview` | COG media type |
| `terrain-dsm` | `./terrain/gem-15-dsm-terrarium.pmtiles` | `visual` | `application/vnd.pmtiles` |
| `terrain-dsm-demo` | `./terrain/gem-15-dsm-terrarium-demo-barcelona.pmtiles` | `visual` | `application/vnd.pmtiles` |
| `thumbnail` | `./thumbnail.png` | `thumbnail` | `image/png` |
| `style-hypsometric` | `./styles/hypsometric.json` | `style`, `default` | `application/vnd.mapbox.style+json` |
| `style-hillshade` | `./styles/hillshade.json` | `style` | `application/vnd.mapbox.style+json` |
| `style-terrain-3d` | `./styles/terrain-3d.json` | `style` | `application/vnd.mapbox.style+json` |
| `documentation` | `./llms.txt` | `documentation` | `text/plain` |
| `README` | `./README.md` | `metadata` | `text/markdown` |

`mapterhorn-gem15/collection.json` assets (keys → roles):

| key | href | roles | type |
|---|---|---|---|
| `terrain-barcelona` | `./mapterhorn-gem15-barcelona.pmtiles` | `data`, `visual` | `application/vnd.pmtiles` |
| `terrain-cusco` | `./mapterhorn-gem15-cusco.pmtiles` | `data`, `visual` | `application/vnd.pmtiles` |
| `terrain-atlas` | `./mapterhorn-gem15-atlas.pmtiles` | `data`, `visual` | `application/vnd.pmtiles` |
| `style-atlas` | `./styles/atlas.json` | `style`, `default` | `application/vnd.mapbox.style+json` |
| `style-barcelona` | `./styles/barcelona.json` | `style` | `application/vnd.mapbox.style+json` |
| `style-cusco` | `./styles/cusco.json` | `style` | `application/vnd.mapbox.style+json` |
| `documentation` | `./llms.txt` | `documentation` | `text/plain` |
| `README` | `./README.md` | `metadata` | `text/markdown` |
| `thumbnail` | `./thumbnail.png` | `thumbnail` | `image/png` |

The PMTiles take `data` as well as `visual`, unlike gem-15's terrain tiles.
There they are a derivative of item COGs the catalog also describes; here they
are the only data the collection has, and `visual` alone trips rashid's
PTL-COL-006 ("collection refers to no data").

Item-level data stays described via `item_assets` (`dsm`: 5 bands, `dtm`:
1 band, Float32, nodata `"nan"`, per-UTM-zone) and the `renders` extension.
The collection is a **mirror**: `providers` producer/licensor = Google
Research, host = Portolan Mirrors; `via` → the Earth Engine catalog page;
`canonical` → Google's STAC collection; `cite-as` → doi:10.31223/X55N75.
License `CC-BY-4.0`, attribution "This dataset is produced by Google".

## Terrain tiles (the MapLibre elevation answer)

- Encoding: **terrarium** (Mapzen): `elevation = (R*256 + G + B/256) − 32768`,
  PNG, 512- or 256-px tiles (256 standard), Web Mercator, `"encoding":
  "terrarium"` on the raster-dem source.
- Band: DSM now; keep `--band dtm` working in the pipeline so a DTM layer can
  ship later without redesign (output name `gem-15-dtm-terrarium.pmtiles`).
- NaN cells are, by default, filled from Copernicus GLO-30 (EGM2008→EGM96
  corrected, feathered toward GEM-15 at the boundary) before encoding — see
  "Known issues" below for why and the exact attribution required. What
  that leaves NaN (void-filling disabled, no GLO-30 tile for that cell —
  mainly open ocean, per AWS's own "ocean areas do not have tiles" note —
  or the rare pixel neither source covers) still encodes as 0 m (terrarium
  has no nodata; this matches AWS terrain-tiles). Wholly-empty tiles are
  omitted below the maxzoom only if MapLibre renders acceptably; otherwise
  emit filled tiles.
- Zoom plan: smoke test locally (one cell, shallow zooms) → on rails, global
  **z0–z14**, built from the COGs' internal overviews at the matching
  resolution per zoom, plus the Barcelona demo file at the same z0–z14 for the
  example app. The global file replaces shallower builds at the **same URL**,
  so styles and apps never change.

  z14, not z13: Web Mercator ground resolution is
  `156543.03 · cos(lat) / 2^z` m/px, so z13 is 19.1 m/px at the equator
  against GEM-15's native 15 m — undersampling by up to **1.27x** through the
  tropics, which is where a large share of the mapped cells sit. It only
  reaches 15 m around 38° latitude. z14 is 9.55 m/px at the equator, so every
  latitude gets full native detail and the deepest zoom is mildly
  oversampled instead of lossy. The cost is 4x the max-zoom tile count; source
  egress is unchanged, because the native-level read from the COGs is the same
  either way.
  Per-zoom vertical quantization follows the existing
  `step(z) = min(2^(19−z) / 256, 1.0)` m formula, which puts **z14 at 0.125 m**
  (z13 0.25 m, z12 0.5 m, 1 m at z0–z11) — far below GEM-15's own
  several-meter DRMSE, so the extra zoom costs accuracy nothing.

## Styles

MapLibre GL style spec v8, self-contained, `sources.terrain.url =
"pmtiles://../terrain/gem-15-dsm-terrarium.pmtiles"` resolved relative to
`styles/` (verify what the Portolan browser and MapLibre accept for pmtiles
raster-dem; absolute public URLs are the fallback). `color-relief` layer type
requires MapLibre GL JS ≥ v5.6 — verify current syntax against MapLibre docs
before writing. Two distinct Google palettes exist: the EE catalog entry's
`gee:visualizations` carries an 11-color ramp (`fadd67 … fbd227`, min 20 /
max 100), and the EE example script (`docs/research/gem15-example.js`) uses a
268-step **cyclic** ramp applied mod 100 m with hillshade — the overview COG
reproduces the latter. For color-relief styles use a conventional absolute
hypsometric ramp and credit the choice in the style `description`.

## Overview COG

Reproduce Google's own look: cyclic palette mod 100 m × hillshade (their EE
example script, saved at `docs/research/gem15-example.js`), from DSM internal
overviews, ~960 m/px, EPSG:3857, 8-bit RGB + alpha (alpha 0 where no data),
512 blocks, ZSTD, embedded stats (`gdal_translate -of COG -stats`), overviews.
Record exact commands in `processing:lineage` on the asset and in AGENTS.md.

## Known issues to document (collection `known_issues` / README)

- Upstream bucket: no CORS (browsers can't read COGs directly; ask Google to
  enable CORS — track in an issue).
- Upstream COGs lack embedded band statistics (Portolan requires them for
  publisher-hosted COGs; these are third-party-hosted, so conformance rests on
  our own assets — rashid may still note it in data passes).
- Upstream STAC is 1.0.0; items carry `raster:bands` (not STAC 1.1 `bands`).
- India intentionally offset; Russia/China/Mongolia/Greenland excluded; ~2.5 %
  of cells masked; DTM weaker under dense canopy (see paper notes).
- **Masked water bodies used to render as 0 m pits in 3D terrain, with
  visibly striped/comb walls — fixed in the terrain tiles by void-filling
  from Copernicus GLO-30.** Investigated 2026-11-14 after a user-reported
  "hole" northwest of Barcelona in the live terrain demo: it is the Sau
  reservoir (Embassament de Sau, ~41.97N 2.40E), masked by GEM-15 itself
  (water + a 9 m buffer, per `docs/research/gem15-paper-notes.md`'s Masking
  section) — a 37,379-pixel (~8.4 km²) NaN cluster at native 15 m
  resolution. Verified at the time by decoding the (pre-fix) published demo
  PMTiles at z10/z12/z13 and comparing every pixel against an independent
  source rewarp at the same tiles: 0/0 mismatches at all three zooms (every
  0-encoded terrain pixel was exactly a source-NaN pixel, and vice versa) —
  faithful rendering of upstream nodata through the NaN→0 terrarium
  convention, not a pipeline bug. The striped wall look was MapLibre's mesh
  triangulation at a sharp, single-texel elevation discontinuity (confirmed
  present in the *source* COG itself, e.g. 435.8 m → NaN → 429.1 m across
  one pixel at z13, not introduced by any resampling step). This affected
  every masked inland water body and snowfield globally under 3D terrain,
  not just this one reservoir.

  **Decision (user, 2026-11-14): option (c) — fill from Copernicus GLO-30,
  in the terrain PMTiles only.** The item COGs/STAC/`items.parquet` stay
  pure GEM-15 (we mirror Google's data, not a blend); the overview COG
  keeps its alpha-transparent voids (it is a coverage mask as much as a
  visualization: see its own asset description). Only the terrain tiles —
  the one asset whose whole job is a renderable, hole-free elevation
  surface — fill voids, on by default (`tools/build_terrain.py
  --no-void-fill` to get the old 0-fill-everything behavior).
  Implementation (see `tools/build_terrain.py`'s module docstring for the
  full detail): GLO-30 (AWS Open Data, unauthenticated HTTPS, verified by a
  real fetch for the Sau cell) fills NaN at `--maxzoom`, converted from
  EGM2008 to EGM96 (measured offset at Sau: **+0.124 m**; pyproj +
  PROJ_NETWORK, verified working, with a startup check that fails loudly
  rather than ever filling with an unconverted height) and feathered
  toward GEM-15's own nearest value over 3 px at the void boundary. Filled
  pixels propagate through quantization and `--overview-mode average`'s
  box-averaging as ordinary elevation, with no special-casing needed (they
  simply are not void from that point on; see `tools/build_terrain.py` on
  `void_masks`'s resulting, slightly broader meaning: "neither GEM-15 nor
  GLO-30 had data here," not "GEM-15 masked this").

  Rejected: (a) keep plain 0-fill (superseded by this decision); (b)
  inpaint from GEM-15's own DSM (invents elevation over real water with no
  independent source, and a reservoir's bed is nothing like its rim); (d)
  a reserved sentinel + style-level flattening (a generic `raster-dem`
  consumer not using our styles would not know to special-case it; GLO-30
  filling needs no consumer-side cooperation at all).

  **Attribution.** The Copernicus WorldDEM-30 licence (verified by reading
  the licence document directly, not a paraphrase), Article 6(b), the
  notice required for *modified* data (this is resampled, geoid-shifted,
  and feather-blended, not redistributed as-is):

      produced using Copernicus WorldDEM-30 © DLR e.V. 2010-2014 and
      © Airbus Defence and Space GmbH 2014-2018 provided under
      COPERNICUS by the European Union and ESA; all rights reserved

  This text is carried in the terrain PMTiles' own MBTiles/PMTiles
  `attribution` metadata (set by `tools/build_terrain.py` whenever
  void-filling is on) and must also appear in `collection.json`'s
  `terrain-dsm`/`terrain-dsm-demo` asset `description`s and in
  `catalog/gem-15/AGENTS.md`/`README.md`, which must not describe the terrain
  tiles as pure-GEM-15 or as "no nodata, ocean/masked cells are 0 m" without
  qualification. **Done** (2026-11-14): `collection.json`'s two terrain asset
  descriptions and `processing:lineage`, `AGENTS.md`, `README.md`,
  `catalog/gem-15/styles/*.json` (both the prose metadata and the source
  `attribution`, which overrides the archive's own notice because MapLibre
  lets a style's source definition win over the TileJSON), and
  `examples/maplibre-terrain/{index,barcelona}.html` plus that example's
  README all now state the fill contract and carry the notice. The live demo
  archive still holds 0 m pits until it is rebuilt; the pages say so and need
  no further edit when it lands.

## The mapterhorn-gem15 experiment

A second collection, `mapterhorn-gem15`, published 2026-11-15. It answers
[mapterhorn#368](https://github.com/mapterhorn/mapterhorn/issues/368), where a
maintainer suggested running the Mapterhorn terrain pipeline over GEM-15
tiffs. We did that on a fork — `cholmes/mapterhorn`, branch `gem-15-source`,
archives built at commit `86d8494`, forked from upstream `main` at `2c4195d`
— and published three
small test archives. Catalonia covers 4 cells to maxzoom 12. Cusco and the
High Atlas cover 1 cell each to maxzoom 13.

**The framing is binding, not stylistic.** Nobody opened a pull request
upstream and nothing was merged. The Mapterhorn project has not endorsed the
result. Every surface that describes these archives says "built with the
Mapterhorn pipeline (BSD-3)" and states the unmerged-fork status. Wording that
reads as a Mapterhorn release is a defect, in prose and in metadata alike. The
archives' own PMTiles metadata declares `attribution: "© Mapterhorn"` and
nothing else, because the pipeline writes that into everything it builds, so
every style and snippet here sets the full attribution on the source instead.

**The source register for this collection**, matching the rule in AGENTS.md:

1. `NOTES.md` on the fork branch — pipeline mechanics, the void-fill and
   geoid decisions, the maxzoom analysis, and the exact licence texts. It is
   the counterpart of `docs/research/gem15-paper-notes.md`.
2. Measurements against the published archives. The PMTiles headers, the
   archive metadata, the tile sizes and the probe elevations in
   `catalog/mapterhorn-gem15/` were each re-read from the published bytes over
   range requests on 2026-11-15, not copied from a build log.
3. `catalog/gem-15/` for anything about GEM-15 itself. Do not re-derive
   coverage, accuracy, band order or the masking rules here.

**Naming.** Collection id `mapterhorn-gem15`: the tool first, the data second,
which is what the collection is a statement about. Archive files are
`mapterhorn-gem15-{area}.pmtiles` in a directory of the same name, so the
published path repeats the stem; that was fixed when the cluster uploaded
them and the metadata follows the bytes. Asset keys are `terrain-{area}` and
`style-{area}`, and the style file names match the archive stems so the
mapping needs no lookup.

**Anything published from these archives takes on three obligations.**

- The GEM-15 CC-BY-4.0 attribution "This dataset is produced by Google".
- The Copernicus WorldDEM-30 Article 6(b) modified-data notice quoted in the
  Known issues section above, required because the void fill puts
  GLO-30-derived pixels in each archive.
- BSD-3 credit for the pipeline, with the not-endorsed framing.

**The bytes did not go through `tools/upload_data.py`.** The cluster that ran
the build uploaded the three archives straight to
`s3://portolan-mirrors/gem-15-catalog/mapterhorn-gem15/` through the
`data.source.coop` gateway. The repository has no tool that stages or
rebuilds them. That is a deliberate exception for a frozen
experiment, and it has a cost worth stating: `tools/update_asset_facts.py`
cannot refresh `file:size` or `file:checksum` for these assets, so those
values were set by hand from the cluster's digests and verified against the
`content-length` the gateway reports. A rebuild means re-measuring them by
hand. The publish boundary itself is untouched: `catalog/` holds metadata,
documentation, styles and one small PNG, as everywhere else.

**Frozen.** These archives are a point measurement. They do not track
upstream Mapterhorn and they will not be rebuilt for a new GEM-15 version.
The collection says so in `description`, README and AGENTS.

**A known defect of the build**: `bundle_area.py` on the fork writes global
bounds into every PMTiles header, so all three archives claim the whole world.
The real tile footprints were measured from each archive's root directory and
are what `extent.spatial.bbox` carries. Documented as a known issue on both
the collection and the fork's notes.

**The thumbnail** is rendered by `tools/build_mapterhorn_thumbnail.py` from
the published High Atlas archive over range requests, rather than from the
GEM-15 COGs, so the catalog card shows the bytes this collection serves. It
uses the same hypsometric ramp as the styles here and as gem-15's
`hypsometric.json`, which keeps one meaning for a colour across the catalog.
Changing the ramp in one place without the other makes the card lie about the
default style.

## Example app

A MapLibre terrain example lives in the **portolan-spec** repo (PR), under its
examples tree: loads the terrain PMTiles via the PMTiles protocol, hypsometric
+ hillshade + 3D terrain with exaggeration control, attribution "This dataset
is produced by Google", links back to this catalog.
