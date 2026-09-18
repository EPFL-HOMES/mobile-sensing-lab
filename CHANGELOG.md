# Changelog

## 0.1.0 — 2026-09-17

Initial public working-project release.

### In-place update — 2026-09-18

- Retain version 0.1.0 and exactly two bundled example projects: Lausanne and San Francisco.
- Merge Lausanne bus lines 1, 3 and 7 into one fleet; refresh Postal and Taxi demand, supply and spatial features. San Francisco uses one 100-vehicle Taxi fleet.
- Calibrate exponential local utility to 99% at the configured saturation duration (five minutes in both examples), with versioned sample identities and an explicit editor explanation.
- Recompute both P05 and standard-deviation analyses: population utility weights for Lausanne and uniform weights for San Francisco.
- Display sensing duration in minutes, support saturation-only map filtering, and show saturated-cell and mean coverage percentages for selected portfolios.
- Preserve immutable example resources while accepting verified timestamp-only manifest differences during upgrades.

### Included

- Local browser, Python, and command-line workflows.
- Immutable project inputs, revisions, results, and portable project export.
- Directed-road, vehicle-agent discrete-event simulation with joint replications.
- Sparse vehicle-level exposure and road-intersecting spatial coverage.
- Uniform physical-vehicle sampling, nonlinear utility, empirical mean/P05 or mean/std frontiers, and bounded scientific exports.
- Local and OSM environment preparation, GTFS reconstruction, uploaded/generated demand and supply, automatic service areas, and bounded background jobs.
- Lausanne and San Francisco example bundles plus three Lausanne notebooks.

### Known limitations

- Local single-user deployment only.
- Public OSM acquisition depends on Overpass availability.
- Example operating assumptions are illustrative and require calibration before policy use.
- macOS on Apple Silicon is the primary validated desktop platform.
