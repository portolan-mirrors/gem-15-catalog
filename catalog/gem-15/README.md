# GEM-15: Surface and Terrain Elevation at 15 m

**13,485 one-degree cells. 26,970 Cloud-Optimized GeoTIFFs. About 12.3 TB. 15 m
grid spacing, 84°N to 61°S.** GEM-15 is Google Research's experimental
near-global elevation dataset: a Digital Surface Model (DSM) and a matching
bare-earth Digital Terrain Model (DTM) built from sub-meter optical stereo
satellite imagery, not from radar or lidar. Agents and automated clients should
read [AGENTS.md](AGENTS.md) instead of this page.

This collection is a **metadata mirror**. The elevation pixels stay in Google's
public bucket and are referenced there. What this catalog adds is a queryable
index of every cell, a global overview image, browser-ready terrain tiles, three
MapLibre styles, and the documentation you are reading. Nothing here is a copy of
the elevation data itself.

Two things make GEM-15 different from the global DEMs most people reach for
first. It is optical, so it resolves single buildings and individual tree crowns
at 15 m where Copernicus GLO-30 shows a smooth plane. And it ships a
co-registered DSM and DTM on the exact same grid, so height above ground is a
subtraction rather than a modelling exercise. The method, the validation, and the
limitations are set out in the preprint, Batchu et al. (2026),
[doi:10.31223/X55N75](https://doi.org/10.31223/X55N75); the dataset's own home is
the
[Earth Engine catalog entry](https://developers.google.com/earth-engine/datasets/catalog/projects_climate-and-sustainability_assets_gem-15_v1),
and Google's authoritative STAC lives
[in the same bucket as the data](https://storage.googleapis.com/climate-and-sustainability/gem-15/stac/gem-15/collection.json).

## Quick Start

Read a real cell's DSM header without downloading its 190 MB. This is cell
`wm91sm1`, the degree square from 91°W to 90°W and 1°S to the equator, in the
Galápagos:

```sh
gdalinfo /vsicurl/https://storage.googleapis.com/climate-and-sustainability/gem-15/m10_m100/wm91sm1/dsm/zall/mosaic.tif
```

That prints `Size is 8192, 16384`, `WGS 84 / UTM zone 15S` (EPSG:32715), a pixel
size of 15 m, five Float32 bands with `NoData Value=nan`, and five levels of
internal overviews. The DTM for the same cell is the same URL with `dtm` in place
of `dsm`, and has one band.

## Going further: find the cells for an area, then read them as one raster

The interesting pattern is not reading one cell. It is treating the cells that
cover your area of interest as a single raster. Two steps do it.

First, ask `items.parquet` which cells cover the area. One HTTP range request
answers for the whole world, with no STAC API and no crawl of 297 sub-catalogs:

```sql
INSTALL httpfs; LOAD httpfs;
SELECT "gem:cell_id" AS cell,
       "proj:code"   AS utm_zone,
       assets.dsm.href AS dsm_url
FROM read_parquet('https://data.source.coop/portolan-mirrors/gem-15-catalog/gem-15/items.parquet')
WHERE bbox.xmin < 2.5 AND bbox.xmax > 1.5
  AND bbox.ymin < 42.0 AND bbox.ymax > 41.0
ORDER BY cell;
```

Then build a GDAL VRT over those hrefs and read the mosaic as one dataset:

```sh
duckdb -noheader -list -c "
  INSTALL httpfs; LOAD httpfs;
  SELECT '/vsicurl/' || assets.dsm.href
  FROM read_parquet('https://data.source.coop/portolan-mirrors/gem-15-catalog/gem-15/items.parquet')
  WHERE bbox.xmin < 2.5 AND bbox.xmax > 1.5
    AND bbox.ymin < 42.0 AND bbox.ymax > 41.0
    AND \"proj:code\" = 'EPSG:32631'
  ORDER BY 1
" > cells.txt

gdalbuildvrt -input_file_list cells.txt dsm.vrt
gdalinfo dsm.vrt
```

Filter on `proj:code` as that query does. Every cell sits in its own local UTM
zone, and `gdalbuildvrt` does not stop when the list mixes zones: it prints a
warning, **skips every cell outside the first cell's zone, and still exits 0**
(verified 2026-11-13 with GDAL 3.10.3). A VRT built without the filter quietly
holds fewer cells than you asked for. To cross a zone boundary, run `gdalwarp`
over the same list with an explicit `-t_srs` instead.

## Bands

Each cell has two assets. The DSM COG carries five bands in this order; the DTM
COG carries one. All bands are Float32, in metres, with NaN as nodata, on the
same exact 15 m grid.

| Asset | Band | Meaning |
|---|---|---|
| `dsm` | 1 `dsm` | Top surface elevation: buildings and canopy included. The block mean of the native 0.5 m DSM inside the 15 m cell. |
| `dsm` | 2 `dsm_percentile_5` | 5th percentile of the native sub-cells. Finds ground gaps, roads and street-canyon floors. Noisiest band under closed rainforest canopy, where the ground is occluded. |
| `dsm` | 3 `dsm_percentile_50` | Median of the native sub-cells. Less sensitive to outliers and step edges than the mean. |
| `dsm` | 4 `dsm_percentile_95` | 95th percentile. Roof ridges, ridgelines and emergent crowns. Stereo smoothing lowers it slightly against lidar. |
| `dsm` | 5 `dsm_std` | Standard deviation of the native sub-cells: sub-pixel roughness, which tracks canopy variability and urban structural complexity. |
| `dtm` | 1 `dtm` | Bare-earth elevation, clamped so it never exceeds the DSM in the same pixel. |

Heights are **orthometric, referenced to the EGM96 geoid**
([EPSG:5773](https://epsg.io/5773)). They are not ellipsoidal heights, and they
are not EGM2008 like Copernicus GLO-30 and FABDEM. Apply a geoid conversion
before you mix GEM-15 with those products for altimetry.

## Suggested uses

The Earth Engine entry lists three, and the paper works each one through a case
study:

- **Flood and inundation modelling.** Watershed, flow and inundation modelling
  on the 15 m DTM and DSM. In the paper's Eardisley (UK) case, the DTM isolates
  a stream corridor between bankside canopies and tracks 1 m lidar to within
  1–3 m over most of an 870 m reach, where GLO-30 shows a false barrier more
  than 20 m high (Sect. 5.2). The paper is explicit that a systematic evaluation
  for low-order reaches is still future work.
- **Canopy and building height.** Height above ground is `dsm − dtm`, or
  `dsm_percentile_95 − dtm` for crowns and ridges, with `dsm_std` as a local
  roughness measure. The 15 m grid resolves individual crowns, clearings, city
  blocks and mining benches; the paper measures the Kalgoorlie open pit at more
  than 115 m deep (Fig. 5). At Sinop, Brazil, a 30 m forest-to-pasture step
  lands inside a single 15 m pixel where 30 m DEMs spread it over 60–100 m
  (Sect. 5.3).
- **Topographic analysis.** Slope and aspect, and orthorectification of
  satellite imagery. In Christchurch, the DSM and the 95th percentile resolve
  individual rooftops where GLO-30 is a near-uniform plane, and the 5th
  percentile recovers street level (Sect. 5.4).

## Limitations and inappropriate uses

- **The DTM is a prediction, not a measurement, under dense canopy.** It comes
  from a model trained on US lidar, with no view of the ground. Under closed
  forest its error reaches about 9 m DRMSE, and its bias reaches **+9.4 m in
  Indonesia** (Sect. 4.2). Use
  [FABDEM](https://doi.org/10.1088/1748-9326/ac4d4f) instead where
  closed-canopy bare earth is the product you need.
- **Heights over India are deliberately offset.** Vertical offsets are built
  into both the DSM and the DTM to satisfy the Government of India National
  Geospatial Policy, and they taper off across the border over tens of
  kilometres. Do not use GEM-15 for absolute heights in or near India
  (Sect. 2.1.3).
- **The product is not independent of Copernicus GLO-30.** Any cell differing
  by more than 50 m from GLO-30 was removed as a suspected blunder. Real change
  larger than 50 m is removed with it: new open-pit mining benches, tall new
  towers, large landslides (Sect. 3.1.2, Sect. 5.5).
- **Regional vertical bias runs from −4.8 m to +1.9 m** against airborne lidar,
  with the largest offset in Canada (Fig. 6c). Debias against local control
  wherever absolute precision matters.
- **No coverage** over Russia, China, Mongolia or Greenland, where commercial
  licensing restrictions preclude suitable source imagery, nor over Antarctica,
  which is south of the 61°S limit (Sect. 2.1).
- **About 2.5 % of cells inside covered tiles are masked**: water plus a 9 m
  buffer, snow and ice, salt pans, low-texture surfaces, and the GLO-30
  outliers above. Masking is concentrated over perennial snowfields, glaciated
  peaks and inland water, and a masked cell is masked in **all six bands**
  (Sect. 3.1.2). The paper's own advice is that these gaps "can be filled using
  the Copernicus GLO-30 DEM at some cost to accuracy".
- **Acquisition dates are mixed and not published per cell.** 36.8 % of cells
  use pre-2022 archive imagery, and temporal transitions can appear along
  acquisition boundaries. There is no per-cell date layer.
- **Super-tall buildings, above roughly 50 m, are often poorly captured**
  because of stereo limitations, per the Earth Engine entry.
- **The dataset is experimental and the preprint is under review.**

## Accuracy, quantified

The paper's primary metric is tile-debiased RMSE (DRMSE) over 1.5 km tiles. It
reports no LE90 and no MAE.

| Comparison | GEM-15 | Copernicus GLO-30 | Others |
|---|---|---|---|
| DSM against airborne and drone campaigns, mean | **3.37 m** | 3.85 m | AW3D30 4.35 m, NASADEM 4.82 m |
| NMAD, same campaigns | **1.98 m** | 2.26 m | AW3D30 2.90 m, NASADEM 3.58 m |
| DSM, seven post-2021 campaigns | **3.69 m** | 4.27 m | |
| DSM against 4,071 CORS GNSS stations | **4.92 m** | 5.80 m | |
| Above-ground strata, more than 5 m | **3.95 m** | 4.31 m | |
| Bare ground, 5 m or less | 2.46 m | **2.33 m** | |
| Steep slopes, 10° or more | **4.19 m** | 4.82 m | |
| DTM against airborne campaigns, mean | 3.57 m | | FABDEM **3.37 m** |

GEM-15 beats AW3D30 and NASADEM on every campaign, on GEDI and on CORS. Against
GLO-30 the per-campaign result runs from 11.9 % worse (US Tropical) to 26.9 %
better (New Zealand 2024–25), and the margin grows with the date of the
reference survey at 3.44 % per year (Spearman ρ = 0.87, p = 0.001, Fig. 7).
GLO-30 is better on the three pre-2021 low-relief benchmarks by 0.05 to 0.16 m,
and better on open flat ground in the GEDI comparison. All figures are from
Sect. 4 and Tables E1–E3 of the preprint.

## Terrain in a web map

Google's bucket sends no CORS headers, so a browser cannot read the GEM-15 COGs
at all, and MapLibre's `raster-dem` source reads only terrarium or
Terrain-RGB encoded tiles, never Float32 GeoTIFF. This catalog therefore
publishes the DSM as a terrarium-encoded PMTiles archive, z0 to z13, which
MapLibre reads directly:

```js
import maplibregl from 'maplibre-gl';
import { Protocol } from 'pmtiles';

maplibregl.addProtocol('pmtiles', new Protocol().tile);

const map = new maplibregl.Map({
  container: 'map',
  center: [2.165, 41.395],
  zoom: 12,
  pitch: 60,
  style: {
    version: 8,
    sources: {
      gem15: {
        type: 'raster-dem',
        url: 'pmtiles://https://data.source.coop/portolan-mirrors/gem-15-catalog/gem-15/terrain/gem-15-dsm-terrarium.pmtiles',
        encoding: 'terrarium',
        tileSize: 256,
      },
    },
    layers: [{ id: 'hillshade', type: 'hillshade', source: 'gem15' }],
    terrain: { source: 'gem15', exaggeration: 1.5 },
  },
});
```

Decode a pixel yourself with `elevation = (R * 256 + G + B / 256) − 32768`.
Terrarium has no nodata value, so masked cells and ocean both encode as 0 m.
Three ready-made styles sit in `styles/`: `hypsometric.json` (the default, an
absolute elevation tint under a hillshade), `hillshade.json` (relief only) and
`terrain-3d.json` (tint, hillshade and 3D terrain). A smaller demo archive over
Barcelona, one zoom deeper, is at `terrain/gem-15-dsm-terrarium-demo-barcelona.pmtiles`.

## Files

| File | What it is |
|---|---|
| `collection.json` | This collection, with 297 `child` links into Google's STAC tree |
| `items.parquet` | All 13,485 items as STAC-GeoParquet, for one-request queries |
| `gem-15_overview.tif` | Global DSM overview, 8-bit RGB with alpha, Web Mercator, about 960 m |
| `terrain/gem-15-dsm-terrarium.pmtiles` | DSM as terrarium raster tiles, z0–z13, global |
| `terrain/gem-15-dsm-terrarium-demo-barcelona.pmtiles` | Same, Barcelona only, z0–z14 |
| `styles/*.json` | Three MapLibre GL styles over the terrain tiles |
| `AGENTS.md`, `llms.txt` | Guidance for agents and language models |

The elevation COGs themselves are not here. Each item's `dsm` and `dtm` hrefs are
absolute URLs under
`https://storage.googleapis.com/climate-and-sustainability/gem-15/`, which Google
keeps under a permanent no-deletion retention policy.

Browse the collection in the
[data browser](https://source.coop/portolan-mirrors/gem-15-catalog/gem-15).

## License and attribution

[CC-BY-4.0](https://creativecommons.org/licenses/by/4.0/). The Earth Engine terms
of use require this exact attribution:

> This dataset is produced by Google

Cite the dataset as:

> Batchu, V. V., Neumann, M., Li, T., Allen, B., Zhang, J., & Van Arsdale, C.
> (2026). GEM-15: Global surface and terrain elevation models (15 meters) from
> sub-meter optical stereo imagery. EarthArXiv [preprint].
> https://eartharxiv.org/repository/view/16034/, doi:10.31223/X55N75

The source imagery came from Airbus Defence and Space and from Maxar
Technologies, which the paper credits in its acknowledgements. Native-resolution
products cannot be redistributed, so the 0.5 m DSM and the 1 m DTM are not part
of the release.

## Provenance

This catalog is a mirror, not the official publication. Google Research produced
GEM-15 and publishes it three ways: as the Earth Engine ImageCollection
`projects/climate-and-sustainability/assets/gem-15_v1`, as an
[explorer app](https://climate-and-sustainability.projects.earthengine.app/view/gem-15),
and as COGs with a STAC catalog at
`gs://climate-and-sustainability/gem-15`. The evaluation code is
[on GitHub](https://github.com/google-research/google-research/tree/master/gem_15).

Portolan Mirrors hosts this copy. The item index, the overview, the terrain tiles
and the styles were derived from Google's published COGs and item JSON; the
pipeline that builds them, and this metadata, live in the
[catalog repository](https://github.com/portolan-mirrors/gem-15-catalog). The
upstream STAC is STAC 1.0.0 and carries `raster:bands`; the index here is
upgraded to STAC 1.1.0. This mirror syncs when Google releases a new GEM-15
version. Problems with the mirror belong in
[its issue tracker](https://github.com/portolan-mirrors/gem-15-catalog/issues);
problems with the data itself belong with the publisher, whose contact address is
on the Earth Engine entry.
