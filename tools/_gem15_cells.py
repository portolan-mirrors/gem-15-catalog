"""Shared cell discovery for the GEM-15 derivative builders.

Every builder (`build_items_parquet.py`, `build_overview.py`,
`build_terrain.py`) needs the same thing first: the list of 13,485 cells,
each one's 1x1-degree bbox, and the URL of its DSM/DTM mosaic COG. This
module gets that list with the cheapest possible crawl of Google's own STAC
-- collection.json's 297 "child" links to group catalogs, each group
catalog's "item" links to cells -- without fetching any of the 13,485 per-item
JSON documents (those are only needed by build_items_parquet.py, which reads
them itself). A cell's bbox and mosaic URLs are derived from its id and group
id by the naming rule in DESIGN.md, not fetched.

Cell id format ``w[m]XXs[m]YY``: the WEST and SOUTH edge of the 1x1-degree
cell, ``m`` meaning "minus". ``wm91sm1`` -> bbox (-91, -1, -90, 0).
"""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

COLLECTION_URL = (
    "https://storage.googleapis.com/climate-and-sustainability/gem-15/"
    "stac/gem-15/collection.json"
)
BUCKET_BASE = "https://storage.googleapis.com/climate-and-sustainability/gem-15"

_CELL_RE = re.compile(r"^w(m?)(\d+)s(m?)(\d+)$")


@dataclass(frozen=True)
class Cell:
    group_id: str
    cell_id: str
    bbox: tuple[float, float, float, float]  # west, south, east, north

    @property
    def dsm_url(self) -> str:
        return mosaic_url(self.group_id, self.cell_id, "dsm")

    @property
    def dtm_url(self) -> str:
        return mosaic_url(self.group_id, self.cell_id, "dtm")

    @property
    def centroid(self) -> tuple[float, float]:
        w, s, e, n = self.bbox
        return ((w + e) / 2.0, (s + n) / 2.0)


def mosaic_url(group_id: str, cell_id: str, band: str) -> str:
    return f"{BUCKET_BASE}/{group_id}/{cell_id}/{band}/zall/mosaic.tif"


def cell_bbox(cell_id: str) -> tuple[float, float, float, float]:
    """The (west, south, east, north) bbox implied by a cell id."""
    m = _CELL_RE.match(cell_id)
    if not m:
        raise ValueError(f"not a GEM-15 cell id: {cell_id!r}")
    w_minus, w_num, s_minus, s_num = m.groups()
    west = -int(w_num) if w_minus else int(w_num)
    south = -int(s_num) if s_minus else int(s_num)
    return (float(west), float(south), float(west) + 1.0, float(south) + 1.0)


def bbox_intersects(a, b) -> bool:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    return ax0 < bx1 and bx0 < ax1 and ay0 < by1 and by0 < ay1


def _fetch_json(session, url: str, cache_path: Path | None, force: bool) -> dict:
    if cache_path is not None and not force and cache_path.exists():
        try:
            return json.loads(cache_path.read_text())
        except json.JSONDecodeError:
            pass
    last_exc = None
    for attempt in range(4):
        try:
            resp = session.get(url, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            break
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            time.sleep(min(2.0 * (attempt + 1), 10.0))
    else:
        raise RuntimeError(f"failed to fetch {url}: {last_exc}") from last_exc
    if cache_path is not None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(data))
    return data


def discover_cells(
    cache_dir: Path,
    workers: int = 32,
    force: bool = False,
    log=print,
) -> list[Cell]:
    """All 13,485 cells, via collection.json -> 297 group catalogs.

    Group and item catalogs are cached under ``cache_dir`` (shared with
    ``build_items_parquet.py``'s cache, so running one builder warms the
    other). No per-item JSON is fetched here.
    """
    import requests

    session = requests.Session()
    session.headers["User-Agent"] = "gem-15-catalog/_gem15_cells"

    coll = _fetch_json(session, COLLECTION_URL, cache_dir / "collection.json", force)
    group_urls = []
    for link in coll.get("links", []):
        if link.get("rel") != "child":
            continue
        url = urljoin(COLLECTION_URL, link["href"])
        group_id = url.rstrip("/").split("/")[-2]
        group_urls.append((group_id, url))
    if not group_urls:
        raise RuntimeError("collection.json carried no child links")
    group_urls.sort()

    cells: list[Cell] = []

    def one(group_id: str, url: str) -> list[Cell]:
        cache_path = cache_dir / "groups" / f"{group_id}.json"
        catalog = _fetch_json(session, url, cache_path, force)
        out = []
        for link in catalog.get("links", []):
            if link.get("rel") != "item":
                continue
            cell_id = link.get("title") or link["href"].rstrip("/").split("/")[-2]
            out.append(Cell(group_id, cell_id, cell_bbox(cell_id)))
        return out

    log(f"discovering cells: fetching {len(group_urls)} group catalogs...")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(one, g, u): g for g, u in group_urls}
        done = 0
        for fut in as_completed(futures):
            cells.extend(fut.result())
            done += 1
            if done % 100 == 0 or done == len(group_urls):
                log(f"  {done}/{len(group_urls)} group catalogs")
    cells.sort(key=lambda c: (c.group_id, c.cell_id))
    log(f"discovered {len(cells)} cells")
    return cells


def filter_cells(
    cells: list[Cell],
    cell_ids: list[str] | None = None,
    bbox: tuple[float, float, float, float] | None = None,
) -> list[Cell]:
    out = cells
    if cell_ids:
        wanted = set(cell_ids)
        out = [c for c in out if c.cell_id in wanted]
    if bbox:
        out = [c for c in out if bbox_intersects(c.bbox, bbox)]
    return out
