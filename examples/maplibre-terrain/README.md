# MapLibre terrain example

A single-page map that reads one asset of this catalog, the terrarium-encoded
elevation PMTiles of GEM-15, and draws it three ways.

GEM-15 is the Google Research near-global 15 m surface and terrain elevation
model described in [doi:10.31223/X55N75](https://doi.org/10.31223/X55N75). This
repository mirrors it as the Portolan Collection
[gem-15-catalog](https://source.coop/portolan-mirrors/gem-15-catalog).

## What it shows

- A `raster-dem` source backed by PMTiles, read in the browser over HTTP range
  requests, with no tile server.
- A `color-relief` layer with an absolute hypsometric ramp, so color means
  height in meters rather than a position in the local range.
- A `hillshade` layer over that tint, using the `igor` method, which is the
  method intended for a hillshade beneath another layer.
- A toggle that raises MapLibre 3D terrain from the same source, with an
  exaggeration slider.
- A style switcher that loads the three published style assets of the Collection
  from the object store. A reader can see that a Portolan style document is
  complete and needs no rewriting by a client.
- Preset views inside GEM-15 coverage, among them the Kalgoorlie Super Pit,
  where the paper resolves the benches of a pit over 115 m deep.

GEM-15 is a surface model, so buildings and canopy are part of the terrain.
Elevations are meters above the EGM96 geoid. Coverage runs 84N to 61S and
excludes Russia, China, Mongolia, and Greenland.

## How to run it

GitHub Pages serves this example at
<https://portolan-mirrors.github.io/gem-15-catalog/maplibre-terrain/>. Open that
address in a browser.

To run the copy in this repository, serve it over HTTP. The page loads an ES
module, so a `file://` URL fails.

```bash
python -m http.server --directory examples/maplibre-terrain 8000
```

Then open `http://localhost:8000/` in a browser.

The page pins both dependencies and loads them from unpkg. MapLibre GL JS 6.18.0
provides ES modules only, so a module script imports it. The pmtiles 4.5.0
browser build sets a global, so a classic script tag loads it. The
`color-relief` layer type requires MapLibre GL JS 5.6.0 or newer, and the
`hillshade-method` property requires 5.5.0 or newer.

This example sits outside `catalog/`, so the publisher never uploads it to
Source Cooperative. GitHub Pages hosts the application, and the published
catalog keeps to metadata and styles.

## Sentinel-2 imagery on the Barcelona page

`barcelona.html` is a second, phone-friendly page over Barcelona alone. Its
panel offers a surface chooser. The default surface is the elevation tint. The
other draws Sentinel-2 true-color imagery over the same 3D terrain, so the
exaggeration slider and the terrain toggle keep working under it.

The imagery is the Copernicus Sentinel-2 quarterly cloudless mosaic, produced
by ESA and mirrored by Taylor Geospatial Engine as the Portolan collection
[sentinel-2-quarterly-cloudless-mosaics](https://source.coop/tge-labs/sentinel-2-quarterly-cloudless-mosaics).
The page reads the 2025 Q4 quarter. The temporal extent of that collection ends
on 2025-12-31, so 2025 Q4 is the most recent quarter available and no 2026
mosaic exists.

Each MGRS tile of that collection stores one band per file, so true color takes
three COGs: B04 for red, B03 for green, and B02 for blue. MapLibre cannot read
a COG, so the page registers an `s2://` protocol backed by geotiff.js. For one
map tile the handler picks the overview level that suits the zoom, reads a
window from each of the three files, warps EPSG:32631 into Web Mercator, and
composites the three bands into one image. Reflectance is the stored integer
over 10000. The stretch maps reflectance 0 to 0.25 onto 0 to 255 with a gamma
of 0.70, tuned against these files over Barcelona.

Nine MGRS tiles meet the demo area, and neighbours overlap by 120 m east to
west and 60 m south to north. For each output pixel the handler takes the first
tile in its baked-in manifest that both covers the pixel and holds data there.
That rule makes the result independent of which read finishes first, and it
stops one tile edge from punching a hole in its neighbour.

geotiff.js weighs about 540 kB, and the page requests it only when a reader
first turns the imagery on, so the elevation path costs no extra bytes.

## The data is not published yet

This catalog has not had its first data publish, so the terrain PMTiles asset
answers 404 today. The page checks for the asset on load and shows an
explanatory banner instead of an empty map. The page needs no edit when the
catalog publishes, because the published addresses stay the same.

## What a Portolan raster-dem terrain layer is

A Portolan Collection publishes cloud-native bytes that a browser can read
directly. For elevation, that means a format MapLibre accepts as a `raster-dem`
source. Such a source is a tiled image whose red, green, and blue channels encode
height.

Google distributes GEM-15 as 26,970 Float32 Cloud Optimized GeoTIFFs. MapLibre
cannot read those as elevation, because a `raster-dem` source accepts only the
terrarium and Mapbox Terrain-RGB encodings. So this catalog derives one PMTiles
archive from the DSM in terrarium encoding, where
`elevation = (R * 256 + G + B / 256) - 32768`. The archive is one file, read over
range requests, global, 256 px tiles, zoom 0 to 14. Zoom 14 carries the native
15 m resolution everywhere, including the tropics, where zoom 13 undersampled it
by up to 1.27 times. MapLibre overzooms past 14 rather than requesting tiles that
do not exist.

Terrarium has no no-data value, so ocean encodes as 0 m, and the hypsometric ramp
paints 0 m as a water tone. GEM-15 masks about 2.5 % of its cells, and the
terrain tiles fill those inland voids from Copernicus GLO-30 before encoding, so
a reservoir or an icefield holds its real height rather than reading as a 0 m
pit. A filled pixel is GLO-30 at 30 m rather than GEM-15 at 15 m, which matters
for measurement and not for drawing. Filling carries a required notice:
"produced using Copernicus WorldDEM-30 © DLR e.V. 2010-2014 and © Airbus Defence
and Space GmbH 2014-2018 provided under COPERNICUS by the European Union and
ESA; all rights reserved".

### Absolute asset URLs in a raster style

A Portolan vector style references its tiles with a relative path,
`pmtiles://../name.pmtiles`, resolved against the `styles/` directory that holds
it. That form depends on the client to resolve it. The generator in the
specification repository
[says so](https://github.com/portolan-sdi/portolan-spec/blob/main/examples/tools/derivatives.py):
the path is the one "clients like portolan-browser bind onto the data they
loaded".

MapLibre GL JS resolves nothing here. It passes a source `url` through to the
request unchanged, and the pmtiles protocol handler takes everything after
`pmtiles://` as the archive address and gives it to `fetch`. A relative path then
resolves against the HTML document rather than against the style that declared
it. A style served from an object store and loaded into a page on another origin
would look for its tiles beside the page.

This example loads the Collection styles into MapLibre, with no client in
between. So the three styles in `catalog/gem-15/styles/` use absolute published
URLs. That decision covers raster styles only. The relative convention for vector
styles stays as it is, because existing clients already resolve it.

## Raster styling is still an open question

The visualization style requirements of Portolan are vector-only today. The spec
does not define how a raster style is expressed, nor how a continuous raster
declares its color ramp. See
<!-- vale ai-tells.GrowthMetaphors = NO -->
<!-- The rule matches a directory name in the URL below, not prose. -->
[the raster styling document](https://github.com/portolan-sdi/portolan-spec/blob/main/specs/incubating/raster-styling.md)
<!-- vale ai-tells.GrowthMetaphors = YES -->
and the discussion in
[#41](https://github.com/portolan-sdi/portolan-spec/issues/41).

This example supplies evidence for that discussion. It proposes no rule. It
shows one raster style that works, written as a plain MapLibre style document
beside the data. It also raises two questions for the spec:

- Where does a style declare a ramp that maps color to absolute elevation?
- Where does a client read the encoding of an elevation asset?

Portolan metadata answers neither question today.

## Attribution

GEM-15 requires attribution. Google produces the data and licenses it
[CC-BY-4.0](https://creativecommons.org/licenses/by/4.0/). The string "This
dataset is produced by Google | CC-BY-4.0" appears on the `raster-dem` source and
in the page panel. Each published style asset repeats the same string.
