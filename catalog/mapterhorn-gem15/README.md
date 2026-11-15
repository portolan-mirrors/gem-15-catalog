# Mapterhorn pipeline over GEM-15: three test areas

**An experiment, and not a Mapterhorn release.** Three terrarium-encoded
terrain archives, built by running GEM-15 elevation data through the
[Mapterhorn](https://mapterhorn.com/) terrain pipeline from an **unmerged
fork**. The Mapterhorn project did not review, merge or endorse any of this,
and no pull request was opened upstream. Agents and automated clients should
read [AGENTS.md](AGENTS.md) instead of this page.

Mapterhorn issue
[#368](https://github.com/mapterhorn/mapterhorn/issues/368), "Thoughts on
Google's GEM-15 being used as the baseline model?", records a maintainer
suggesting that somebody "run the Mapterhorn terrain pipeline with some of the
google tiffs". This collection is that run. The fork is
[cholmes/mapterhorn, branch `gem-15-source`](https://github.com/cholmes/mapterhorn/tree/gem-15-source),
built at commit `86d8494`, forked from upstream `main` at `2c4195d`. Its
[NOTES.md](https://github.com/cholmes/mapterhorn/blob/gem-15-source/NOTES.md)
records the pipeline mechanics, the decisions and the measurements behind
everything on this page.

The question the areas answer is narrow: what does a global 15 m optical
source add to a terrain archive whose deepest zoom comes from a resolution
rule? The answer turned out to be "one zoom level, and only below about 38
degrees of latitude".

A [live comparison](https://portolan-mirrors.github.io/gem-15-catalog/maplibre-terrain/mapterhorn-gem15.html)
draws the three archives in the browser.

## The archives

Built 2026-11-15 on a cluster, 28 m 07 s of wall clock for all of them.

| Area | GEM-15 cells | Source CRS | Max zoom | Tiles | Size | Build |
|---|---|---|---|---|---|---|
| Catalonia | 4: `w1s41` `w2s41` `w3s40` `w3s41` | EPSG:32631 | 12 | 10,931 | 42.1 MB | 9 m 13 s |
| Cusco, Peru | 1: `wm73sm14` | EPSG:32718 | 13 | 10,929 | 129.4 MB | 8 m 36 s |
| High Atlas, Morocco | 1: `wm8s31` | EPSG:32629 | 13 | 10,931 | 114.6 MB | 10 m 18 s |

The archives use Mapzen terrarium encoding in 512 px lossless WebP tiles,
PMTiles specification v3, clustered, from zoom 0 to the maximum above. They
are separate files with no overlap, so a map picks one.

The tile counts barely differ because the pipeline writes a WebP for each tile
in an aggregation block, including the flat 0 m tiles outside the source
cells. Byte size tracks the extra zoom level and the roughness of the ground
instead: a one-cell archive at zoom 13 over the Andes is three times the size
of a four-cell archive at zoom 12 over Catalonia.

Catalonia overlaps this catalog's own
[GEM-15 terrain demo](../gem-15/README.md), so the two can be compared
directly. Cusco and the High Atlas were picked because Mapterhorn production
has no source finer than 30 m Copernicus GLO-30 in either place. NOTES.md
verifies that against three of Mapterhorn's own published files: the coverage
index, the archive manifest and the attribution list. Reading the coverage
index again on 2026-11-15 returned `glo30` alone at both points, and `es2a`
(2 m) with `es5a` and `es5c` (5 m) alongside `glo30` over Barcelona,
Montserrat and the Sau reservoir.

## The maxzoom result

This is the substantive finding, and it is about the pipeline rather than
about GEM-15.

Mapterhorn's `get_smallest_overzoom` picks the first zoom whose 512 px Web
Mercator tile pixel fully samples the source, then clamps up to zoom 12. A
15 m ground pixel at latitude *phi* occupies 15 / cos(*phi*) metres of
EPSG:3857, and the zoom 12 tile pixel is 19.11 m. So a 15 m source only
crosses into zoom 13 where 15 / cos(*phi*) is more than 19.11 m, which means
**below about 38.3 degrees of latitude**.

| Area | Latitude | Source pixel in EPSG:3857 | GEM-15 max zoom | GLO-30 max zoom there |
|---|---|---|---|---|
| Catalonia | about 41°N | 19.87 to 20.42 m | 12 | 12, clamped up from 11 |
| Cusco | about 13°S | 15.57 to 15.67 m | 13 | 12 |
| High Atlas | about 31°N | 17.82 to 17.90 m | 13 | 12 |

- **Over Catalonia, GEM-15 buys no extra zoom level.** The rule puts a 15 m
  source and a 30 m source at the same zoom 12. The tiles are better, because
  a 14.3 m ground pixel at that latitude resolves a 15 m source rather than a
  30 m one, but the archive is no deeper.
- **Over Cusco and the High Atlas, GEM-15 adds exactly one level.** Both
  archives go to zoom 13 where Mapterhorn publishes zoom 12 today.
- **Depth equivalent to zoom 14 is unreachable for a 15 m source at any
  latitude** under this rule, because that needs 15 / cos(*phi*) above
  4.78 m. Any expectation that GEM-15 arrives "around zoom 14" is wrong.

NOTES.md section 9 puts the conclusion upstream. A global 15 m baseline is
worth little above latitude 38 while the rule stands, so the rule is the thing
to look at. That is a maintainer decision rather than something a source
contribution should work around.

## Probe readings

These points were decoded straight out of the published archives on
2026-11-15, by inverting the terrarium encoding,
`elevation = (R * 256 + G + B / 256) − 32768`.

| Point | Reading | What it tests |
|---|---|---|
| Sau reservoir, 2.400E 41.970N | **403.50 m** | GEM-15 masks this reservoir as NaN. A build without the void fill reads 0 m here. |
| Montserrat, 1.838E 41.593N | **702.00 m** | Ordinary GEM-15 terrain, expected near 700 m. |
| Cusco, 72.54W 13.16S | **1973.75 m** | 3.4 m from the GLO-30 reference at the same point, adjusted to EGM96 (1977.19 m). |
| Toubkal, 7.92W 31.06N | **3853.75 m** | 4.3 m from the GLO-30 reference, adjusted to EGM96 (3849.49 m). |
| Toubkal summit, 7.9147W 31.0606N | **4108.50 m** | The true summit is 4,167 m, and GLO-30 reads 4,158 m. See the caveat below. |

The Sau reading is the one that matters. GEM-15 masks water plus a 9 m
buffer, so the reservoir is NaN in the source, and the pipeline's own
nodata handling would turn that into a flat 0 m pit at the bottom of a
valley. A reading of 403.50 m shows that the Copernicus GLO-30 void fill ran
in the bytes that were published.

The two GLO-30 comparisons at Cusco and at the 7.92W probe point are a sanity
check rather than an accuracy measurement. They compare a 15 m surface model
against a 30 m one over steep ground, where a few metres of disagreement on a
slope is the expected result.

Read the summit figure with care. A zoom 13 pixel is about 8 m across on the
ground at that latitude, and a pyramidal summit occupies less than that, so
the pixel samples the upper slope instead of the pinnacle. The 58 m shortfall
against the true height measures tile sampling, not the accuracy of GEM-15.

## Using the tiles in MapLibre

Each archive is a `raster-dem` source. Set `tileSize` to 512, because
Mapterhorn writes 512 px tiles rather than the 256 px the rest of this catalog
uses, and set `maxzoom` to the value for that archive.

```js
import maplibregl from 'maplibre-gl';
import { Protocol } from 'pmtiles';

maplibregl.addProtocol('pmtiles', new Protocol().tile);

const BASE = 'https://data.source.coop/portolan-mirrors/gem-15-catalog/mapterhorn-gem15';

const map = new maplibregl.Map({
  container: 'map',
  center: [-7.9147, 31.0606],   // Toubkal
  zoom: 11,
  pitch: 60,
  style: {
    version: 8,
    sources: {
      atlas: {
        type: 'raster-dem',
        url: `pmtiles://${BASE}/mapterhorn-gem15-atlas.pmtiles`,
        encoding: 'terrarium',
        tileSize: 512,
        minzoom: 0,
        maxzoom: 13,            // 12 for the Catalonia archive
        attribution:
          'This dataset is produced by Google | CC-BY-4.0 | produced using ' +
          'Copernicus WorldDEM-30 © DLR e.V. 2010-2014 and © Airbus Defence ' +
          'and Space GmbH 2014-2018 provided under COPERNICUS by the ' +
          'European Union and ESA; all rights reserved | built with the ' +
          'Mapterhorn pipeline (BSD-3), from an unmerged fork',
      },
    },
    layers: [{ id: 'hillshade', type: 'hillshade', source: 'atlas' }],
    terrain: { source: 'atlas', exaggeration: 1.5 },
  },
});
```

Set `attribution` on the source as that snippet does. The archives declare
only the pipeline's own `© Mapterhorn` credit in their PMTiles metadata
(measured 2026-11-15), which credits the tool and says nothing about where the
elevation came from. MapLibre prefers a source's own `attribution` over the
archive metadata, so the explicit string is what the map shows.

The styles in `styles/` do all of this already: `atlas.json` (the default),
`cusco.json` and `barcelona.json`. Each one is a self-contained MapLibre GL
style with an absolute hypsometric tint under a hillshade, the right `maxzoom`
for its archive, and the full attribution. They use the same elevation ramp as
the `gem-15` collection's hypsometric style, so a colour means the same height
across this catalog.

## Known issues

- **The PMTiles header bounds are wrong.** Each archive declares the whole
  world, 180W to 180E and 85.05S to 85.05N. That
  comes from the fork's `bundle_area.py` and describes nothing about
  coverage. The tiles present in each archive span the tile-aligned
  aggregation blocks below, and real GEM-15 elevation exists only over the
  one-degree cells inside them. A client that reads the header bounds to
  decide where to draw will draw everywhere.

  | Archive | Tiles present | Cells with elevation |
  |---|---|---|
  | Catalonia | 0 to 5.625E, 36.598 to 45.089N | `[1,41,2,42]` `[2,41,3,42]` `[3,40,4,41]` `[3,41,4,42]` |
  | Cusco | 73.125W to 70.3125W, 16.636S to 11.178S | `[-73,-14,-72,-13]` |
  | High Atlas | 8.4375W to 5.625W, 29.535N to 34.307N | `[-8,31,-7,32]` |

- **Catalonia gains no depth.** Zoom 12 is what Mapterhorn already produces
  there from GLO-30, and its production aggregation over Catalonia includes
  the Spanish national models at 2 m and 5 m (read from the coverage index on
  2026-11-15). GEM-15 does not compete where national lidar exists. That
  archive is a comparison baseline.
- **Experimental, and frozen.** These archives are a point measurement from
  2026-11-15. They will not follow upstream Mapterhorn, and they will not be
  rebuilt when Google releases a new GEM-15 version.
- **Filled pixels are unmarked.** GEM-15's voids are filled from Copernicus
  GLO-30 at 30 m before encoding, and a client cannot tell a filled pixel from
  a GEM-15 one. Draw with these archives and measure with the
  [`gem-15`](../gem-15/collection.json) item COGs.
- **Terrarium has no nodata value**, so each tile outside the source cells
  decodes as 0 m and renders as a flat plain at sea level.
- **Surface model only.** The fork wires up the GEM-15 DSM alone. The DTM
  needs no band extraction and the same void mask applies, so it would be a
  small addition, and nobody has run it.
- **The fork is not ready for upstream.** Its source-catalog entries have a
  `LICENSE.md` where the Mapterhorn source catalog specifies a `LICENSE.pdf`,
  and a global ingest would need roughly 60 sources, one per UTM zone, and
  about 12 TB of transfer for the DSM alone. NOTES.md section 9 lists what
  else is missing.
- **Heights are on EGM96.** GEM-15 enters the pipeline on the EGM96 geoid
  without conversion, which is a deliberate choice: Mapterhorn normalizes none
  of the dozen national vertical datums its production sources use. The geoid
  conversion happens only inside the void fill, where EGM2008 GLO-30 data
  meets EGM96 GEM-15 data a few pixels apart.

## License and attribution

[CC-BY-4.0](https://creativecommons.org/licenses/by/4.0/) for the elevation
data. Anything published from these archives needs the notices below, which
are obligations rather than courtesies.

GEM-15 requires this exact attribution:

> This dataset is produced by Google

The filled void pixels derive from Copernicus WorldDEM-30, so its licence,
Article 6(b), requires the modified-data notice, verbatim:

```text
produced using Copernicus WorldDEM-30 © DLR e.V. 2010-2014 and © Airbus
Defence and Space GmbH 2014-2018 provided under COPERNICUS by the European
Union and ESA; all rights reserved
```

The tiles were produced by the Mapterhorn pipeline, which is BSD-3-licensed
code at [github.com/mapterhorn/mapterhorn](https://github.com/mapterhorn/mapterhorn),
run from an unmerged fork. Credit it as a tool. Say "built with the Mapterhorn
pipeline" rather than anything that implies this is Mapterhorn data, and keep
the BSD-3 notice. State that the Mapterhorn project has not endorsed these
archives.

Cite the elevation data as:

```text
Batchu, V. V., Neumann, M., Li, T., Allen, B., Zhang, J., & Van Arsdale, C.
(2026). GEM-15: Global surface and terrain elevation models (15 meters) from
sub-meter optical stereo imagery. EarthArXiv [preprint].
https://eartharxiv.org/repository/view/16034/, doi:10.31223/X55N75
```

## Files

| File | What it is |
|---|---|
| `collection.json` | This collection, with the archives as assets |
| `mapterhorn-gem15-barcelona.pmtiles` | Catalonia, zoom 0 to 12, 42.1 MB |
| `mapterhorn-gem15-cusco.pmtiles` | Cusco, zoom 0 to 13, 129.4 MB |
| `mapterhorn-gem15-atlas.pmtiles` | High Atlas, zoom 0 to 13, 114.6 MB |
| `styles/*.json` | One MapLibre GL style per archive |
| `thumbnail.png` | The High Atlas, decoded from its own archive |
| `AGENTS.md`, `llms.txt` | Guidance for agents and language models |

Browse the collection in the
[data browser](https://source.coop/portolan-mirrors/gem-15-catalog/mapterhorn-gem15).

## Provenance

Google Research produced GEM-15 and publishes the elevation COGs these tiles
were built from; the [`gem-15`](../gem-15/README.md) collection in this
catalog documents them. The Mapterhorn project wrote the terrain pipeline and
publishes it under BSD-3. Portolan Mirrors ran the forked pipeline, published
the archives, and wrote this metadata, and is not affiliated with the
Mapterhorn project.

The archives were produced on 2026-11-15 by the fork's own scripts: the
GEM-15 DSM band extracted from Google's bucket, nodata filled from Copernicus
GLO-30 with an EGM2008-to-EGM96 correction and a 3 px feather, then the
pipeline's unmodified aggregation, downsampling and bundling stages. Each
asset records its derivation in `processing:lineage`. The tooling and this
metadata are in the
[catalog repository](https://github.com/portolan-mirrors/gem-15-catalog), and
problems with the archives belong in
[its issue tracker](https://github.com/portolan-mirrors/gem-15-catalog/issues).
