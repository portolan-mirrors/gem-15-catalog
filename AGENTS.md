# Agent norms — gem-15-catalog

Read `DESIGN.md` first: it is the coordination contract for this catalog (what
is mirrored, what is derived, the asset contract, and where bytes live). The
[portolan-spec](https://github.com/portolan-sdi/portolan-spec) repo is the
ground truth for the Portolan spec. Use conventional commits. Before
documenting any command, flag, or API, verify that it exists.

## This Repository

A git-backed Portolan catalog mirroring GEM-15 (Google's 15 m elevation
models). Metadata-first: the COGs and per-item STAC stay at Google's bucket;
this repository holds the catalog metadata, documentation, and styles, plus
the pipeline (`tools/`) that builds the derived files (items.parquet, overview
COG, terrain PMTiles, thumbnail) into `staging/publish/`.

### The publish boundary

`catalog/` is the published catalog. Everything in it is published, and
nothing outside it ever is. Do not move a file into `catalog/` to make it
publish, and do not add a path outside `publish_dir` to `tools/publish.py`.
Generated data files go to `staging/publish/` (same layout as `catalog/`) and
reach the bucket through `tools/upload_data.py`.

### Data never enters git

Never commit a GeoParquet, COG, PMTiles, Zarr, or COPC file. Per-item STAC
JSON is also never committed or generated: item links point into Google's own
STAC tree. Git keeps every version of a binary forever and deleting it later
reclaims nothing.

### Heavy builds run on the big server

The full terrain-tile and overview builds read terabytes from Google's bucket.
Run them where `RUNBOOK.md` says, not on a laptop. Every `tools/` script must
support a smoke-test invocation on one cell.

### The conformance allow-list

`ACCEPTED` in `tests/test_conformance.py` ships empty. Never add an entry
without a matching row in `docs/conformance.md` giving the rule, where it
fires, why it is accepted, and the issue tracking its removal.

### Published agent guides

Every claim in a `catalog/**/AGENTS.md` is either quoted from a source or
measured from the data. The source register for this catalog is
`docs/research/gem15-paper-notes.md` (paper facts, with section references),
Google's own metadata (Earth Engine entry, bucket STAC), and measurements run
against the published files. An invented fact produces a confident wrong
answer that nothing downstream catches.

### The links back to this repository

`catalog/catalog.json` ships a `vcs` link and an `issues` link, both absolute.
The repository sits outside the published catalog, so a relative href resolves
against the public base URL.
