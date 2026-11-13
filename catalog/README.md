# GEM-15: Google Elevation Models at 15 m

GEM-15, Google Research's experimental near-global surface and terrain elevation
models, mirrored on Source Cooperative as a Portolan catalog.

Agents and automated clients should read [AGENTS.md](AGENTS.md) instead of this
page.

## What is here

One collection.

| Collection | Items | Grid | Coverage | What it is |
|---|---|---|---|---|
| [`gem-15`](gem-15/README.md) | 13,485 one-degree cells | 15 m | 84°N–61°S | A 5-band Digital Surface Model and a co-registered 1-band bare-earth Digital Terrain Model, from sub-meter optical stereo imagery |

The 26,970 Cloud-Optimized GeoTIFFs behind those items, about 12.3 TB, stay in
Google's public bucket and are referenced there. What this catalog publishes is
the metadata, an index of every cell as STAC-GeoParquet, a global overview image,
terrarium-encoded terrain tiles for MapLibre, three MapLibre styles, and the
documentation. The collection's [README](gem-15/README.md) covers bands,
accuracy and limitations; its [agent guide](gem-15/AGENTS.md) covers access
patterns and the traps.

Browse the catalog in the
[data browser](https://source.coop/portolan-mirrors/gem-15-catalog).

## License

[CC-BY-4.0](https://creativecommons.org/licenses/by/4.0/). The Earth Engine terms
of use require this exact attribution:

> This dataset is produced by Google

## Provenance

This catalog is a mirror, not the official publication. Google Research produced
GEM-15 and publishes it as an
[Earth Engine ImageCollection](https://developers.google.com/earth-engine/datasets/catalog/projects_climate-and-sustainability_assets_gem-15_v1),
as an
[explorer app](https://climate-and-sustainability.projects.earthengine.app/view/gem-15),
and as COGs with a STAC catalog at
[`gs://climate-and-sustainability/gem-15`](https://storage.googleapis.com/climate-and-sustainability/gem-15/stac/gem-15/collection.json),
under a permanent no-deletion retention policy. The method and validation are in
the preprint, Batchu et al. (2026),
[doi:10.31223/X55N75](https://doi.org/10.31223/X55N75).

Portolan Mirrors hosts this copy. It syncs when Google releases a new GEM-15
version, not on a clock, because the source is a versioned release rather than a
feed. The metadata and the pipeline that builds the derived files live in the
[catalog repository](https://github.com/portolan-mirrors/gem-15-catalog), with an
[issue tracker](https://github.com/portolan-mirrors/gem-15-catalog/issues) for
problems with the mirror.

## Access

Find the cells covering a point, with one HTTP range request, in DuckDB:

```sql
INSTALL httpfs; LOAD httpfs;
SELECT "gem:cell_id" AS cell, "proj:code" AS utm_zone, assets.dsm.href
FROM read_parquet('https://data.source.coop/portolan-mirrors/gem-15-catalog/gem-15/items.parquet')
WHERE bbox.xmin <= 2.165 AND bbox.xmax > 2.165
  AND bbox.ymin <= 41.395 AND bbox.ymax > 41.395;
```

Then read that cell's five-band DSM straight from Google's bucket, no download:

```sh
gdalinfo /vsicurl/https://storage.googleapis.com/climate-and-sustainability/gem-15/p40_p0/w2s41/dsm/zall/mosaic.tif
```
