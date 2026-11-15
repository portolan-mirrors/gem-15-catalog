#!/usr/bin/env python3
"""Render ``catalog/mapterhorn-gem15/thumbnail.png`` from a published archive.

The thumbnail for the ``mapterhorn-gem15`` collection is rendered from the
collection's own published terrarium tiles, not from the GEM-15 COGs, so the
picture shows the bytes the collection actually serves. Tiles arrive over HTTP
range requests through the PMTiles directory, so a thumbnail costs a few dozen
tiles rather than a whole archive.

Like ``build_thumbnail.py``, this writes a small PNG straight into
``catalog/`` rather than into ``staging/publish/``. A PNG of this size is not
a GeoParquet, COG or PMTiles file, so AGENTS.md's "data never enters git" rule
is not in play.

    python3 tools/build_mapterhorn_thumbnail.py              # the real file
    python3 tools/build_mapterhorn_thumbnail.py --probe       # coverage only
    python3 tools/build_mapterhorn_thumbnail.py --area cusco --zoom 9 \
        --output staging/smoke/cusco.png

``--probe`` reads one coarse zoom and reports how much of the frame carries
elevation above sea level, which is the smoke test: it answers "is this frame
pointed at the data" without rendering anything.

The shading is an absolute hypsometric tint multiplied by a hillshade. It is
deliberately not the cyclic 100 m palette that ``build_overview.py`` uses for
GEM-15 itself: that palette reproduces Google's own Earth Engine example, and
these archives are a terrain-tile product rather than a GEM-15 rendering. The
ramp below is the one the catalog's hypsometric styles already use, so the
card and the collection's default style show the same colours.
"""
from __future__ import annotations

import argparse
import io
import math
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PUBLIC_BASE = ("https://data.source.coop/portolan-mirrors/gem-15-catalog/"
               "mapterhorn-gem15")

THUMB_W, THUMB_H = 800, 533  # 3:2, the Portolan catalog card shape
EARTH_R = 6378137.0
# Mapterhorn writes 512 px tiles, so one tile pixel is half the usual size.
TILE_PX = 512

# Centre and half-height per area. The High Atlas is the default: it has
# 3,303 m of relief inside one cell and almost no canopy, so the hillshade
# reads as terrain rather than as vegetation (NOTES.md section 4).
AREAS = {
    "atlas": {
        "archive": "mapterhorn-gem15-atlas.pmtiles",
        "maxzoom": 13,
        "center": (-7.50, 31.28),
        "half_height_deg": 0.30,
    },
    "cusco": {
        "archive": "mapterhorn-gem15-cusco.pmtiles",
        "maxzoom": 13,
        "center": (-72.50, -13.40),
        "half_height_deg": 0.30,
    },
    "barcelona": {
        "archive": "mapterhorn-gem15-barcelona.pmtiles",
        "maxzoom": 12,
        "center": (2.165, 41.600),
        "half_height_deg": 0.40,
    },
}

# Absolute hypsometric ramp, metres above the EGM96 geoid, low to high. These
# are the stops of this catalog's hypsometric styles, verbatim, so the
# thumbnail and the styles in catalog/mapterhorn-gem15/styles/ agree. Changing
# one without the other makes the catalog card lie about the default style.
RAMP_HEX = [
    (-430, "#0f3d2e"), (-5, "#1d5c42"), (0, "#5d7f93"), (1, "#2d6a3f"),
    (100, "#4e8c4a"), (300, "#7aa85a"), (600, "#a8c06a"), (900, "#cfcf7d"),
    (1200, "#e2cf8c"), (1600, "#d8b074"), (2000, "#c0905c"), (2400, "#a4744a"),
    (2800, "#8a5c3e"), (3200, "#745038"), (3600, "#6d5b50"), (4200, "#8a7d74"),
    (4800, "#a99d95"), (5400, "#c9c0ba"), (6000, "#e6e2df"), (6800, "#ffffff"),
    (8849, "#ffffff"),
]
RAMP = [(stop, (int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)))
        for stop, h in RAMP_HEX]


def mercator_y(lat_deg: float) -> float:
    lat_rad = math.radians(lat_deg)
    return EARTH_R * math.log(math.tan(math.pi / 4 + lat_rad / 2))


def frame_bbox(center_lon: float, center_lat: float, half_height_deg: float):
    """(west, south, east, north) in degrees, close to 3:2 once projected."""
    lat0 = center_lat - half_height_deg
    lat1 = center_lat + half_height_deg
    height_m = mercator_y(lat1) - mercator_y(lat0)
    width_m = height_m * (THUMB_W / THUMB_H)
    meters_per_degree_lon = EARTH_R * math.pi / 180.0  # constant in EPSG:3857
    half_width_deg = (width_m / 2) / meters_per_degree_lon
    return (center_lon - half_width_deg, lat0,
            center_lon + half_width_deg, lat1)


def tile_xy_float(lon: float, lat: float, zoom: int):
    n = 2 ** zoom
    x = (lon + 180.0) / 360.0 * n
    y = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    return x, y


def pick_zoom(bbox, maxzoom: int) -> int:
    """The shallowest zoom whose mosaic is at least as wide as the output."""
    west, _, east, _ = bbox
    for zoom in range(0, maxzoom + 1):
        width_px = (tile_xy_float(east, 0.0, zoom)[0]
                    - tile_xy_float(west, 0.0, zoom)[0]) * TILE_PX
        if width_px >= THUMB_W:
            return zoom
    return maxzoom


def range_reader(url: str, counter: dict):
    def get_bytes(offset: int, length: int) -> bytes:
        counter["requests"] += 1
        counter["bytes"] += length
        request = urllib.request.Request(url, headers={
            "Range": f"bytes={offset}-{offset + length - 1}",
            "User-Agent": "portolan-gem15-catalog-thumbnail",
        })
        with urllib.request.urlopen(request) as response:
            return response.read()
    return get_bytes


def read_elevation(source, zoom: int, bbox):
    """Decode a terrarium mosaic over bbox into a float array of metres.

    Returns (elevation, missing), where missing marks pixels no tile covered.
    """
    import numpy as np
    from PIL import Image
    from pmtiles.reader import Reader

    west, south, east, north = bbox
    x0f, y0f = tile_xy_float(west, north, zoom)
    x1f, y1f = tile_xy_float(east, south, zoom)
    tx0, ty0 = math.floor(x0f), math.floor(y0f)
    tx1, ty1 = math.floor(x1f), math.floor(y1f)

    width = (tx1 - tx0 + 1) * TILE_PX
    height = (ty1 - ty0 + 1) * TILE_PX
    mosaic = np.zeros((height, width, 3), dtype=np.uint8)
    missing = np.ones((height, width), dtype=bool)

    reader = Reader(source)
    for ty in range(ty0, ty1 + 1):
        for tx in range(tx0, tx1 + 1):
            raw = reader.get(zoom, tx, ty)
            if raw is None:
                continue
            tile = np.asarray(Image.open(io.BytesIO(raw)).convert("RGB"))
            if tile.shape[0] != TILE_PX or tile.shape[1] != TILE_PX:
                raise SystemExit(
                    f"tile {zoom}/{tx}/{ty} is {tile.shape[1]}x{tile.shape[0]} "
                    f"px, not {TILE_PX}x{TILE_PX}; the mosaic maths assumes "
                    f"{TILE_PX} px tiles"
                )
            row = (ty - ty0) * TILE_PX
            col = (tx - tx0) * TILE_PX
            mosaic[row:row + TILE_PX, col:col + TILE_PX] = tile
            missing[row:row + TILE_PX, col:col + TILE_PX] = False

    # Crop the mosaic to the frame before decoding, so the arrays stay small.
    top = int(round((y0f - ty0) * TILE_PX))
    left = int(round((x0f - tx0) * TILE_PX))
    bottom = int(round((y1f - ty0) * TILE_PX))
    right = int(round((x1f - tx0) * TILE_PX))
    mosaic = mosaic[top:bottom, left:right]
    missing = missing[top:bottom, left:right]

    rgb = mosaic.astype(np.float32)
    elevation = (rgb[:, :, 0] * 256.0 + rgb[:, :, 1]
                 + rgb[:, :, 2] / 256.0) - 32768.0
    return elevation, missing


def hillshade(elevation, pixel_m: float, exaggeration: float = 1.6):
    """Horn's method, 335 degrees azimuth, 45 degrees altitude."""
    import numpy as np

    dy, dx = np.gradient(elevation * exaggeration, pixel_m)
    slope = np.arctan(np.hypot(dx, dy))
    aspect = np.arctan2(-dx, dy)
    azimuth = math.radians(360.0 - 335.0 + 90.0)
    altitude = math.radians(45.0)
    shade = (math.sin(altitude) * np.cos(slope)
             + math.cos(altitude) * np.sin(slope) * np.cos(azimuth - aspect))
    return np.clip(shade, 0.0, 1.0)


def hypsometric(elevation):
    import numpy as np

    stops = np.array([stop for stop, _ in RAMP], dtype=np.float32)
    colors = np.array([color for _, color in RAMP], dtype=np.float32)
    flat = np.clip(elevation, stops[0], stops[-1]).ravel()
    out = np.empty((flat.size, 3), dtype=np.float32)
    for channel in range(3):
        out[:, channel] = np.interp(flat, stops, colors[:, channel])
    return out.reshape(elevation.shape + (3,))


def render(area: str, zoom: int | None, output: Path, probe: bool) -> int:
    import numpy as np
    from PIL import Image

    spec = AREAS[area]
    bbox = frame_bbox(*spec["center"], spec["half_height_deg"])
    if zoom is None:
        zoom = pick_zoom(bbox, spec["maxzoom"])
    if zoom > spec["maxzoom"]:
        raise SystemExit(
            f"zoom {zoom} is deeper than the {area} archive's maximum "
            f"({spec['maxzoom']})"
        )

    url = f"{PUBLIC_BASE}/{spec['archive']}"
    counter = {"requests": 0, "bytes": 0}
    print(f"area {area}  zoom {zoom}  bbox "
          + " ".join(f"{v:.4f}" for v in bbox))
    print(f"source {url}")

    elevation, missing = read_elevation(range_reader(url, counter), zoom, bbox)
    print(f"read {counter['requests']} range request(s), "
          f"{counter['bytes']:,} bytes, mosaic {elevation.shape[1]}x"
          f"{elevation.shape[0]} px")

    covered = ~missing
    above_sea = covered & (elevation > 1.0)
    share = 100.0 * above_sea.sum() / above_sea.size
    print(f"frame carries elevation above 1 m over {share:.1f} % of pixels; "
          f"range {elevation[covered].min():.1f} to "
          f"{elevation[covered].max():.1f} m")
    if share < 50.0:
        print("warning  less than half the frame is above sea level: either "
              "the frame is off the data or the area really is that flat")

    if probe:
        return 0

    center_lat = spec["center"][1]
    pixel_m = (2 * math.pi * EARTH_R * math.cos(math.radians(center_lat))
               / (TILE_PX * 2 ** zoom))
    print(f"pixel size at the frame centre: {pixel_m:.2f} m")

    tint = hypsometric(elevation)
    shade = hillshade(elevation, pixel_m)[:, :, None]
    # Lift the shading off pure black so the tint stays readable in shadow.
    rgb = tint * (0.35 + 0.65 * shade)
    image = Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8), "RGB")
    image = image.resize((THUMB_W, THUMB_H), Image.LANCZOS)
    # A 256-colour palette halves the committed file with no visible change:
    # a tint-times-shade image holds few distinct hues. This PNG goes into
    # git, so the saving is worth the one extra step.
    image = image.quantize(colors=256, method=Image.MEDIANCUT,
                           dither=Image.FLOYDSTEINBERG)

    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output, "PNG", optimize=True)
    print(f"wrote {output} ({output.stat().st_size:,} bytes, "
          f"{THUMB_W}x{THUMB_H})")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--area", choices=sorted(AREAS), default="atlas")
    parser.add_argument("--zoom", type=int, default=None,
                        help="override the chosen zoom (smoke test: use 8-9)")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--probe", action="store_true",
                        help="report frame coverage and render nothing")
    args = parser.parse_args()

    output = args.output
    if output is None:
        output = ROOT / "catalog" / "mapterhorn-gem15" / "thumbnail.png"
    return render(args.area, args.zoom, output, args.probe)


if __name__ == "__main__":
    sys.exit(main())
