# GEM-15 paper — fact sheet for catalog documentation

Source: Batchu, V., Neumann, M., Li, T., Allen, B., Zhang, J., Van Arsdale, C.
(2026). "GEM-15: Global surface and terrain elevation models (15 meters) from
sub-meter optical stereo imagery." EarthArXiv preprint (submitted to ESSD),
[doi:10.31223/X55N75](https://doi.org/10.31223/X55N75),
[landing page](https://eartharxiv.org/repository/view/16034/). Extracted
2026-11-13 from the 52-page PDF. Section/table references point into the
preprint. Every claim in the published catalog documentation traces to this
file, to Google's own metadata (Earth Engine catalog entry, bucket STAC), or
to a measurement we ran — nothing else.

## Production methodology

**Sensors & shares (Sect. 2.2, Fig. 2):** Commercial Airbus and Maxar optical
stereo imagery acquired through 2024, native GSDs 0.3–1.5 m. Pléiades Neo
(0.30 m pan), acquired 2022–2024: **63.2 % of mapped tiles**. Other sub-meter
archive (WorldView-2/3/4, GeoEye-1, Pléiades 1A/1B): **24.4 %**. SPOT-6/7
(1.5 m pan wide-swath): **12.4 %**, mainly boreal, desert, remote rural.

**Stereo pipeline (Sect. 3.1.1):** Two-stage. (1) Pose refinement of vendor
RPC models, starting from ALOS AW3D30 as coarse prior, alternating heightmap
and shift estimation coarse-to-fine (~15 m to ~0.5 m). (2) DSM generation:
coarse 1.0 m depth maps from a multi-temporal stack via plane-sweep stereo,
fused by graph-cut optimization; then per-pair ML depth (Swin-transformer
U-Net, as in Batchu et al. 2024 "Satellite Sunroof") on optimal stereo pairs,
fused at 12.5 cm and downsampled to the **native 0.5 m DSM**.

**DSM→DTM (Sect. 3.1.1):** DTM predicted at **1.0 m** by a seven-stage
multi-scale residual U-Net on 1024 m UTM tiles (4 channels: RGB +
mean-normalized DSM), trained against 1.0 m USGS 3DEP airborne-lidar DTMs (US
only); at inference DTM is clamped ≤ DSM and Gaussian-smoothed (3 m radius).
Native 0.5 m DSM and 1 m DTM are proprietary (imagery licensing) and not
distributed.

**Aggregation to 15 m (Sect. 3.1.2, App. B):** 0.5 m DSM / 1 m DTM bilinearly
resampled to a 2.5 m grid (6×6 sub-cells per 15 m cell); DSM = mean,
percentiles and σz from the same sub-cells; **DTM = median** of sub-cells,
then clamped ≤ DSM. The two-stage scheme matches direct block reduction within
0.02 m DRMSE on all six bands.

**Masking (Sect. 3.1.2):** (a) water masked at 0.5 m, dilated 9.0 m;
(b) cells with |Δz| > 50 m vs Copernicus GLO-30 removed (stereo blunders,
clouds) — the product "is to that extent not independent of GLO-30";
(c) planar-artifact filter (DSM σz < 0.02 m over non-water, with 60 m erosion
/ 120 m dilation) removing stepped blocks over snowfields, flat deserts,
glaciated plateaus. Total masking ≈ **2.5 % of cells**, concentrated over
perennial snowfields, glaciated peaks, inland water. Masked cells are masked
in **all six bands**. Paper's gap-fill advice: masked cells "can be filled
using the Copernicus GLO-30 DEM at some cost to accuracy."

## Accuracy

Primary metric: tile-debiased RMSE ("DRMSE") over 1.5 km tiles; also NMAD and
pooled mean bias µ = DEM − reference (Sect. 3.2). The paper reports no LE90 or
MAE.

**Headline (Abstract, Sect. 4.1.1, Table E1):** mean airborne/drone-campaign
DSM DRMSE **GEM-15 3.37 m vs GLO-30 3.85 m (12.3 % lower)**, AW3D30 4.35 m,
NASADEM 4.82 m; NMAD 1.98 / 2.26 / 2.90 / 3.58 m. On the seven post-2021
benchmarks: 3.69 vs 4.27 m (13.7 % lower). GEM-15 beats AW3D30 and NASADEM on
every campaign, GEDI, and CORS. Vs GLO-30 per campaign: from 11.9 % worse (US
Tropical) to 26.9 % better (New Zealand 2024–25); the margin grows with
reference survey date (+3.44 %/yr, Spearman ρ=0.87, p=0.001; Fig. 7). GLO-30
is slightly better on the three pre-2021 low-relief benchmarks (Mozambique
2014, Spain 2018, US Tropical 2020), by 0.05–0.16 m.

**GEDI 2025 (159k footprints, Table E1):** DSM DRMSE 5.05 vs GLO-30 5.10 m;
restricted to slope < 5° and height-above-ground ≤ 5 m: 1.21 vs 0.85 m
(GLO-30 better on open flat ground). CORS GNSS (4,071 stations): global DRMSE
**4.92 vs 5.80 m (15.2 % lower)**, GEM-15 lowest on all six continents.

**Per-benchmark DSM DRMSE (GEM-15 / GLO-30, Table E1):** India Drone
2.23/2.79; 3D Trees 6.66/6.88; Global Drone 3.05/3.89; New Zealand 3.92/5.37;
US NEON 4.00/4.66; Canada 3.61/3.95; UK 2.33/2.36; US Tropical 1.33/1.19;
Spain 1.39/1.34; Brazil 4.20/4.68; Indonesia 6.06/7.52; Mozambique 1.69/1.53.

**Strata (Sect. 4.1.2–4.1.3):** above-ground (>5 m) GEM-15 mean 3.95 vs 4.31 m
(up to 29.3 % better, NZ); bare ground (≤5 m) GLO-30 slightly better on most
(2.33 vs 2.46 m mean). Steep slopes (≥10°): GEM-15 mean 4.19 vs 4.82 m.

**Vertical bias vs airborne lidar (Fig. 6c):** within ±1 m in UK, Indonesia,
NZ, Spain; 1.2–2.3 m magnitude in Mozambique, US Tropical, US NEON, Brazil;
largest **Canada −4.80 m**. (Hence the EE-catalog guidance: debias against
local control where absolute precision is critical.)

**DTM vs FABDEM (Sect. 4.2, Table E2):** "Overall, the GEM-15 DTM is not more
accurate than FABDEM; it differs in its finer 15 m grid, more recent imagery,
and exact co-registration with the GEM-15 DSM." Mean airborne DRMSE 3.57 vs
3.37 m. Worst gaps under closed forest: US NEON 5.88 vs 3.99 m; Indonesia 7.57
vs 6.87 m; GEDI 4.19 vs 3.18 m; closed-canopy DTM bias up to **+9.4 m
(Indonesia)**. GEM-15 DTM better on 3D Trees, Global Drone, and on several
steep/flat strata.

**Percentiles & σz (Sect. 4.3, Table E3):** percentile layers within 1.5 m
DRMSE of the DSM everywhere; under closed rainforest canopy P5 has the highest
error (ground gaps occluded). Bias decreases monotonically P5→P95: stereo
occludes narrow ground gaps (raises P5) and smooths crown tips and roof peaks
(lowers P95). DSM σz DRMSE 0.66–1.60 m outside rainforest.

**NEON time series (Fig. 8):** GEM-15 lowest DRMSE in every survey year of
both fixed cohorts; error rises for post-epoch (2025) flights, margin over
GLO-30 up to 1.51 m.

## Datums, grid, tiling (Sect. 2.1.3, Table 1)

- Heights: meters, orthometric, **EGM96 geoid** (EPSG:5773). Horizontal:
  WGS 84 / local UTM, exact 15 m spacing aligned to the zone origin; 1°×1°
  tiles never cross a UTM zone boundary.
- **13,485 tiles** on the integer 1° grid, named `w[m]XXs[m]YY` (west-edge
  longitude, south-edge latitude, `m` = minus): wm72s42 = [−72,−71]×[42,43].
- All six bands share the grid exactly; DTM ≤ DSM enforced. Horizontal
  accuracy of GEM-15 itself is not quantified in the paper.
- **India:** vertical offsets incorporated into DSM and DTM per the Government
  of India National Geospatial Policy (>3 m absolute vertical accuracy limit),
  tapering off outside India over tens of kilometers. Absolute heights over
  India are intentionally degraded.
- For evaluation, GLO-30/FABDEM were converted EGM2008→EGM96; the EE example
  script notes the visual difference is minimal but precise altimetry needs a
  geoid correction.

## Coverage (Sect. 2.1–2.2)

84°N–61°S, "covering the large majority of the ice-free land surface outside
Russia, China, Mongolia, and Greenland, where commercial licensing
restrictions preclude suitable source imagery." ~2.5 % of cells masked within
covered tiles. Imagery through 2024; Pléiades Neo window 2022–2024 (63.2 % of
tiles); regions outside that coverage rely on older archive imagery. No
per-tile acquisition-date layer is published.

## Limitations checklist (Sect. 5.5 + scattered)

- No coverage: Russia, China, Mongolia, Greenland (licensing); Antarctica
  (south of 61°S).
- Voids: water (+9 m buffer), snow/ice, salt pans, low-texture surfaces,
  >50 m GLO-30 outliers (~2.5 % of cells).
- GLO-30 dependence: genuine changes exceeding 50 m (new open-pit mining
  benches, tall new towers, large landslides) are masked out.
- DTM under dense/closed canopy is ML-inferred without a ground view: DRMSE up
  to ~9 m on steep forested sites; FABDEM more accurate there.
- Temporal mixing across acquisition boundaries; 36.8 % of tiles use pre-2022
  archive imagery.
- Regional vertical offsets −4.8 m to +1.9 m vs airborne lidar; debias against
  local control for absolute work.
- Super-tall (~>50 m) buildings often poorly captured (stereo limitations —
  from the EE catalog entry).
- CORS residuals partly reflect antenna mounts, not pure DEM error.

## Band semantics and uses (Sect. 2.1.1, Table 1, Sect. 5)

- `dsm` — block-mean top surface (buildings + canopy).
- `dsm_percentile_5` — ground gaps, roads, street-canyon floors, lower canopy.
- `dsm_percentile_50` — median; less sensitive to outliers and step edges.
- `dsm_percentile_95` — roof ridges, ridgelines, emergent crowns.
- `dsm_std` — sub-cell roughness: canopy variability, urban structural
  complexity.
- `dtm` — bare earth; DTM ≤ DSM enforced.
- **Height above ground = dsm − dtm** (canopy height, building height);
  resolves individual crowns, clearings, city blocks, mining benches
  (Kalgoorlie pit >115 m deep, Fig. 5).
- Hydrology (Sect. 5.2, Eardisley UK): the DTM isolates stream corridors
  between bankside canopies, tracking 1 m lidar within 1–3 m over most of an
  870 m reach, vs a >20 m artificial barrier in GLO-30; systematic evaluation
  for low-order reaches is explicitly left for future work.
- Forest edges (Sect. 5.3, Sinop Brazil): a ~30 m forest-to-pasture step is
  captured within a single 15 m pixel where 30 m DEMs spread it over
  60–100 m.
- Urban (Sect. 5.4, Christchurch): DSM/P95 resolve individual rooftops where
  GLO-30 is a near-uniform plane; P5 recovers street level.

## License & attribution — exact quotes

- Abstract: "The dataset is available under a CC BY 4.0 license at
  https://developers.google.com/earth-engine/datasets/catalog/projects_climate-and-sustainability_assets_gem-15_v1."
- Code and data availability: "The GEM-15 dataset (v1) is openly available
  under the Creative Commons Attribution 4.0 International (CC BY 4.0) license
  at https://storage.googleapis.com/climate-and-sustainability/gem-15."
- EE catalog terms: requires the attribution **"This dataset is produced by
  Google"**.
- Acknowledgements credit Airbus Defence and Space and Maxar Technologies for
  the commercial source imagery (native-resolution products cannot be
  redistributed).

## Distribution channels (Sect. 2.1.3; Code and data availability)

1. Earth Engine ImageCollection
   `projects/climate-and-sustainability/assets/gem-15_v1`.
2. Explorer app:
   https://climate-and-sustainability.projects.earthengine.app/view/gem-15
3. GCS COGs + STAC: https://storage.googleapis.com/climate-and-sustainability/gem-15
   (bucket has "a permanent, no-deletion retention policy").
4. Evaluation code:
   https://github.com/google-research/google-research/tree/master/gem_15
