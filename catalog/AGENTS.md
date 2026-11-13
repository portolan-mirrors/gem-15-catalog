# AGENTS.md — GEM-15 catalog

Guidance for AI agents and automated clients working with this catalog.

**One rule survives every edit to this file.** Every claim here is either quoted
from a source or measured from the data. If you cannot point at where a fact
came from, it does not belong in this file. An agent acting on an invented join
key or an invented column name produces a confident wrong answer, and nothing
downstream catches it.

## What this catalog holds

Root: `https://data.source.coop/portolan-mirrors/gem-15-catalog/catalog.json`.

One collection, `gem-15`: Google Research's GEM-15 elevation models at 15 m,
13,485 one-degree cells, each with a five-band Digital Surface Model COG and a
one-band bare-earth Digital Terrain Model COG. This catalog is a metadata
mirror. It publishes no elevation pixels and no item JSON. The COGs and the
per-item STAC stay in Google's public bucket at
`https://storage.googleapis.com/climate-and-sustainability/gem-15/`.

Read `gem-15/AGENTS.md` before writing any query. It decodes the cell naming
scheme, gives the runnable DuckDB and GDAL recipes, and lists the quirks that
produce silently wrong answers. The two that cost the most are there: heights
are orthometric on the EGM96 geoid rather than ellipsoidal, and every cell is in
its own local UTM zone.

## How to navigate

Two routes reach the items, and they return the same facts.

1. **The item index.** `gem-15/items.parquet` is every item as
   STAC-GeoParquet. One HTTP range request answers a spatial question for the
   whole world. This is the route to use.
2. **The child links.** `gem-15/collection.json` carries 297 `child` links, each
   an absolute URL to one of Google's 10°×10° group catalogs, which link their
   item JSON. Follow these when you need Google's own item document rather than
   this catalog's copy of its fields.

The collection also carries assets a client can use without any item: an
`overview` COG of the whole mosaic, `terrain-dsm` PMTiles for MapLibre, and
three MapLibre styles.

## Join keys

No two collections join here, because there is one collection. Within it,
`gem:cell_id` is the stable key, unique across the 13,485 items, and the STAC
item `id` is `gem-15_<gem:cell_id>`. `gem:group_id` names the group catalog that
holds an item, and is the path segment its URLs use.

## Quirks that produce silently wrong answers

- **Google's bucket sends no CORS headers.** Verified 2026-11-13: a `HEAD` with
  an `Origin` header returns 200 with `accept-ranges: bytes` and no
  `access-control-allow-origin`. A server or desktop client reads the COGs over
  range requests without trouble. A browser cannot read them at all. Browser
  clients use this catalog's terrain PMTiles and overview instead, which are
  served with CORS from Source Cooperative.
- **Hrefs in this catalog point at two different hosts.** The collection's own
  assets are relative paths under
  `https://data.source.coop/portolan-mirrors/gem-15-catalog/`. Every item asset
  href, and every `child` link, is an absolute URL into Google's bucket. Do not
  resolve item asset hrefs against this catalog's base.
- **Upstream is STAC 1.0.0; this catalog is STAC 1.1.0.** Google's item JSON
  uses `raster:bands` and the integer `proj:epsg`. This catalog's `items.parquet`
  and `item_assets` use `bands` and the string `proj:code`. Code written against
  one form fails on the other.

## Structure

Assets and structural links resolve relative to the object that carries them.
The root catalog carries an absolute `self` link to its published location.
