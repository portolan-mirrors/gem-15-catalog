#!/usr/bin/env python3
"""Write catalog/gem-15/collection.json.

Regeneration aid: collection.json is hand-maintained from here on, but this
script records how it was assembled, and the 297 child links come from
docs/research/child_links.json verbatim rather than being retyped. If you
rerun it, diff the output against the committed file before accepting it —
later hand edits (e.g. asset facts injected by update_asset_facts.py) must
be preserved.
"""
import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "catalog" / "gem-15" / "collection.json"

child_links = json.loads((REPO / "docs/research/child_links.json").read_text())
assert len(child_links) == 297, len(child_links)

UPSTREAM_EPSG = [
    32601, 32602, 32603, 32604, 32605, 32606, 32607, 32608, 32609, 32610,
    32611, 32612, 32613, 32614, 32615, 32616, 32617, 32618, 32619, 32620,
    32621, 32622, 32625, 32626, 32627, 32628, 32629, 32630, 32631, 32632,
    32633, 32634, 32635, 32636, 32637, 32638, 32639, 32640, 32641, 32642,
    32643, 32644, 32645, 32646, 32647, 32648, 32649, 32650, 32651, 32652,
    32653, 32654, 32655, 32656, 32657, 32658, 32659, 32660, 32701, 32702,
    32703, 32704, 32705, 32706, 32707, 32708, 32709, 32710, 32712, 32713,
    32715, 32716, 32717, 32718, 32719, 32720, 32721, 32722, 32723, 32724,
    32725, 32726, 32728, 32729, 32730, 32731, 32732, 32733, 32734, 32735,
    32736, 32737, 32738, 32739, 32740, 32741, 32742, 32743, 32747, 32748,
    32749, 32750, 32751, 32752, 32753, 32754, 32755, 32756, 32757, 32758,
    32759, 32760,
]

COG = "image/tiff; application=geotiff; profile=cloud-optimized"

# The sync time this metadata was built against Google's published STAC.
UPDATED = "2026-11-13T00:00:00Z"

# The published base URL, from catalog.publish.yaml. Used only where a
# relative href cannot work: the web-map-links pmtiles links name a URL a
# map client loads directly.
PUBLIC_BASE = "https://data.source.coop/portolan-mirrors/gem-15-catalog"

# The 11-stop cyclic ramp this catalog uses for local relief, as hex.
CYCLIC_HEX = [
    "fadd67", "b8b257", "7ca88f", "4aa4ca", "3869d1", "6f5bc8",
    "b862b0", "bc3a6b", "cb3438", "f08429", "fbd227",
]
stops = [0, 26, 51, 77, 102, 128, 153, 179, 204, 230, 255]
cyclic_colormap = {
    str(stop): [int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 255]
    for stop, h in zip(stops, CYCLIC_HEX)
}


def band(name, description):
    return {
        "name": name,
        "description": description,
        "data_type": "float32",
        "nodata": "nan",
        "unit": "m",
        "raster:spatial_resolution": 15.0,
    }


DESCRIPTION = """\
GEM-15 is Google Research's experimental near-global elevation dataset: a 15 m
Digital Surface Model (DSM) and a matching bare-earth Digital Terrain Model (DTM),
derived from sub-meter *optical stereo* satellite imagery rather than radar or
lidar. It covers 13,485 one-degree cells as 26,970 Cloud-Optimized GeoTIFFs,
about 12.3 TB. This Portolan collection is a **metadata mirror**: the pixels stay
in Google's public bucket and are referenced there, while the catalog adds a
queryable item index, a global overview, browser-ready terrain tiles, styles and
documentation. Agents should start at [AGENTS.md](AGENTS.md); people should start
at [README.md](README.md).

**How it was made.** Vendor stereo pairs from Airbus and Maxar, acquired through
2024 at 0.3–1.5 m native ground sample distance, are pose-refined against ALOS
AW3D30 and reconstructed into a native 0.5 m DSM; a deep-learning model trained on
USGS 3DEP airborne lidar predicts a 1.0 m DTM from RGB plus the DSM. Both are
aggregated to the 15 m grid (Sect. 3.1, [paper](https://doi.org/10.31223/X55N75)).
Three sensor tiers contribute: Pléiades Neo at 0.30 m, acquired 2022–2024, supplies
63.2 % of mapped cells; other sub-meter archive imagery (WorldView-2/3/4, GeoEye-1,
Pléiades 1A/1B) 24.4 %; and 1.5 m SPOT-6/7, mostly boreal, desert and remote rural,
12.4 % (Sect. 2.2).

**Coverage.** 84°N to 61°S, "covering the large majority of the ice-free land
surface outside Russia, China, Mongolia, and Greenland, where commercial licensing
restrictions preclude suitable source imagery" (Sect. 2.1). Antarctica is outside
the latitude range. Roughly 2.5 % of cells inside covered tiles are masked —
water plus a 9 m buffer, snow and ice, salt pans, low-texture surfaces, and any
cell differing by more than 50 m from Copernicus GLO-30 — and a masked cell is
masked in all six bands (Sect. 3.1.2). Heights over India are deliberately offset
to comply with the Government of India National Geospatial Policy, tapering off
across the border over tens of kilometres (Sect. 2.1.3).

**Bands and datum.** Each cell ships a five-band DSM COG (`dsm`,
`dsm_percentile_5`, `dsm_percentile_50`, `dsm_percentile_95`, `dsm_std`) and a
one-band DTM COG, all Float32 in metres with NaN nodata, on an exact 15 m grid in
the cell's local UTM zone. Heights are **orthometric, referenced to the EGM96
geoid** ([EPSG:5773](https://epsg.io/5773)) — not ellipsoidal. Height above ground
is `dsm − dtm`, and the DTM is clamped to never exceed the DSM.

**Accuracy.** Against airborne and drone lidar campaigns, GEM-15's mean
tile-debiased RMSE for the DSM is **3.37 m, against 3.85 m for Copernicus GLO-30**
(12.3 % lower), 4.35 m for AW3D30 and 4.82 m for NASADEM; on the seven benchmarks
surveyed after 2021 the margin widens to 3.69 m against 4.27 m (Sect. 4.1.1).
Against 4,071 CORS GNSS stations it is 4.92 m against 5.80 m. GLO-30 remains
slightly better on flat bare ground. The DTM is *not* more accurate than FABDEM
overall (3.57 m against 3.37 m) and is markedly weaker under closed canopy, where
its bias reaches +9.4 m (Sect. 4.2). Regional mean offsets against airborne lidar
run from −4.8 m to +1.9 m, so debias against local control where absolute
precision matters.

**Temporal extent.** The interval published here, 2022-01-01 to 2024-12-31, is the
*imagery acquisition era*, which is what a user searching by time wants. It is not
what the two upstream records carry: Google's
[GCS STAC collection](https://storage.googleapis.com/climate-and-sustainability/gem-15/stac/gem-15/collection.json)
repeats its own processing timestamp (2026-09-29) as both ends, and the
[Earth Engine entry](https://developers.google.com/earth-engine/datasets/catalog/projects_climate-and-sustainability_assets_gem-15_v1)
reports a single 2025-01-01 snapshot instant. The paper states imagery acquired
through 2024, with the dominant Pléiades Neo tier acquired 2022–2024; the 36.8 %
of cells built from older archive imagery predate this interval, and no per-cell
acquisition-date layer is published.

**What this mirror adds.** `items.parquet` is a STAC-GeoParquet index of all
13,485 items, so one range request replaces 13,485 JSON fetches. A global overview
COG shows the mosaic without touching any item. Terrarium-encoded raster
[PMTiles](https://docs.protomaps.com/pmtiles/) make the DSM usable as a MapLibre
`raster-dem` source for hillshade, color relief and 3D terrain, with three MapLibre
styles alongside. These derived layers exist for a concrete reason: the GEM-15 COGs
are served from Google's bucket, which honours HTTP range requests but sends **no
CORS headers** (verified 2026-11-13), so a browser cannot read them directly.
Server-side and desktop clients read the COGs fine over `/vsicurl/`; browser apps
use the layers this catalog hosts.

Licensed [CC-BY-4.0](https://creativecommons.org/licenses/by/4.0/) and requires the
attribution "This dataset is produced by Google". The dataset is experimental and
the preprint is under review."""


collection = {
    "type": "Collection",
    "stac_version": "1.1.0",
    "stac_extensions": [
        "https://schemas.portolan-sdi.org/portolan/v0.2.0/schema.json",
        "https://stac-extensions.github.io/file/v2.1.0/schema.json",
        "https://stac-extensions.github.io/item-assets/v1.0.0/schema.json",
        "https://stac-extensions.github.io/processing/v1.2.0/schema.json",
        "https://stac-extensions.github.io/projection/v2.0.0/schema.json",
        "https://stac-extensions.github.io/raster/v2.0.0/schema.json",
        "https://stac-extensions.github.io/render/v2.0.0/schema.json",
        "https://stac-extensions.github.io/scientific/v1.0.0/schema.json",
        "https://stac-extensions.github.io/version/v1.2.0/schema.json",
        "https://stac-extensions.github.io/web-map-links/v1.3.0/schema.json",
    ],
    "id": "gem-15",
    "title": "GEM-15: Surface and Terrain Elevation at 15 m",
    "description": DESCRIPTION,
    "version": "1.0",
    "experimental": True,
    "keywords": [
        "elevation", "DEM", "DSM", "DTM", "terrain", "topography",
        "canopy height", "building height", "stereo", "photogrammetry",
        "Google Research", "GEM-15",
    ],
    "license": "CC-BY-4.0",
    "providers": [
        {
            "name": "Google Research",
            "description": (
                "Produced GEM-15 from commercial Airbus and Maxar optical "
                "stereo imagery and publishes the COGs and source STAC."
            ),
            "roles": ["producer", "processor", "licensor"],
            "url": "https://research.google/",
        },
        {
            "name": "Portolan Mirrors",
            "description": (
                "Maintains this Portolan mirror: the item index, overview, "
                "terrain tiles, styles and documentation."
            ),
            "roles": ["host"],
            "url": "https://github.com/portolan-mirrors/gem-15-catalog",
        },
    ],
    "extent": {
        "spatial": {"bbox": [[-179.0, -61.0, 180.0, 84.0]]},
        "temporal": {
            "interval": [["2022-01-01T00:00:00Z", "2024-12-31T23:59:59Z"]]
        },
    },
    "summaries": {
        "gsd": [15.0],
        "proj:code": [f"EPSG:{code}" for code in UPSTREAM_EPSG],
        "platform": [
            "pleiades-neo", "worldview-2", "worldview-3", "worldview-4",
            "geoeye-1", "pleiades-1a", "pleiades-1b", "spot-6", "spot-7",
        ],
    },
    "item_assets": {
        "dsm": {
            "type": COG,
            "title": "Digital Surface Model (DSM), 5 bands",
            "description": (
                "Surface elevation at 15 m: the block mean of the native "
                "0.5 m DSM, plus the 5th, 50th and 95th percentiles and the "
                "standard deviation of the 36 sub-cells inside each 15 m "
                "pixel. Band order is dsm, dsm_percentile_5, "
                "dsm_percentile_50, dsm_percentile_95, dsm_std."
            ),
            "roles": ["data"],
            "bands": [
                band("dsm", "Top surface elevation, including buildings and "
                            "canopy: the mean of the native sub-cells."),
                band("dsm_percentile_5", "5th percentile of the native "
                                         "sub-cells. Picks up ground gaps, "
                                         "roads, street-canyon floors and "
                                         "lower canopy; the noisiest band "
                                         "under closed rainforest canopy, "
                                         "where ground gaps are occluded."),
                band("dsm_percentile_50", "Median of the native sub-cells. "
                                          "Less sensitive to outliers and "
                                          "step edges than the mean."),
                band("dsm_percentile_95", "95th percentile of the native "
                                          "sub-cells. Picks up roof ridges, "
                                          "ridgelines and emergent crowns; "
                                          "stereo smoothing lowers it "
                                          "slightly against lidar."),
                band("dsm_std", "Standard deviation of the native sub-cells: "
                                "sub-pixel roughness, which tracks canopy "
                                "variability and urban structural "
                                "complexity."),
            ],
        },
        "dtm": {
            "type": COG,
            "title": "Digital Terrain Model (DTM), 1 band",
            "description": (
                "Bare-earth elevation at 15 m: the median of the native "
                "1.0 m DTM sub-cells, clamped to never exceed the DSM. The "
                "DTM is model-inferred, so under closed canopy it is a "
                "prediction rather than a measurement."
            ),
            "roles": ["data"],
            "bands": [
                band("dtm", "Bare-earth terrain elevation, clamped to be no "
                            "greater than the DSM in the same pixel."),
            ],
        },
    },
    "assets": {
        "geoparquet-items": {
            "href": "./items.parquet",
            "type": "application/vnd.apache.parquet",
            "title": "STAC items as GeoParquet",
            "description": (
                "All 13,485 items as STAC-GeoParquet, spatially ordered, so "
                "one range request replaces 13,485 JSON fetches. Asset hrefs "
                "in the rows are absolute URLs into Google's bucket."
            ),
            "roles": ["collection-mirror"],
            "processing:lineage": (
                "Built from Google's published item JSON, upgraded from STAC "
                "1.0.0 to 1.1.0: raster:bands becomes bands, proj:epsg "
                "becomes proj:code."
            ),
        },
        "overview": {
            "href": "./gem-15_overview.tif",
            "type": COG,
            "title": "Global DSM overview (COG)",
            "description": (
                "The whole DSM mosaic in one 8-bit RGB + alpha COG in Web "
                "Mercator at about 960 m, for maps and coverage checks that "
                "should not touch 13,485 items. Alpha is 0 where no cell has "
                "data, which also makes it a coverage mask. Not an elevation "
                "layer: read the item COGs for heights."
            ),
            "roles": ["visual", "overview"],
            "proj:code": "EPSG:3857",
            "start_datetime": "2022-01-01T00:00:00Z",
            "end_datetime": "2024-12-31T23:59:59Z",
            "processing:lineage": (
                "Rendered from the DSM band's internal overviews, reprojected "
                "to EPSG:3857, coloured with the cyclic 100 m ramp and "
                "hillshade of Google's Earth Engine example script, and "
                "quantized to 8-bit RGB with an alpha band."
            ),
        },
        "terrain-dsm": {
            "href": "./terrain/gem-15-dsm-terrarium.pmtiles",
            "type": "application/vnd.pmtiles",
            "title": "DSM as terrarium-encoded terrain tiles (global)",
            "description": (
                "The DSM as Mapzen terrarium-encoded raster tiles, z0–z13, in "
                "one PMTiles archive. MapLibre reads this as a `raster-dem` "
                "source with `\"encoding\": \"terrarium\"`, which no Float32 "
                "COG can serve. Decode with "
                "elevation = (R * 256 + G + B / 256) − 32768. Terrarium has "
                "no nodata, so masked cells and ocean encode as 0 m."
            ),
            "roles": ["visual"],
            "processing:lineage": (
                "Resampled from the DSM band's internal overviews at the "
                "resolution matching each zoom, encoded to terrarium PNG "
                "tiles and packed into PMTiles."
            ),
        },
        "terrain-dsm-demo": {
            "href": "./terrain/gem-15-dsm-terrarium-demo-barcelona.pmtiles",
            "type": "application/vnd.pmtiles",
            "title": "DSM terrain tiles, Barcelona demo (z0–z14)",
            "description": (
                "A small single-area archive over Barcelona, built the same "
                "way as the global file but one zoom deeper, for examples and "
                "for testing a terrain client without pulling the global "
                "archive."
            ),
            "roles": ["visual"],
            "processing:lineage": (
                "Same pipeline as the global terrain archive, restricted to "
                "the Barcelona area and extended to z14."
            ),
        },
        "style-hypsometric": {
            "href": "./styles/hypsometric.json",
            "type": "application/vnd.mapbox.style+json",
            "title": "Hypsometric tint with hillshade (default)",
            "description": (
                "MapLibre GL style: colour-relief tint over the terrain "
                "tiles, with hillshade on top."
            ),
            "roles": ["style", "default"],
        },
        "style-hillshade": {
            "href": "./styles/hillshade.json",
            "type": "application/vnd.mapbox.style+json",
            "title": "Neutral hillshade",
            "description": (
                "MapLibre GL style: hillshade only, no tint, for use as a "
                "relief underlay beneath other data."
            ),
            "roles": ["style"],
        },
        "style-terrain-3d": {
            "href": "./styles/terrain-3d.json",
            "type": "application/vnd.mapbox.style+json",
            "title": "3D terrain",
            "description": (
                "MapLibre GL style: hypsometric tint and hillshade with the "
                "terrain tiles also set as the map's `terrain` source, plus a "
                "`sky` specification, for a tilted 3D view."
            ),
            "roles": ["style"],
        },
        "documentation": {
            "href": "./llms.txt",
            "type": "text/plain",
            "title": "llms.txt",
            "description": "Compact orientation file for language models.",
            "roles": ["documentation"],
        },
        "README": {
            "href": "./README.md",
            "type": "text/markdown",
            "title": "Collection README",
            "description": "The landing page for this collection.",
            "roles": ["metadata"],
        },
    },
    "renders": {
        "dsm": {
            "title": "Surface elevation, global range (−100 to 4000 m)",
            "assets": ["dsm"],
            "bidx": [1],
            "colormap_name": "terrain",
            "resampling": "bilinear",
            "rescale": [[-100, 4000]],
            "nodata": "nan",
            "minmax_zoom": [8, 22],
        },
        "dtm": {
            "title": "Bare-earth elevation, global range (−100 to 4000 m)",
            "assets": ["dtm"],
            "bidx": [1],
            "colormap_name": "terrain",
            "resampling": "bilinear",
            "rescale": [[-100, 4000]],
            "nodata": "nan",
            "minmax_zoom": [8, 22],
        },
        "dsm-local-relief": {
            "title": "Surface relief, local area (cyclic 100 m ramp)",
            "assets": ["dsm"],
            "bidx": [1],
            "colormap": cyclic_colormap,
            "resampling": "bilinear",
            "rescale": [[0, 100]],
            "nodata": "nan",
            "minmax_zoom": [12, 22],
            "description": (
                "Approximates the look of Google's Earth Engine example "
                "script, which colours elevation modulo 100 m and multiplies "
                "by hillshade. The render extension cannot express a modulo, "
                "so this ramp covers one 100 m window: move the rescale to a "
                "100 m band around the local terrain. For local areas only — "
                "a single 100 m window says nothing outside it."
            ),
        },
        "overview": {
            "title": "Global DSM overview, below zoom 8",
            "assets": ["overview"],
            "bidx": [1, 2, 3],
            "resampling": "bilinear",
            "minmax_zoom": [0, 7],
        },
    },
    "sci:doi": "10.31223/X55N75",
    "sci:citation": (
        "Batchu, V. V., Neumann, M., Li, T., Allen, B., Zhang, J., & "
        "Van Arsdale, C. (2026). GEM-15: Global surface and terrain elevation "
        "models (15 meters) from sub-meter optical stereo imagery. EarthArXiv "
        "[preprint]. https://eartharxiv.org/repository/view/16034/, "
        "doi:10.31223/X55N75"
    ),
    "links": [
        {"rel": "root", "href": "../catalog.json", "type": "application/json"},
        {"rel": "parent", "href": "../catalog.json",
         "type": "application/json"},
        {"rel": "license",
         "href": "https://creativecommons.org/licenses/by/4.0/",
         "type": "text/html",
         "title": "Creative Commons Attribution 4.0 International"},
        {"rel": "via",
         "href": "https://developers.google.com/earth-engine/datasets/catalog/"
                 "projects_climate-and-sustainability_assets_gem-15_v1",
         "type": "text/html",
         "title": "GEM-15 in the Earth Engine data catalog"},
        {"rel": "canonical",
         "href": "https://storage.googleapis.com/climate-and-sustainability/"
                 "gem-15/stac/gem-15/collection.json",
         "type": "application/json",
         "title": "Google's GEM-15 STAC collection"},
        {"rel": "cite-as",
         "href": "https://doi.org/10.31223/X55N75",
         "type": "text/html",
         "title": "GEM-15 preprint (EarthArXiv)"},
        {"rel": "describedby", "href": "./README.md", "type": "text/markdown",
         "title": "Collection README"},
        {"rel": "agents", "href": "./AGENTS.md", "type": "text/markdown",
         "title": "Collection agent guide"},
        {"rel": "pmtiles",
         "href": f"{PUBLIC_BASE}/gem-15/terrain/gem-15-dsm-terrarium.pmtiles",
         "type": "application/vnd.pmtiles",
         "title": "DSM terrain tiles (terrarium), global",
         "pmtiles:layers": ["dsm"]},
        {"rel": "pmtiles",
         "href": f"{PUBLIC_BASE}/gem-15/terrain/"
                 f"gem-15-dsm-terrarium-demo-barcelona.pmtiles",
         "type": "application/vnd.pmtiles",
         "title": "DSM terrain tiles (terrarium), Barcelona demo",
         "pmtiles:layers": ["dsm"]},
    ] + child_links,
    "updated": UPDATED,
}

OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps(collection, indent=2) + "\n")
print(f"wrote {OUT} ({OUT.stat().st_size} bytes, "
      f"{len(collection['links'])} links)")
