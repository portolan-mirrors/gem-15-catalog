#!/usr/bin/env python3
"""Build ``staging/publish/gem-15/items.parquet`` from Google's own STAC.

GEM-15 publishes one STAC 1.0.0 item per 1x1-degree cell (13,485 of them) at
``https://storage.googleapis.com/climate-and-sustainability/gem-15/stac/gem-15/
{group}/{cell}/{cell}.json``, reachable by crawling the upstream collection:

    collection.json  -(297 "child" links)->  {group}/catalog.json
    {group}/catalog.json  -("item" links)->  {cell}/{cell}.json

This script crawls that tree, caches every JSON document it fetches, upgrades
each item to STAC 1.1, and writes the whole set as one STAC-GeoParquet file.
Nothing here ever uploads a COG or a per-item JSON of our own: we mirror
metadata, Google keeps hosting the bytes (see DESIGN.md).

    python3 tools/build_items_parquet.py                 # full run, resumable
    python3 tools/build_items_parquet.py --limit 40 \\
        --output staging/smoke/items-sample.parquet      # smoke test

Resumability: every group catalog and every item JSON is cached to disk
(``--cache-dir``, default ``staging/cache``) under its own path before being
parsed. A second run skips any URL whose cache file already exists. Pass
``--force`` to refetch everything (e.g. after Google revises the upstream
STAC).

## STAC 1.0.0 -> 1.1 transform, and why

- ``stac_version`` -> ``"1.1.0"``.
- ``raster:bands`` on each asset -> core ``bands``. STAC 1.1 promoted most of
  the raster extension's per-band fields into the core Band Object with the
  same, unprefixed names (``data_type``, ``nodata``, ``unit``,
  ``description``, ``name``), but **not** ``spatial_resolution`` -- that one
  stays on the raster extension (v2.0.0), so it is renamed to
  ``raster:spatial_resolution`` on every band. The ``raster`` extension URL
  therefore stays in ``stac_extensions`` (bumped v1.1.0 -> v2.0.0 alongside
  the rest of the 1.1 upgrade), matching the same choice already made for
  ``item_assets`` in ``catalog/gem-15/collection.json``: both describe the
  same bands and must agree on which fields are prefixed.
- ``proj:epsg`` (an integer) -> also get ``proj:code`` (``"EPSG:<n>"``), per
  the projection extension v2.0.0 that ships alongside STAC 1.1. We keep
  ``proj:epsg`` *alongside* ``proj:code`` rather than replacing it, since a
  consumer that only knows the older field should not silently lose it.
  ``stac_extensions`` is bumped from projection v1.1.0 to v2.0.0 to match.
- ``collection`` is already ``"gem-15"`` upstream; left as-is.
- Upstream ``links`` (``root``, ``parent``, ``collection``) are relative
  hrefs into *Google's* catalog tree and resolve to nothing useful once the
  item is a row in our parquet file, so they are dropped. One link is added:
  ``rel: canonical`` to the exact upstream item URL this row was built from,
  so a reader can always get back to Google's own copy.
- Nothing else is touched: geometry, bbox, properties (including the
  vendor ``gem:cell_id``/``gem:group_id`` fields, kept as upstream ships
  them), and the ``file:*``/``alternate`` asset fields are copied through.

## Row order and the bbox covering column

Rows are sorted by the quadkey (Z-order / Microsoft quadtree index) of each
cell's bbox centroid at zoom 12 (``mercantile.quadkey``), giving the kind of
spatial locality a hilbert curve would, with a stdlib-adjacent dependency
already in the environment and no new one to add. This groups neighboring
1-degree cells into the same byte range, so a bbox-filtered reader using the
GeoParquet 1.1 "bbox" covering column prunes whole row ranges instead of
scanning the file.

That covering column comes from the ``stac_geoparquet`` library for free:
every STAC item carries a ``bbox``, which the library turns into a
``{xmin,ymin,xmax,ymax}`` struct column, and ``to_parquet(..., 1.1.0)``
records it as the GeoParquet 1.1 "covering" in the ``geo`` metadata. Standard
Parquet per-column statistics (min/max) on that struct's fields are what let
a reader prune without touching the covering spec at all; pyarrow writes
those by default.

## Row groups

13,485 rows is far under the spec's 150,000-row-group ceiling, so this script
writes exactly one row group by asking ``stac_geoparquet`` for one Arrow
batch covering every row (``--row-group-size``, default 150000, is the chunk
size passed to ``parse_stac_items_to_arrow``; one chunk == one row group when
chunk size >= row count). A future run against a bigger upstream would only
need to lower this to re-enable multiple row groups.
"""
from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin

ROOT = Path(__file__).resolve().parent.parent

COLLECTION_URL = (
    "https://storage.googleapis.com/climate-and-sustainability/gem-15/"
    "stac/gem-15/collection.json"
)

DEFAULT_CACHE_DIR = ROOT / "staging" / "cache"
DEFAULT_OUTPUT = ROOT / "staging" / "publish" / "gem-15" / "items.parquet"

# Most of the raster extension's per-band fields were promoted into the STAC
# 1.1 core Band Object with the same names; spatial_resolution was not, so it
# moves to raster:spatial_resolution and the raster extension stays declared
# (see module docstring, and catalog/gem-15/collection.json's item_assets,
# which makes the same choice for the same bands).
PROJECTION_EXT_1_1 = "https://stac-extensions.github.io/projection/v1.1.0/schema.json"
PROJECTION_EXT_2_0 = "https://stac-extensions.github.io/projection/v2.0.0/schema.json"
RASTER_EXT_1_1 = "https://stac-extensions.github.io/raster/v1.1.0/schema.json"
RASTER_EXT_2_0 = "https://stac-extensions.github.io/raster/v2.0.0/schema.json"

# Band Object fields STAC 1.1 made core (unprefixed); everything else on a
# raster:bands entry (just spatial_resolution, for GEM-15) keeps the raster:
# prefix.
CORE_BAND_FIELDS = {"name", "description", "data_type", "nodata", "unit"}

PROGRESS_EVERY = 500


def log(msg: str) -> None:
    print(msg, flush=True)


def fetch_json(session, url: str, timeout: float = 30.0, retries: int = 4):
    """GET url, parse as JSON, retrying with backoff on transient failures."""
    last_exc = None
    for attempt in range(retries):
        try:
            resp = session.get(url, timeout=timeout)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001 - retry any transient failure
            last_exc = exc
            if attempt < retries - 1:
                time.sleep(min(2.0 * (attempt + 1), 10.0))
    raise RuntimeError(f"failed to fetch {url}: {last_exc}") from last_exc


def cached_json(session, url: str, cache_path: Path, force: bool) -> dict:
    """Return the JSON at url, via the on-disk cache at cache_path."""
    if not force and cache_path.exists():
        try:
            return json.loads(cache_path.read_text())
        except json.JSONDecodeError:
            pass  # corrupt cache entry; refetch below
    data = fetch_json(session, url)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(data))
    return data


def load_group_catalog_urls(session, cache_dir: Path, force: bool) -> list[tuple[str, str]]:
    """[(group_id, group_catalog_url), ...] from the upstream collection."""
    coll = cached_json(
        session, COLLECTION_URL, cache_dir / "collection.json", force
    )
    out = []
    for link in coll.get("links", []):
        if link.get("rel") != "child":
            continue
        url = urljoin(COLLECTION_URL, link["href"])
        group_id = url.rstrip("/").split("/")[-2]
        out.append((group_id, url))
    if not out:
        raise RuntimeError("collection.json carried no child links")
    return sorted(out)


def enumerate_items(
    session, groups: list[tuple[str, str]], cache_dir: Path, workers: int, force: bool
) -> list[tuple[str, str, str]]:
    """[(group_id, cell_id, item_url), ...] across every group catalog."""
    specs: list[tuple[str, str, str]] = []

    def one(group_id: str, url: str) -> list[tuple[str, str, str]]:
        cache_path = cache_dir / "groups" / f"{group_id}.json"
        catalog = cached_json(session, url, cache_path, force)
        rows = []
        for link in catalog.get("links", []):
            if link.get("rel") != "item":
                continue
            item_url = urljoin(url, link["href"])
            cell_id = link.get("title") or item_url.rstrip("/").split("/")[-2]
            rows.append((group_id, cell_id, item_url))
        return rows

    log(f"fetching {len(groups)} group catalogs...")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(one, g, u): g for g, u in groups}
        done = 0
        for fut in as_completed(futures):
            specs.extend(fut.result())
            done += 1
            if done % 50 == 0 or done == len(groups):
                log(f"  {done}/{len(groups)} group catalogs")
    return sorted(specs)


def fetch_items(
    session,
    specs: list[tuple[str, str, str]],
    cache_dir: Path,
    workers: int,
    force: bool,
) -> list[tuple[tuple[str, str, str], dict]]:
    """Fetch (or load from cache) every item JSON. Returns (spec, raw_item)."""
    results: list[tuple[tuple[str, str, str], dict]] = []
    errors: list[str] = []

    def one(spec: tuple[str, str, str]) -> tuple[tuple[str, str, str], dict]:
        group_id, cell_id, url = spec
        cache_path = cache_dir / "items" / group_id / f"{cell_id}.json"
        return spec, cached_json(session, url, cache_path, force)

    log(f"fetching {len(specs)} item documents "
        f"({workers} workers, cache: {cache_dir / 'items'})...")
    done = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(one, s): s for s in specs}
        for fut in as_completed(futures):
            done += 1
            try:
                results.append(fut.result())
            except Exception as exc:  # noqa: BLE001 - keep going, report at end
                errors.append(str(exc))
            if done % PROGRESS_EVERY == 0 or done == len(specs):
                rate = done / max(time.time() - t0, 1e-6)
                log(f"  {done}/{len(specs)} items ({rate:.0f}/s)")
    if errors:
        log(f"WARNING: {len(errors)} item fetch(es) failed, e.g.:")
        for e in errors[:5]:
            log(f"  {e}")
        if len(results) == 0:
            raise SystemExit("all item fetches failed; aborting")
    return sorted(results, key=lambda r: (r[0][0], r[0][1]))


def transform_item(raw: dict, item_url: str) -> dict:
    """Upgrade one upstream STAC 1.0.0 item dict to STAC 1.1 in-place-ish."""
    item = dict(raw)
    item["stac_version"] = "1.1.0"

    exts = [
        RASTER_EXT_2_0 if e == RASTER_EXT_1_1 else
        PROJECTION_EXT_2_0 if e == PROJECTION_EXT_1_1 else
        e
        for e in item.get("stac_extensions", [])
    ]
    item["stac_extensions"] = exts

    props = dict(item.get("properties", {}))
    epsg = props.get("proj:epsg")
    if epsg is not None and "proj:code" not in props:
        props["proj:code"] = f"EPSG:{epsg}"
    item["properties"] = props

    assets = {}
    for key, asset in item.get("assets", {}).items():
        asset = dict(asset)
        if "raster:bands" in asset:
            asset["bands"] = [
                {
                    (k if k in CORE_BAND_FIELDS else f"raster:{k}"): v
                    for k, v in band.items()
                }
                for band in asset.pop("raster:bands")
            ]
        a_epsg = asset.get("proj:epsg")
        if a_epsg is not None and "proj:code" not in asset:
            asset["proj:code"] = f"EPSG:{a_epsg}"
        assets[key] = asset
    item["assets"] = assets

    item["links"] = [
        {"rel": "canonical", "href": item_url, "type": "application/json"}
    ]
    item.setdefault("collection", "gem-15")
    return item


def centroid(item: dict) -> tuple[float, float]:
    bbox = item["bbox"]
    return ((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0)


def quadkey_sort_key(item: dict, zoom: int):
    import mercantile

    lon, lat = centroid(item)
    tile = mercantile.tile(lon, lat, zoom)
    return mercantile.quadkey(tile)


def write_parquet(items: list[dict], output: Path, row_group_size: int) -> None:
    from stac_geoparquet import arrow as sga

    output.parent.mkdir(parents=True, exist_ok=True)
    reader = sga.parse_stac_items_to_arrow(items, chunk_size=row_group_size)
    sga.to_parquet(reader, output, schema_version="1.1.0")


def validate(output: Path) -> None:
    import duckdb

    con = duckdb.connect()
    (count,) = con.execute(f"SELECT count(*) FROM read_parquet('{output}')").fetchone()
    bbox_row = con.execute(
        f"SELECT min(bbox.xmin), min(bbox.ymin), max(bbox.xmax), max(bbox.ymax) "
        f"FROM read_parquet('{output}')"
    ).fetchone()
    sample_row = con.execute(
        f"SELECT id, collection, bbox FROM read_parquet('{output}') LIMIT 1"
    ).fetchone()
    log(f"validate: {count} rows")
    log(f"validate: bbox extent (xmin,ymin,xmax,ymax) = {bbox_row}")
    log(f"validate: sample row = {sample_row}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    p.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    p.add_argument("--workers", type=int, default=32)
    p.add_argument("--limit", type=int, default=None,
                   help="only process the first N cells (sorted by id); for smoke tests")
    p.add_argument("--row-group-size", type=int, default=150_000)
    p.add_argument("--quadkey-zoom", type=int, default=12)
    p.add_argument("--force", action="store_true",
                   help="refetch every cached document")
    p.add_argument("--skip-validate", action="store_true")
    args = p.parse_args()

    import requests

    session = requests.Session()
    session.headers["User-Agent"] = "gem-15-catalog/build_items_parquet"

    groups = load_group_catalog_urls(session, args.cache_dir, args.force)
    log(f"{len(groups)} group catalogs")
    specs = enumerate_items(session, groups, args.cache_dir, args.workers, args.force)
    log(f"{len(specs)} cells total")
    if args.limit:
        specs = specs[: args.limit]
        log(f"--limit {args.limit}: processing {len(specs)} cells")

    fetched = fetch_items(session, specs, args.cache_dir, args.workers, args.force)

    items = [transform_item(raw, spec[2]) for spec, raw in fetched]
    items.sort(key=lambda it: quadkey_sort_key(it, args.quadkey_zoom))

    log(f"writing {len(items)} items to {args.output} "
        f"(row_group_size={args.row_group_size})")
    write_parquet(items, args.output, args.row_group_size)
    size = args.output.stat().st_size
    log(f"wrote {args.output} ({size / 1e6:.2f} MB)")

    if not args.skip_validate:
        validate(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
