# Mapterhorn pipeline over GEM-15: agent guide

The claims here are quoted from the experiment's own notes
([NOTES.md](https://github.com/cholmes/mapterhorn/blob/gem-15-source/NOTES.md)
in the fork), from the GEM-15 preprint
([doi:10.31223/X55N75](https://doi.org/10.31223/X55N75)), or measured from the
published archives. Measurements are dated. A fact with no source does not
belong in this file.

## The archives

**This is an experiment, and not a Mapterhorn release.** Three terrarium
terrain archives, built by running GEM-15 elevation COGs through the
[Mapterhorn](https://github.com/mapterhorn/mapterhorn) terrain pipeline from
an unmerged fork: `cholmes/mapterhorn`, branch `gem-15-source`, built at
commit `86d8494`, forked from upstream `main` at `2c4195d`. The Mapterhorn project
did not review, merge or endorse them, and no pull request was opened
upstream. Credit the pipeline as a tool, under its BSD-3 licence, and never
describe these tiles as Mapterhorn data.

The collection publishes no items and no elevation COGs. The elevation source
is the [`gem-15`](../gem-15/collection.json) collection in this catalog, which
is itself a metadata mirror of Google's bucket.

| Asset key | Archive | Band | Max zoom | Tiles | Size |
|---|---|---|---|---|---|
| `terrain-barcelona` | `mapterhorn-gem15-barcelona.pmtiles` | DSM | 12 | 10,931 | 42,064,200 B |
| `terrain-barcelona-dtm` | `mapterhorn-gem15-barcelona-dtm.pmtiles` | DTM | 12 | 10,931 | 37,912,542 B |
| `terrain-cusco` | `mapterhorn-gem15-cusco.pmtiles` | DSM | 13 | 10,929 | 129,375,429 B |
| `terrain-cusco-dtm` | `mapterhorn-gem15-cusco-dtm.pmtiles` | DTM | 13 | 10,929 | 129,224,207 B |
| `terrain-atlas` | `mapterhorn-gem15-atlas.pmtiles` | DSM | 13 | 10,931 | 114,564,655 B |
| `terrain-atlas-dtm` | `mapterhorn-gem15-atlas-dtm.pmtiles` | DTM | 13 | 10,931 | 112,345,297 B |

**An unsuffixed name is the surface model.** The archive file names came from
the build, where the bare-earth band took a `-dtm` suffix and the surface
band took none, so the asset keys follow the file names rather than inventing
a `-dsm` one. Read the band
off the table or off the asset title, never off the absence of a suffix alone.

Each one is terrarium-encoded 512 px lossless WebP, PMTiles specification v3,
clustered, minimum zoom 0. The figures above came from the PMTiles headers,
read over a 127-byte range request each on 2026-11-15. Within an area the two
bands are pixel-aligned and share a maximum zoom, so a client can swap one for
the other without recomputing a tile address.

## Coverage footprints

The footprints below are different sizes, and code that confuses them draws in
the wrong place.

Both bands of an area share a footprint, so each row below covers two
archives.

| Area | PMTiles header says | Tiles present | GEM-15 cells with elevation |
|---|---|---|---|
| Catalonia | the whole world | 0 to 5.625E, 36.598 to 45.089N | `w1s41` `w2s41` `w3s40` `w3s41` |
| Cusco | the whole world | 73.125W to 70.3125W, 16.636S to 11.178S | `wm73sm14` |
| High Atlas | the whole world | 8.4375W to 5.625W, 29.535N to 34.307N | `wm8s31` |

Cell names decode as `w[m]XX s[m]YY`, where `w` gives the west-edge longitude,
`s` the south-edge latitude, a leading `m` means minus, and a cell spans one
degree east and one degree north from that corner. So `wm73sm14` is
`[-73, -14, -72, -13]` and `w2s41` is `[2, 41, 3, 42]`. The same scheme is
documented in [`gem-15/AGENTS.md`](../gem-15/AGENTS.md).

The collection's `extent.spatial.bbox` uses the tile footprints. Its first box
is the overall envelope. The boxes after it follow the table order above.

The cells have one further property. A GEM-15 cell raster is 8192 by 8192
pixels at 15 m, so 122.88 km square, which is a generous square around the
one-degree footprint rather than a tight fit. Elevation extends a little past
each cell box above.

## Runnable access patterns

Read an elevation out of a published archive without downloading it. This
needs `pmtiles` and `Pillow`, and it fetches a few tens of kilobytes:

```python
import io, math, urllib.request
from PIL import Image
from pmtiles.reader import Reader

URL = ("https://data.source.coop/portolan-mirrors/gem-15-catalog/"
       "mapterhorn-gem15/mapterhorn-gem15-atlas.pmtiles")

def ranged(offset, length):
    request = urllib.request.Request(URL, headers={
        "Range": f"bytes={offset}-{offset + length - 1}"})
    with urllib.request.urlopen(request) as response:
        return response.read()

lon, lat, zoom = -7.9147, 31.0606, 13          # Toubkal, this archive's max
n = 2 ** zoom
fx = (lon + 180.0) / 360.0 * n
fy = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
tile = Reader(ranged).get(zoom, int(fx), int(fy))

image = Image.open(io.BytesIO(tile)).convert("RGB")      # 512 x 512 WebP
r, g, b = image.getpixel((int((fx % 1) * 512), int((fy % 1) * 512)))
print((r * 256 + g + b / 256) - 32768)                   # 4108.5
```

That run printed `4108.5` on 2026-11-15. The decode is the inverse of the
pipeline's own terrarium writer:
`elevation = (R * 256 + G + B / 256) − 32768`.

Inspect an archive's header with the `pmtiles` CLI:

```sh
pmtiles show https://data.source.coop/portolan-mirrors/gem-15-catalog/mapterhorn-gem15/mapterhorn-gem15-cusco.pmtiles
```

Expect `tile type: WebP`, `tile compression: none`, `min zoom: 0` and
`max zoom: 13`. Ignore the bounds it prints: see the first quirk below.

## Quirks that produce silently wrong answers

- **The PMTiles header bounds are the whole world, in all three archives.**
  Measured 2026-11-15: 180W to 180E, 85.05S to 85.05N, on files whose tiles
  cover at most a few degrees. The fork's `bundle_area.py` writes those bounds
  and never narrows them. Code that takes those bounds as coverage gets the
  whole world. Use the collection's `extent.spatial.bbox` or the table above
  instead.
- **Tiles are 512 px.** MapLibre needs `"tileSize": 512` on the
  `raster-dem` source. A source left at the 256 default renders terrain at the
  wrong scale, which looks like a plausible map rather than an error. The
  `gem-15` collection's own terrain tiles are 256 px, so a snippet copied from
  there needs this one change.
- **Maximum zoom differs per archive**: 12 for Catalonia, 13 for Cusco and the
  High Atlas. Set `maxzoom` per source. Zoom 12 over Catalonia is a property
  of the pipeline's resolution rule at that latitude, not a truncated build.
- **Terrarium has no nodata value.** Tiles inside an archive but outside its
  GEM-15 cells decode as exactly 0 m, which is a legal elevation. A reading of
  0.0 means "no data here" far more often than it means sea level. The
  pipeline writes a WebP for each tile of an aggregation block, so most of the
  roughly 10,930 tiles in each archive are those flat ones. Tile count is not
  a measure of coverage.
- **Some pixels are Copernicus GLO-30 at 30 m, and none of them are marked.**
  GEM-15 masks about 2.5 % of its cells, water plus a 9 m buffer among them,
  and the fork fills that nodata from GLO-30 before tiling. The Sau reservoir
  reads 403.50 m here and is NaN in GEM-15. A tile gives no way to tell a filled pixel
  from a GEM-15 one, so these archives answer "what does the
  terrain look like" and never "what does GEM-15 measure here". Read the
  `gem-15` item COGs for that.
- **Heights are orthometric on the EGM96 geoid (EPSG:5773)**, not ellipsoidal
  and not EGM2008. GEM-15 enters the pipeline unconverted, which is
  deliberate: Mapterhorn normalizes none of the national vertical datums its
  production sources use, and the offset is an order of magnitude below
  GEM-15's own 3.37 m RMSE. The EGM2008-to-EGM96 conversion runs only inside
  the void fill, where the two datums meet a few pixels apart.
- **The archive metadata credits only the pipeline.** Each archive's PMTiles
  metadata declares one key, `attribution`, set to
  `<a href="https://mapterhorn.com/attribution">© Mapterhorn</a>` (measured
  2026-11-15). That is the tool credit the pipeline writes into everything it
  builds. It names neither Google nor Copernicus, so a client that surfaces
  the archive's own attribution and stops there is under-crediting the data.
  Set the full attribution on the source, which MapLibre prefers over the
  archive metadata, or use one of the styles in `styles/`.
- **The gap between the two bands is not a canopy or building height on
  steep ground.** The DTM reads at or below the DSM at every point measured,
  and the amount is usable on flat ground alone. Each archive is mosaicked and
  resampled on its own, so at a zoom 13 pixel of about
  9.55 m the difference in where the two rasters land inside the pixel can
  exceed the real gap. Measured 2026-11-15 on bare rock, where the true gap
  is near zero either way: 0.75 m at the Toubkal summit against 8.25 m on the
  slope below it. Over the Barcelona Eixample, which is flat, the gap of
  5.00 m is a credible city-block height. The `gem-15` collection records the
  same caveat at source, where the clamp that keeps the DTM at or below the
  DSM holds at full resolution only.
- **Over a filled void the two bands are identical by construction.** The
  Sau reservoir reads 403.50 m in both. Copernicus GLO-30 supplies the fill,
  it is a surface model, and no bare-earth correction is applied to it. A
  zero gap there says nothing about what stands on the ground.
- **The bare-earth band is model output.** GEM-15 predicts its DTM from the
  DSM with a model trained on US airborne lidar, so it is the more
  experimental of the two. Under closed canopy its bias reaches +9.4 m, which
  covers the Cusco archive.
- **These are not a mirror of GEM-15 pixels.** The tiles are resampled,
  void-filled and re-encoded to 8 bits per channel. Treat them as a rendering
  input.

## Styles and related layers

- **Styles.** `styles/atlas.json` (the default), `styles/cusco.json` and
  `styles/barcelona.json` are self-contained MapLibre GL styles, one per
  archive, each with the right `maxzoom`, `"tileSize": 512` and the full
  attribution. They use the same absolute elevation ramp as the `gem-15`
  collection's hypsometric style, so a colour means the same height across
  this catalog. Minimum client is MapLibre GL JS 5.6.0, where the
  `color-relief` layer type arrived. Each one draws the area's surface model.
  To draw the bare-earth band, change the one `url` line to the `-dtm`
  sibling and change nothing else. The collection has no separate DTM style
  on purpose, because the ramp, the hillshade, the tile size and the maximum
  zoom are identical across the two bands.
- **Thumbnail.** `thumbnail.png` is the High Atlas archive decoded at zoom 10
  and shaded with that same ramp, so the catalog card and the default style
  agree. `tools/build_mapterhorn_thumbnail.py` in the catalog repository
  regenerates it from the published tiles.
- **The elevation source.** [`gem-15`](../gem-15/collection.json) holds the
  item index, the COG hrefs into Google's bucket, the band semantics and the
  accuracy tables. Its own terrain PMTiles cover the planet at 256 px tiles
  and are built by a different pipeline, so the two sets of tiles are not
  interchangeable.

## Attribution, which is not optional

Anything published from these archives takes on the obligations below. The
first two come from licences, the last from honesty about provenance.

1. GEM-15, CC-BY-4.0, requires the exact string
   `This dataset is produced by Google`.
2. Copernicus WorldDEM-30, Article 6(b), requires the modified-data notice,
   because the filled void pixels derive from GLO-30:

   ```text
   produced using Copernicus WorldDEM-30 © DLR e.V. 2010-2014 and © Airbus
   Defence and Space GmbH 2014-2018 provided under COPERNICUS by the European
   Union and ESA; all rights reserved
   ```

3. The Mapterhorn pipeline, BSD-3. Say "built with the Mapterhorn pipeline",
   keep the notice, and state that the archives come from an unmerged fork
   that the Mapterhorn project has not endorsed.

## Provenance

The elevation came from Google Research, through the GEM-15 DSM COGs in
Google's public bucket. The tiling came from the Mapterhorn pipeline, run
unmodified from `aggregation_covering.py` onward; the fork adds a GEM-15
download step, a GLO-30 void-fill step and a per-area bundling step.

Portolan Mirrors ran the build on 2026-11-15, published the archives, and
wrote this metadata, and is not affiliated with the Mapterhorn project. The
per-asset `processing:lineage` records the steps for each archive. The
catalog metadata and the thumbnail tool are in the
[catalog repository](https://github.com/portolan-mirrors/gem-15-catalog).
