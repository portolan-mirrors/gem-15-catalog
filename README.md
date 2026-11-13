# gem-15-catalog

The git-backed [Portolan](https://www.portolan-sdi.org/) catalog that mirrors
**GEM-15**, Google Research's experimental near-global surface (DSM) and
bare-earth (DTM) elevation models at 15 m, derived from sub-meter optical
stereo imagery ([paper](https://doi.org/10.31223/X55N75),
[Earth Engine entry](https://developers.google.com/earth-engine/datasets/catalog/projects_climate-and-sustainability_assets_gem-15_v1)).

The published catalog lives at
**[source.coop/portolan-mirrors/gem-15-catalog](https://source.coop/portolan-mirrors/gem-15-catalog)**.
The 26,970 Cloud-Optimized GeoTIFFs (12.3 TB) stay in Google's public bucket,
`gs://climate-and-sustainability/gem-15`; this catalog references them in
place and adds:

- Portolan-conformant STAC metadata, with item links resolving into Google's
  own STAC tree, and an `items.parquet` index of all 13,485 one-degree cells.
- Documentation and agent guides built from the GEM-15 paper
  ([notes with section references](docs/research/gem15-paper-notes.md)).
- A global overview COG rendered with Google's own cyclic-hillshade
  visualization.
- **Terrarium-encoded terrain PMTiles**, so MapLibre GL can use GEM-15 as an
  elevation layer (`raster-dem`): hillshade, color-relief, and 3D terrain, with
  ready-made styles in `catalog/gem-15/styles/`.

## Layout

- `catalog/` — the published catalog: everything in it is published, nothing
  outside it ever is. STAC JSON, README/AGENTS guides, styles.
- `staging/publish/` — generated data files (gitignored): items.parquet,
  overview COG, terrain PMTiles, thumbnail. Uploaded by
  `tools/upload_data.py`.
- `tools/` — the build pipeline and publisher. `RUNBOOK.md` documents the full
  build, which runs on a large server (it reads from Google's 12 TB bucket).
- `docs/research/` — the source register: paper fact sheet, Google's example
  visualization script, generated link lists.
- `DESIGN.md` — the decisions and the asset contract.

## Contributing

Corrections and improvements are welcome as pull requests. CI validates every
change with [rashid](https://pypi.org/project/rashid/) (Portolan conformance)
and stac-check. Problems with the data itself belong upstream with Google; the
[issue tracker](https://github.com/portolan-mirrors/gem-15-catalog/issues)
here covers the catalog, its metadata, and the derived layers.

## License

Catalog metadata and tooling: Apache-2.0 (see `LICENSE`). The GEM-15 data is
© Google, [CC-BY-4.0](https://creativecommons.org/licenses/by/4.0/), and
requires the attribution "This dataset is produced by Google".
