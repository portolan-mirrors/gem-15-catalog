#!/usr/bin/env python3
"""Refresh the measured facts on ``catalog/gem-15/collection.json``'s assets.

Everything else in that file is hand-authored prose (titles, descriptions,
``processing:lineage``) that this script must never touch. The only things
it owns are the facts that can only be known *after* a build runs: a file's
size, its checksum, and -- for the overview COG specifically -- its
dimensions, affine transform, and per-band statistics.

    python3 tools/update_asset_facts.py             # apply, print a diff
    python3 tools/update_asset_facts.py --dry-run    # print the diff only

Idempotent: run it again right after a successful run and every value is
identical, so no write happens and the diff is empty. Run it again after a
rebuild (new bytes, same path) and only the facts that changed are touched;
``processing:lineage`` and every hand-written field survive untouched, and a
field this script does not know how to compute is simply left alone.

For each asset below, if the file it describes does not exist in
``staging/publish/`` (or, for the thumbnail, in ``catalog/``), it is skipped
with a note -- not an error, since a partial pipeline run is a normal thing
to check facts against.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COLLECTION_PATH = ROOT / "catalog" / "gem-15" / "collection.json"
PUBLISH_DIR = ROOT / "staging" / "publish" / "gem-15"

# Band Object fields STAC 1.1 made core; kept in sync with
# build_items_parquet.py's CORE_BAND_FIELDS so the overview asset's
# synthesized `bands` entries use the same prefixing rule as the items.
CORE_BAND_FIELDS = {"name", "description", "data_type", "nodata", "unit", "statistics"}

THUMBNAIL_DEFAULT = {
    "href": "./thumbnail.png",
    "type": "image/png",
    "title": "Thumbnail",
    "description": (
        "An 800x533 preview over the Barcelona demo area, rendered the same "
        "way as the overview COG (cyclic 100 m palette x hillshade) at "
        "about 90 m/px."
    ),
    "roles": ["thumbnail"],
}


def log(msg: str) -> None:
    print(msg, flush=True)


def sha256_multihash(path: Path) -> str:
    """The multihash hex string for sha2-256: 0x12 (code) 0x20 (32 bytes),
    i.e. the literal hex prefix "1220", followed by the sha256 hex digest."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return "1220" + digest.hexdigest()


def overview_facts(path: Path) -> dict:
    """proj:shape, proj:transform, and per-band statistics for the COG."""
    import rasterio

    with rasterio.open(path) as ds:
        shape = list(ds.shape)  # [height, width], matching upstream's own proj:shape
        transform = list(ds.transform)[:6]
        names = ["red", "green", "blue", "alpha"]
        bands = []
        for i, stats in enumerate(ds.stats(approx=True)):
            bands.append({
                "name": names[i] if i < len(names) else f"band_{i + 1}",
                "data_type": ds.dtypes[i],
                "statistics": {
                    "minimum": stats.min,
                    "maximum": stats.max,
                    "mean": stats.mean,
                    "stddev": stats.std,
                },
            })
    return {"proj:shape": shape, "proj:transform": transform, "bands": bands}


def diff_set(asset: dict, key: str, value, changes: list[str], label: str) -> None:
    old = asset.get(key)
    if old == value:
        return
    asset[key] = value
    if old is None:
        changes.append(f"  {label}: {key}: (absent) -> set")
    else:
        changes.append(f"  {label}: {key}: changed")


def lineage_sidecar(path: Path) -> str | None:
    sidecar = path.with_suffix(".lineage.txt")
    if sidecar.exists():
        return sidecar.read_text().strip()
    return None


ASSET_SPECS = [
    {
        "key": "geoparquet-items",
        "path": PUBLISH_DIR / "items.parquet",
    },
    {
        "key": "overview",
        "path": PUBLISH_DIR / "gem-15_overview.tif",
        "extra": overview_facts,
    },
    {
        "key": "terrain-dsm",
        "path": PUBLISH_DIR / "terrain" / "gem-15-dsm-terrarium.pmtiles",
    },
    {
        "key": "terrain-dsm-demo",
        "path": PUBLISH_DIR / "terrain" / "gem-15-dsm-terrarium-demo-barcelona.pmtiles",
    },
    {
        "key": "thumbnail",
        "path": ROOT / "catalog" / "gem-15" / "thumbnail.png",
        "default": THUMBNAIL_DEFAULT,
    },
]


def update_collection(collection: dict, dry_run: bool) -> list[str]:
    assets = collection.setdefault("assets", {})
    changes: list[str] = []

    for spec in ASSET_SPECS:
        key = spec["key"]
        path: Path = spec["path"]
        if not path.exists():
            log(f"skip {key}: {path} not found")
            continue

        is_new = key not in assets
        if is_new:
            default = spec.get("default")
            if default is None:
                log(f"skip {key}: not present in collection.json assets and "
                    f"no default to add (unexpected -- was the asset key "
                    f"renamed?)")
                continue
            assets[key] = dict(default)
            changes.append(f"  {key}: asset added")
        asset = assets[key]

        size = path.stat().st_size
        checksum = sha256_multihash(path)
        diff_set(asset, "file:size", size, changes, key)
        diff_set(asset, "file:checksum", checksum, changes, key)

        if "extra" in spec:
            extra = spec["extra"](path)
            for field, value in extra.items():
                diff_set(asset, field, value, changes, key)

        if "processing:lineage" not in asset:
            lineage = lineage_sidecar(path)
            if lineage:
                asset["processing:lineage"] = lineage
                changes.append(f"  {key}: processing:lineage: (absent) -> set from sidecar")

    return changes


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--collection", type=Path, default=COLLECTION_PATH)
    p.add_argument("--dry-run", action="store_true",
                   help="print the diff but do not write collection.json")
    args = p.parse_args()

    if not args.collection.exists():
        sys.exit(f"{args.collection} does not exist")

    original_text = args.collection.read_text()
    collection = json.loads(original_text)

    changes = update_collection(collection, args.dry_run)

    if not changes:
        log("no changes; collection.json already reflects the staged files")
        return 0

    log(f"{len(changes)} change(s):")
    for line in changes:
        log(line)

    if args.dry_run:
        log("\n--dry-run set; collection.json not written")
        return 0

    new_text = json.dumps(collection, indent=2) + "\n"
    if new_text == original_text:
        log("no byte-level change; not rewriting collection.json")
        return 0
    args.collection.write_text(new_text)
    log(f"wrote {args.collection}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
