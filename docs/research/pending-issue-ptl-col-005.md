### Use case

A publisher runs a metadata-first mirror of a large raster dataset. The producer already publishes complete per-item STAC next to the data. Example: [gem-15-catalog](https://github.com/portolan-mirrors/gem-15-catalog) mirrors GEM-15, Google's 15 m elevation models. The dataset spans 13,485 one-degree cells. The COGs total 12.3 TB. Google hosts the COGs in one public bucket, `gs://climate-and-sustainability/gem-15`. Google also hosts a full static STAC tree there. Its 13,485 items carry proj and raster metadata. The mirror adds Portolan conformance, documentation, visualization derivatives, and a STAC-GeoParquet item mirror. Its collection links items down into the producer's own tree, through 297 absolute `child` links to the upstream group catalogs. The publisher does not want to republish 13,485 item JSONs that duplicate the upstream byte for byte.

### Current limitations

`PTL-COL-005` (PORTO-CORE-015, PORTO-CORE-032, PORTO-FMT-042) fails such a collection. The message: "collection registers item mirror 'geoparquet-items' but publishes no items". The normative item JSON does exist. A client can reach it through the collection's child links. The upstream producer hosts it, behind absolute third-party links that the validator does not traverse. The spec has no way to state that the normative items for a mirror are the upstream catalog's items. The `canonical` link already expresses that relationship at the collection level. In practice, a metadata-first mirror must then republish thousands of unchanged item files. The only alternative is a permanent conformance waiver.

### Priority

P2: Planned. A workaround exists, the case is rare, or the work improves speed or ease of use.

### Example

```console
$ rashid check catalog/   # rashid 0.1.8, in portolan-mirrors/gem-15-catalog
error  PTL-COL-005  gem-15/collection.json: collection registers item mirror
'geoparquet-items' but publishes no items; the mirror is a derived copy and
the item JSON remains the normative representation
```

The collection carries a `canonical` link to https://storage.googleapis.com/climate-and-sustainability/gem-15/stac/gem-15/collection.json. It carries 297 `child` links into that tree. It registers `./items.parquet` with role `collection-mirror`, built 1:1 from the upstream items and upgraded to STAC 1.1. The current workaround is an `ACCEPTED` waiver row in the catalog's conformance docs that points at this issue.

### Proposed behavior

Define one configuration. The collection carries a `canonical` link to an upstream STAC collection. Its `child` or `item` links resolve into that upstream tree. In that configuration, allow the `collection-mirror` asset to derive from the upstream items. Either relax PTL-COL-005 in that configuration, or define it to verify a sample of upstream items against the mirror.

### Implementation notes

Option (a) reads two signals together. A collection-level `canonical` link plus external `child` links mean the upstream producer hosts the items. Option (b) adds an explicit field or role that states the item mirror's source. Option (c) fetches N upstream items through the child links and cross-checks them against `items.parquet` rows. Option (c) depends on the network, so it fits `--live` mode only. A related point is that the wording of PORTO-FMT-040 and PORTO-FMT-042 assumes the publisher owns the item JSONs.

### How to confirm it works

`rashid check` on portolan-mirrors/gem-15-catalog reports no PTL-COL-005 error. It still fails a collection whose `collection-mirror` matches no reachable items, upstream or local.
