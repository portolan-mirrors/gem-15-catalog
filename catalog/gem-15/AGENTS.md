# Agent guide: GEM-15 surface and terrain elevation at 15 m

Every claim here is quoted from the GEM-15 preprint
([doi:10.31223/X55N75](https://doi.org/10.31223/X55N75)), from Google's own
metadata, or measured from the files. Measurements are dated. If a fact has no
source, it does not belong in this file.

## What the data is, and how it connects

Two elevation surfaces on one grid: a Digital Surface Model from sub-meter
optical stereo imagery, and a bare-earth Digital Terrain Model predicted from it
by a model trained on US airborne lidar. Height above ground is `dsm − dtm`. The
collection holds 13,485 items, one per one-degree cell, and the elevation COGs
live in Google's bucket, not in this catalog.

**Identifiers.** The stable key is `gem:cell_id`, the cell name. The STAC item
`id` is `gem-15_<cell_id>` (for example `gem-15_w2s41`). `gem:group_id` names the
10°×10° sub-catalog the item sits in.

**Cell names decode as `w[m]XX s[m]YY`:** `w` gives the **west-edge** longitude,
`s` the **south-edge** latitude, and a leading `m` means minus. Each cell spans
exactly one degree east and one degree north from that corner.

| Cell | West edge | South edge | bbox |
|---|---|---|---|
| `w2s41` | 2°E | 41°N | `[2, 41, 3, 42]` |
| `wm91sm1` | 91°W | 1°S | `[-91, -1, -90, 0]` |

Group names follow the same scheme in 10° steps, south-west corner first and
latitude first: `p40_p0` holds 40°N–50°N, 0°E–10°E; `m10_m100` holds 10°S–0°,
100°W–90°W. (Verified 2026-11-13 against the published items.)

**Every cell is in its own local UTM zone,** on an exact 15 m grid aligned to the
zone origin, and a cell never crosses a zone boundary (Sect. 2.1.3). The
consequence drives every mosaic you build: cells from different zones are in
different CRSs, so any operation spanning a zone boundary needs a reprojection
first. Google's own Earth Engine example script says so and reprojects the mosaic
to EPSG:3857 before it computes a hillshade, because the mosaic "does not retain
a single native projection".

## Runnable access patterns

Asset hrefs on the items are **absolute URLs into Google's bucket**, so nothing
needs resolving against a base.

Fetch one item's metadata:

```sh
curl -s https://storage.googleapis.com/climate-and-sustainability/gem-15/stac/gem-15/m10_m100/wm91sm1/wm91sm1.json
```

The URL template is `<stac>/<gem:group_id>/<gem:cell_id>/<gem:cell_id>.json`
under `https://storage.googleapis.com/climate-and-sustainability/gem-15/stac/gem-15/`.

Read a COG header over HTTP range requests, no download:

```sh
gdalinfo /vsicurl/https://storage.googleapis.com/climate-and-sustainability/gem-15/m10_m100/wm91sm1/dsm/zall/mosaic.tif
```

The COG URL template is `<data>/<group>/<cell>/<dsm|dtm>/zall/mosaic.tif` under
`https://storage.googleapis.com/climate-and-sustainability/gem-15/`. Pixel
dimensions vary with latitude: 8192 × 8192 for `w2s41`, 8192 × 16384 for
`wm91sm1` (measured 2026-11-13). The files are Float32 with 512 px blocks and
five internal overview levels, so an `out_shape` or `-outsize` read costs only a
few range requests.

Query the whole collection in one range request, from the item index:

```sql
INSTALL httpfs; LOAD httpfs;
SELECT "gem:cell_id" AS cell, "proj:code" AS utm, assets.dsm.href, assets.dtm.href
FROM read_parquet('https://data.source.coop/portolan-mirrors/gem-15-catalog/gem-15/items.parquet')
WHERE bbox.xmin < 2.5 AND bbox.xmax > 1.5
  AND bbox.ymin < 42.0 AND bbox.ymax > 41.0;
```

Assemble several cells as one raster. Filter to a single UTM zone first:

```sh
duckdb -noheader -list -c "
  INSTALL httpfs; LOAD httpfs;
  SELECT '/vsicurl/' || assets.dsm.href
  FROM read_parquet('https://data.source.coop/portolan-mirrors/gem-15-catalog/gem-15/items.parquet')
  WHERE bbox.xmin < 2.5 AND bbox.xmax > 1.5
    AND bbox.ymin < 42.0 AND bbox.ymax > 41.0
    AND \"proj:code\" = 'EPSG:32631'
" > cells.txt
gdalbuildvrt -input_file_list cells.txt dsm.vrt
```

To span zones, skip the VRT and warp the same list to one CRS. `-ovr AUTO` makes
`gdalwarp` read the internal overview that matches the output resolution, which
keeps a coarse preview cheap:

```sh
gdalwarp -t_srs EPSG:3857 -tr 2000 2000 -ovr AUTO -r average -overwrite \
  $(cat cells.txt | tr '\n' ' ') preview.tif
```

Drop `-tr` and `-ovr` for a full-resolution mosaic, and expect it to read every
source pixel.

Every recipe above was run on 2026-11-13. The DuckDB recipes were run against a
sample index built from upstream item JSON with the same schema as the published
file; the published `items.parquet` is the full 13,485-item index.

## Quirks that produce silently wrong answers

- **Heights are orthometric on the EGM96 geoid (EPSG:5773), not ellipsoidal.**
  Treating them as WGS 84 ellipsoidal heights introduces the full geoid
  separation. Measured with pyproj 3.7.1 on 2026-11-13: **+49.5 m at Barcelona,
  +71.1 m in Papua New Guinea, −103.7 m south of India.** This is the largest
  error available in this dataset and nothing in the pixels reveals it.
- **GLO-30, AW3D30, NASADEM and FABDEM use EGM2008.** Differencing them against
  GEM-15 without a geoid conversion adds a smooth regional error. The paper
  converted the references EGM2008 to EGM96 before evaluating. Google's example
  script calls the visual difference minimal and still asks for a geoid
  correction before precise altimetry.
- **Band order in the five-band DSM COG is `dsm`, `dsm_percentile_5`,
  `dsm_percentile_50`, `dsm_percentile_95`, `dsm_std`. Band 1 is the DSM, not
  the 5th percentile.** The COG carries no band names or descriptions:
  `gdalinfo` reports `ColorInterp=Gray` for band 1 and `Undefined` for bands 2
  to 5 (verified 2026-11-13). The order exists only in the STAC `bands` array.
- **Nodata is NaN, not a sentinel.** `value == nodata` never matches. Use
  `numpy.isnan`, `IS NOT DISTINCT FROM`, or GDAL's mask band. Masked cells are
  masked in **all six bands**, so a cell missing from the DSM is missing from
  the DTM too.
- **`gdalbuildvrt` does not fail on mixed UTM zones.** It prints a warning,
  skips every input outside the first input's zone, and exits 0 (verified
  2026-11-13, GDAL 3.10.3). An unfiltered VRT silently contains fewer cells than
  the list you gave it. Always filter on `proj:code`, or use `gdalwarp`.
- **`dtm <= dsm` holds at full resolution only.** The clamp is applied before
  aggregation (Sect. 3.1.2), and a full-resolution window confirms it:
  `min(dsm − dtm) = 0.0000` over a 512 × 512 window of cell `w0s41`. The same
  subtraction on the two files' 512 px **overview levels** gives −1.86 m,
  because GDAL resamples the DSM and the DTM independently (both measured
  2026-11-13). Compute height above ground at full resolution, or expect small
  negatives.
- **Item `datetime` is a processing timestamp, not an acquisition date.** Every
  item carries `2026-09-29T14:17:40Z` for `datetime`, `created` and `updated`
  (verified 2026-11-13). The collection's temporal extent is the imagery
  acquisition era instead. GEM-15 publishes **no per-cell acquisition date**, so
  no filter on these items can select imagery by date.
- **Heights over India are deliberately offset** in both the DSM and the DTM,
  tapering off outside India over tens of kilometres (Sect. 2.1.3). The offset is
  not flagged in the data. Absolute heights in that region are wrong on purpose.
- **Google's bucket sends no CORS headers** (verified 2026-11-13: a `HEAD` with
  an `Origin` header returns 200 with `accept-ranges: bytes` and no
  `access-control-allow-origin`). Range requests work from a server or a desktop
  GIS, and fail from a browser. For anything browser-side, use this catalog's
  terrain PMTiles and overview, which are served with CORS.
- **The upstream COGs embed no band statistics.** `gdalinfo` reports no
  minimum, maximum, mean or standard deviation (verified 2026-11-13). Compute
  your own, or render with the collection's `renders`, which carry explicit
  rescale ranges.
- **The two sources disagree on how the DTM is aggregated.** The paper says the
  15 m DTM is the **median** of the native sub-cells, then clamped to the DSM
  (Sect. 3.1.2, App. B). The Earth Engine band table calls it the **mean** of
  the 2.5 m bilinear native DTM. The difference is small but real, and neither
  source is dated later than the other. Do not build an argument on which
  statistic it is.
- **The upstream STAC is 1.0.0 and uses `raster:bands` and `proj:epsg`.** This
  catalog's `items.parquet` and `item_assets` are upgraded to STAC 1.1: the
  field is `bands`, and the CRS is the string `proj:code` ("EPSG:32631"), not
  the integer `proj:epsg`. Code written against one form breaks on the other.

## Band semantics

| Band | What it measures | Use it for |
|---|---|---|
| `dsm` | Mean of the native 0.5 m sub-cells: the top surface, buildings and canopy included | General surface elevation, orthorectification, slope and aspect |
| `dsm_percentile_5` | 5th percentile of the sub-cells | Ground gaps, roads, street-canyon floors, lower canopy. Highest error under closed rainforest canopy, where ground gaps are occluded (Sect. 4.3) |
| `dsm_percentile_50` | Median of the sub-cells | A mean less sensitive to outliers and step edges |
| `dsm_percentile_95` | 95th percentile | Roof ridges, ridgelines, emergent crowns. Stereo smooths crown tips and roof peaks, which lowers it (Sect. 4.3) |
| `dsm_std` | Standard deviation of the sub-cells | Sub-pixel roughness: canopy variability, urban structural complexity. DRMSE 0.66–1.60 m outside rainforest (Sect. 4.3) |
| `dtm` | Median of the native 1 m DTM sub-cells, clamped to the DSM | Bare earth, hydrology, height above ground. Model-inferred under canopy, bias up to +9.4 m under closed canopy (Sect. 4.2) |

Bias decreases monotonically from P5 to P95, and all four percentile layers sit
within 1.5 m DRMSE of the DSM everywhere (Sect. 4.3).

## Query recipes

Count the cells in each UTM zone, to see where a mosaic will need reprojection:

```sql
SELECT "proj:code", count(*) AS cells
FROM read_parquet('https://data.source.coop/portolan-mirrors/gem-15-catalog/gem-15/items.parquet')
GROUP BY 1 ORDER BY cells DESC;
```

Find the cell that contains a point, and get both COG URLs:

```sql
SELECT "gem:cell_id", "proj:code", assets.dsm.href, assets.dtm.href
FROM read_parquet('https://data.source.coop/portolan-mirrors/gem-15-catalog/gem-15/items.parquet')
WHERE bbox.xmin <= 2.165 AND bbox.xmax > 2.165
  AND bbox.ymin <= 41.395 AND bbox.ymax > 41.395;
```

Height above ground for one cell, read from internal overviews:

```python
import numpy as np, rasterio
base = "https://storage.googleapis.com/climate-and-sustainability/gem-15/p40_p0/w0s41"
with rasterio.open(f"{base}/dsm/zall/mosaic.tif") as src:
    dsm = src.read(1, out_shape=(512, 512))      # band 1 is dsm
with rasterio.open(f"{base}/dtm/zall/mosaic.tif") as src:
    dtm = src.read(1, out_shape=(512, 512))
height = dsm - dtm                                # NaN stays NaN
print(np.nanmedian(height), np.nanmax(height))
```

That run returned a median of 0.68 m and a maximum of 114.49 m over cell
`w0s41` on 2026-11-13. Read the full-resolution window instead of overviews when
the sign of `height` matters: see the clamp quirk above.

All three recipes were run on 2026-11-13, the DuckDB ones against a sample index
with the published schema.

## Related data and layers

- **Terrain tiles.** `terrain/gem-15-dsm-terrarium.pmtiles` holds the DSM as
  Mapzen terrarium-encoded raster tiles, z0 to z13, for MapLibre's `raster-dem`
  source. Decode with `elevation = (R * 256 + G + B / 256) − 32768`. Terrarium
  has no nodata, so masked cells and ocean encode as 0 m, which makes the tiles
  unsuitable for analysis. Use them to draw, not to measure.
  `terrain/gem-15-dsm-terrarium-demo-barcelona.pmtiles` is the same encoding over
  one area, one zoom deeper, for tests.
- **Styles.** `styles/hypsometric.json` (default), `styles/hillshade.json` and
  `styles/terrain-3d.json` are self-contained MapLibre GL styles over those
  tiles.
- **Overview.** `gem-15_overview.tif` is the whole mosaic as one 8-bit RGB COG
  with an alpha band in EPSG:3857. Its alpha channel doubles as a coverage mask.
  It holds no elevation values.
- **Copernicus GLO-30** is the gap-fill the paper itself recommends: masked
  cells "can be filled using the Copernicus GLO-30 DEM at some cost to accuracy"
  (Sect. 3.1.2). Convert EGM2008 to EGM96 first. Note that GLO-30 is already
  inside GEM-15's production chain as the 50 m outlier filter, so the two are not
  independent.
- **FABDEM** is more accurate than the GEM-15 DTM under closed forest
  (Sect. 4.2). Prefer it where that is the terrain you care about.

## Provenance

Google Research produces GEM-15 and hosts the COGs, the per-item STAC and the
10°×10° group catalogs. This collection carries 297 `child` links straight into
that tree, so the item JSON an agent reads is Google's own, never a copy. No item
JSON is published here.

Portolan Mirrors derives four files from that upstream: `items.parquet` from the
13,485 item JSONs (upgraded to STAC 1.1 and spatially ordered), the overview COG
and the terrain PMTiles from the DSM COGs' internal overviews, and the styles.
The scripts that build them are in `tools/` in the
[catalog repository](https://github.com/portolan-mirrors/gem-15-catalog), which
also holds the design contract and the issue tracker. Each derived asset records
its derivation in `processing:lineage`.
