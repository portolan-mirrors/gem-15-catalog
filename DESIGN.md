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
| git (`catalog/`) | catalog.json, collection.json, README/AGENTS/llms.txt, styles/*.json |
| `staging/publish/` (gitignored, uploaded by `tools/upload_data.py`) | items.parquet, overview COG, terrain PMTiles, thumbnail.png |
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
└── styles/
    ├── hypsometric.json    # default: color-relief tint + hillshade
    ├── hillshade.json      # hillshade only, neutral
    └── terrain-3d.json     # tint + hillshade + `terrain` + sky
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
- NaN cells and ocean encode as 0 m (terrarium has no nodata; this matches AWS
  terrain-tiles). Wholly-empty tiles are omitted below the maxzoom only if
  MapLibre renders acceptably; otherwise emit filled tiles.
- Zoom plan: smoke test locally (one cell, shallow zooms) → on rails, global
  **z0–z13** (native 15 m ≈ z13) built from the COGs' internal overviews at
  the matching resolution per zoom, plus the Barcelona demo file z0–14 for the
  example app. The global file replaces shallower builds at the **same URL**,
  so styles and apps never change.

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

## Example app

A MapLibre terrain example lives in the **portolan-spec** repo (PR), under its
examples tree: loads the terrain PMTiles via the PMTiles protocol, hypsometric
+ hillshade + 3D terrain with exaggeration control, attribution "This dataset
is produced by Google", links back to this catalog.
