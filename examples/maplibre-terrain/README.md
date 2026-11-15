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

## Comparing three terrain tile sources

`compare.html` draws GEM-15 beside two other public terrarium tile sets, in
three synced panes over the same view. A click reads one elevation out of each
of them. GitHub Pages serves the page at
<https://portolan-mirrors.github.io/gem-15-catalog/maplibre-terrain/compare.html>.

The page is held inside the GEM-15 demo box, longitude 1 to 4 and latitude 40
to 42. Outside that box only two of the three panes have anything to draw.
Preset views cover Barcelona city center, Montserrat, the Sau reservoir, and
the Llobregat plain.

### Tilt

Each pane builds a MapLibre terrain mesh from the same `raster-dem` source that
it draws. So an oblique view shows three separate meshes, not one mesh under
three skins. GEM-15 measures the top of the surface, so a city block
becomes a raised block in its mesh. The AWS pane renders the same block as one
smooth slope.

The page opens flat, at a pitch of 0. The `3D` button tilts all three panes to
a pitch of 60. A right-drag or a ctrl-drag also rotates and tilts them. The
compass on the first pane restores north up and flat. A shared relief slider
scales every mesh by one factor, from 0 to 3, and it opens at 1. One factor
across the panes is a deliberate choice. A per-pane exaggeration would flatter
one source, and the three panes would no longer be readable against each other.

Center, zoom, bearing, and pitch all sync, and the sync cannot feed back on
itself. A reentrancy flag discards the move events that the follower panes fire
while the sync moves them. A comparison against the last view pushed discards
the duplicate event that arrives when MapLibre reports one gesture as a move
and as a rotate.

Terrain is stricter than drawing about the maxzoom that a source declares. If a
`raster-dem` source declares a maxzoom deeper than its archive, the tiles for
that zoom are absent, and the mesh tears or flattens there. Each of the three
source definitions carries the measured maximum, 14, 16, and 15, and terrain
reuses those definitions.

The probe works tilted. With terrain set, MapLibre unprojects a click against
the mesh, so the probe reads the point on the surface that the reader hit. A
tilted pane adds one case that a flat pane cannot produce. A click on the sky,
or past the terrain horizon, unprojects far outside the view. No point out there
has three sources to compare, so the page discards that click rather than
answer it with three em dashes.

### Source by source

| | GEM-15 demo | Mapterhorn | AWS Terrain Tiles |
| --- | --- | --- | --- |
| Model | surface, DSM | terrain, their word | bare earth |
| Tiles | terrarium WebP PMTiles | terrarium WebP PMTiles | terrarium PNG |
| Tile size | 256 px | 512 px | 256 px |
| Zooms | 0 to 14 | 0 to 12 planet, 13 to 17 shards | 0 to 15 |
| Deepest tile here | z14 | z16 | z15 |
| Source here | GEM-15 at 15 m | not published per tile | EU-DEM at 30 m |

GEM-15 is the only one of the three that measures the surface. Its heights
include buildings and canopy. Over a city block or a forest it measures the top
of the surface rather than the ground. A positive difference against a
bare-earth source there is signal rather than error.

The reverse happens too, so do not read every difference as a model difference.
A 30 m source smooths a hilltop down and a valley up. At the Barcelona city
center preset the AWS pane reads 10 m higher than GEM-15, not lower, because
EU-DEM at 30 m spreads the city over a coarse grid. The demo archive also
quantises to whole metres below zoom 14, measured on the live file, so a probe
compares numbers at zoom 14 and nowhere shallower.

### What was verified about the Mapterhorn archive

Everything below was read from the live files and from the Mapterhorn README on
2026-11-14, not assumed.

The archive set is at <https://source.coop/mapterhorn/mapterhorn>. Their README
gives the layout and this page follows it exactly. One `planet.pmtiles` covers
zoom 0 to 12. One archive per zoom 6 tile covers the deeper zooms, named
`6-{x >> (z - 6)}-{y >> (z - 6)}.pmtiles`. A full listing of the prefix returns
611 keys: 459 PMTiles archives, `README.md`, `attribution.json`,
`download_urls.json`, and 149 raw source tarballs under `sources/`. The tile
URL this page builds is

```
https://s3.us-west-2.amazonaws.com/us-west-2.opendata.source.coop/mapterhorn/mapterhorn/<name>.pmtiles
```

The same objects also answer on `https://data.source.coop/mapterhorn/mapterhorn/`.
This page uses the address their README publishes.

PMTiles v3 headers read over range requests: `planet.pmtiles` is WebP, zoom 0
to 12, global, and 355 GB. `6-32-23.pmtiles`, the shard that holds Barcelona,
is WebP, zoom 13 to 17, over longitude 0 to 5.625 and latitude 40.98 to 45.09,
and 9.2 GB. Tiles are 512 px, which their own MapLibre example declares and a
decode confirms. The encoding is terrarium.

**The maxzoom the shard declares is not the maxzoom it serves here.** The
header says 17. Every zoom 17 lookup over this box returns no tile, at all four
preset places. Zoom 16 answers at all four. So the source stops at 16, which
keeps MapLibre from requesting tiles that are not there. The probe walks down
from 17 until a tile answers, then reports the zoom it used. The Swiss shard,
`6-33-22.pmtiles`, does serve zoom 17 over the Matterhorn, so the gap over
Catalonia is a per-area build decision rather than a broken archive.

**Which source Mapterhorn uses here is not published, and this page does not
guess.** Their catalogue, `attribution.json`, lists 134 entries. Among them are
a Spanish 2 m terrain model and a 5 m one, both CC-BY 4.0 from scne.es, and a
global Copernicus GLO-30 at 30 m. Nothing in the archive or the catalogue says
which of those built one tile, and unlike the AWS set there is no per-tile
source header to read.

An attempt to infer the source resolution from the tiles failed. The record of
that attempt belongs here so nobody repeats it. A profile across the Montserrat
face at zoom 16 changes by 0.03 to 0.28 m per 0.89 m pixel. It also keeps some
residual structure at a 30 m period, which looked like a coarse source
upsampled. The control killed the idea. The same measurement over the
Matterhorn, where Mapterhorn has swissALTI3D at 0.5 m, shows that periodic
structure more strongly rather than less. The measurement reads terrain
character, not source grid. So the preceding table says "not published per
tile" and leaves it there.

Attribution follows their README, which states that Mapterhorn uses the sources
listed at <https://mapterhorn.com/attribution>, and their own MapLibre example
credits "© Mapterhorn" with a link to that page. The page prints that string
and names the count of sources behind it. That is their whole stated
requirement, and it is also the only form that remains correct when the source
behind a tile is unknown.

### What was verified about the AWS set

The tiles are at `https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png`,
terrarium PNG, 256 px. Zoom 15 answers 200 and zoom 16 answers 404, so the
source stops at 15. The registry entry describes the set as "bare-earth terrain
heights" and names Mapzen, a Linux Foundation project, as its manager.

Over Catalonia the underlying source is EU-DEM at 30 m, so the deep zooms are
upsampled here too. The page can say so per tile rather than in general. Each
response includes an `x-amz-meta-x-imagery-sources` header with the source
files that went into it, and the bucket exposes that header to cross-origin
readers.
The probe prints it.

Attribution is quoted from the required-attribution block of
[the joerd attribution document](https://github.com/tilezen/joerd/blob/master/docs/attribution.md),
keeping the European entry that this view draws and the global fallbacks.

### CORS and range requests

Each of the three hosts answered an `Origin` header probe on 2026-11-14 with
`access-control-allow-origin: *`. Both Source Cooperative hosts answer 206 to a
`Range` request. The AWS bucket answers an `OPTIONS` preflight with 200 and
allows `GET`. So a browser reads the three of them directly, with no proxy and
no tile server.

A GitHub release asset is the one host that cannot serve these tiles. It honours
range requests but sends no CORS headers, and its preflight answers 405, so the
browser blocks every tile request. `barcelona.html` records the same note.

### The probe path for one pixel

On a click the page asks each source for the tile that covers the point, at that
source's best zoom. GEM-15 answers at zoom 14, Mapterhorn at the deepest zoom
that answers, and AWS at zoom 15. The page then fetches the tile bytes and decodes
them with `createImageBitmap`. It draws the bitmap onto a 1 by 1 canvas at a
negative offset, which lifts out the target pixel alone and allocates no full
tile of image data. Then
`elevation = R * 256 + G + B / 256 - 32768`.

Fetching the bytes rather than pointing an `img` element at the URL also avoids
canvas tainting, so the page needs no `crossOrigin` attribute to read the
pixel back.

Terrarium has no no-data value. A pixel of 0, 0, 0 decodes to &minus;32768 m,
which is the floor of the encoding rather than a place on Earth, so the probe treats
it and a transparent pixel as no value and prints an em dash. A failed fetch, a
missing tile and an undecodable tile all end the same way, and the panel says
which zooms it tried and how each one came back.

## Mapterhorn today beside a GEM-15 build

`mapterhorn-gem15.html` puts what Mapterhorn ships today beside both GEM-15
bands, in three synced panes, over three areas. GitHub Pages serves it at
<https://portolan-mirrors.github.io/gem-15-catalog/maplibre-terrain/mapterhorn-gem15.html>.

| Pane | Source |
| --- | --- |
| left | Mapterhorn today. Spanish lidar over Barcelona, Copernicus GLO-30 elsewhere |
| middle | GEM-15 DTM, the bare-earth band |
| right | GEM-15 DSM, the surface band, with buildings and canopy in it |

The middle pane is what makes the Barcelona comparison fair. Mapterhorn serves
a bare-earth model there, so a surface model beside it differs by two things at
once: resolution and the model. The GEM-15 DTM beside the lidar DTM is the
matched test, and the DSM pane then shows what the surface band adds on top.

The archives in the two GEM-15 panes are experimental. They come from an
unmerged fork, [cholmes/mapterhorn tree gem-15-source](https://github.com/cholmes/mapterhorn/tree/gem-15-source),
and the upstream discussion is
[mapterhorn/mapterhorn#368](https://github.com/mapterhorn/mapterhorn/issues/368).
The Mapterhorn project publishes none of them. The page says so in a note that
stays on screen.

### Why three areas

Mapterhorn covers zoom 0 to 12 everywhere from one `planet.pmtiles`, and zoom
13 and deeper from one archive per zoom 6 tile, named `6-{x}-{y}.pmtiles`. A
regional archive exists where a national model justifies one, and nowhere else.
So the honest question is not whether GEM-15 beats Mapterhorn. It is where.

| Area | Mapterhorn today | Both GEM-15 bands |
| --- | --- | --- |
| Barcelona | shard `6-32-23`, to zoom 16 | zoom 0 to 12 |
| Cusco and Machu Picchu | `planet.pmtiles`, zoom 12 | zoom 0 to 13 |
| High Atlas and Toubkal | `planet.pmtiles`, zoom 12 | zoom 0 to 13 |

Over Barcelona, Mapterhorn production is Spanish lidar at zoom 16. GEM-15
enters at zoom 12 under the Mapterhorn maxzoom rule, because 15 m at 41N does
not clear their zoom 13 threshold. GEM-15 does not compete where national lidar
exists, and the page states that rather than hiding it. The left pane and the
middle one are both bare earth, and zoom 16 against zoom 12 is the gap between
them, so that pair is the comparison to read at Montserrat.

Over Cusco and over the High Atlas, Mapterhorn has Copernicus GLO-30 alone,
because no regional archive exists there. The GEM-15 build adds zoom 13, one
level deeper than production, from a 15 m source rather than a 30 m one. That
is the case for the experiment.

All three GEM-15 archives carry the water-void fill from Copernicus GLO-30, the
same fill the Sau reservoir preset shows.

### What was measured on the live files

Everything below was read from the archives on 2026-11-15. Each PMTiles v3
header came back over a range request, and the root directory was decoded to
get the real tile footprint. No archive was downloaded in full.

| Archive | Size | Zooms | Tiles | Footprint from the directory |
| --- | --- | --- | --- | --- |
| `mapterhorn-gem15-barcelona.pmtiles` | 42.1 MB | 0 to 12 | 10931 | lon 0 to 5.625, lat 36.598 to 45.089 |
| `mapterhorn-gem15-barcelona-dtm.pmtiles` | 37.9 MB | 0 to 12 | 10931 | the same |
| `mapterhorn-gem15-cusco.pmtiles` | 129.4 MB | 0 to 13 | 10929 | lon &minus;73.125 to &minus;70.3125, lat &minus;16.636 to &minus;11.178 |
| `mapterhorn-gem15-cusco-dtm.pmtiles` | 129.2 MB | 0 to 13 | 10929 | the same |
| `mapterhorn-gem15-atlas.pmtiles` | 114.6 MB | 0 to 13 | 10931 | lon &minus;8.4375 to &minus;5.625, lat 29.535 to 34.307 |
| `mapterhorn-gem15-atlas-dtm.pmtiles` | 112.3 MB | 0 to 13 | 10931 | the same |

All six are terrarium WebP, 512 px, uncompressed tiles, minzoom 0. All six
answer 206 to a range request and send `access-control-allow-origin: *`. One
tile out of each one was decoded to confirm the tile size and the encoding. The
heights agree with the ground. Machu Picchu reads 2442 m, Toubkal 4109 m, and
Imlil 1736 m.

Each DTM archive matches its DSM sibling in minzoom, in maxzoom, and in tile
count, which the directory column records. So the page keys the footprint and
the maxzoom off the area and not off the band.

**The header bounds of these archives say the whole world, which is wrong about
coverage.** So the page declares the measured footprint on the `raster-dem`
source rather than the header value. That keeps MapLibre from requesting tiles outside
the build.

A HEAD request on each candidate Mapterhorn archive shows which areas have
one. `6-32-23.pmtiles` answers 200 and holds the Barcelona view box.
`6-19-34.pmtiles` answers 404, and it is the only zoom 6 tile the whole Cusco
footprint falls in. `6-30-26.pmtiles` answers 404, and it holds Toubkal, Imlil,
and Oukaimeden.

### The band gap, point by point

The GEM-15 DTM is derived from the DSM by a machine-learning model. Nothing in
the build measures the ground separately. So the gap between the two GEM-15
panes is the output of that model, and not a measured object height.

Every figure below is DTM minus DSM, read at the deepest zoom each archive has,
at the preset points of the page, on 2026-11-15.

| Point | Band gap | Ground |
| --- | --- | --- |
| Sau reservoir, 2.4000E 41.9700N | 0.00 m | both bands take the GLO-30 water-void fill |
| Montserrat, 1.8380E 41.5930N | &minus;0.50 m | bare rock |
| Eixample, 2.1627E 41.3917N | &minus;5.00 m | city blocks, so buildings removed |
| Ollantaytambo, 72.2650W 13.2575S | &minus;3.75 m | valley floor and terraces |
| Cusco valley, 71.9780W 13.5190S | 0.00 m | open ground |
| Toubkal summit, 7.9147W 31.0606N | &minus;0.75 m | bare rock above the tree line |
| Imlil valley, 7.9190W 31.1360N | &minus;3.00 m | a valley floor at 1736 m |
| Oukaimeden, 7.8610W 31.1930N | 0.00 m | a high basin |
| Machu Picchu, 72.5450W 13.1630S | &minus;92.25 m | a citadel ridge, and read on |

DTM is at or below DSM at every one of those points. That is the only thing the
pair of bands guarantees.

**The size of the gap is not a proxy for canopy or building height on steep
ground.** Machu Picchu is the proof. No canopy on that ridge is 92 m tall. The
build mosaics each band independently, the pixel is 9.30 m there, and on a ridge
that drops hundreds of metres across a few pixels a sub-pixel difference in
where each band lands moves the number by tens of metres. The High Atlas shows
the same effect at a readable scale. The gap is 0.75 m at the Toubkal summit and
10.25 m about 150 m west of it, at 7.91W 31.06N, on an 8.18 m pixel. The flat
points are the trustworthy ones. The page repeats that caveat in a note that
stays on screen, in the delta row of the probe, and in the hint on the Machu
Picchu preset.

### The view box is not the footprint

One neighbour forced that split. `6-30-25.pmtiles` answers 200. It covers the
Middle Atlas, north of latitude 32.0997, and the Atlas footprint reaches to
34.307. A reader who panned past 32.0997 would cross into an archive that does
exist, and the left pane would stop being planet-only without saying so. So the
Atlas view box stops at latitude 32.05. The Barcelona box is held the other
way, inside `6-32-23` rather than straying into `6-32-24`, which also answers
200 and which this page has measured nothing about. A comment beside each area
in the page records its box and the reason for it.

### Tilt, sync, and the area switch

Each pane raises a MapLibre terrain mesh from the same `raster-dem` source it
draws, so an oblique view shows three meshes rather than one mesh with three
skins on it. The two GEM-15 meshes differ from each other by whatever the DTM
model removed. The `3D` button tilts all three to a pitch of 60, and a
right-drag rotates and tilts them by hand. One relief slider scales every mesh
by one factor. Center, zoom, bearing, and pitch all sync, under the same two
guards `compare.html` uses.

An area switch rewrites three things together: the style of each pane, the
camera limits, and every place name in the bar. The camera limits need the
awkward order. `maxBounds` clamps the camera, so the new box cannot go on
before the flight, or a flight from Barcelona to Cusco inside the Cusco box
would never leave Barcelona. The box comes off, the panes fly, and the new box
goes on at the end of the flight. A token guards that, because a reader can
switch area again in mid-flight.

A click reads one elevation out of each pane, at the best zoom that pane has in
this area. The decode path is the one `compare.html` uses. Over Barcelona the
left pane walks down from zoom 17 and stops at 16, and the two GEM-15 panes read
zoom 12. Over the other two areas the left pane reads zoom 12 and the two GEM-15
panes read zoom 13. The panel prints the elevation, the zoom, the ground
resolution of that zoom, the tile, and the archive the bytes came from. Under
the cards it prints all three pairs of differences: each band against the
Mapterhorn model, and then one band against the other. Each pair is a separate
question, so the panel shows all three.

### Attribution on this page

Both GEM-15 sources carry three credits, and all three are required rather than
polite. Google produces the elevation data and licenses it CC-BY-4.0. The
filled pixels carry the Copernicus notice. The tiles were cut by the Mapterhorn
pipeline, and the archive metadata itself holds their string, so the page
repeats it and names the fork. Both bands come from one build under one
license, so one string serves them. The left pane credits "© Mapterhorn" with a
link to their attribution page, the same form `compare.html` uses.

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
