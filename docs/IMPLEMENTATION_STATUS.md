# Implementation status

## Current candidate

Version `0.1.0` is being prepared as a public working-project release. It provides a local browser application, importable Python package, command-line interface, durable background jobs, immutable scientific artifacts, and portable project export.

Implemented workflow:

- local or OSM-based environment preparation with directed roads, reporting grids, travel-time models, and grid-aggregated spatial features;
- uploaded, generated, and GTFS-derived demand and supply;
- one shared vehicle-agent discrete-event kernel with fixed physical catalogs and joint replications;
- sparse physical-vehicle exposure on road-intersecting grid cells;
- fleet operations, sensing maps, temporal profiles, and bounded exports;
- count-portfolio enumeration, uniform physical-vehicle sampling, nonlinear within-sample utility, empirical statistics, and budget frontiers;
- independent simulation reporting and portfolio utility intervals;
- weighted spatial-feature mixtures and demand-balanced automatic service areas;
- resilient, bounded OSM road/feature acquisition with caching, endpoint diagnostics, adaptive subdivision, and serial public requests;
- two read-only city examples and three executable Lausanne notebooks.

## Scientific interpretation

Operational replications `R` and portfolio sampling rounds `J` are independent. Supply defines physical vehicles; portfolio counts never alter operational supply. Each portfolio round draws one joint replication and concrete vehicles, evaluates nonlinear utility on that realization, and only then contributes to statistics.

The bundled examples use synthetic demand, inferred duties, assumed speeds, heuristic dispatch, illustrative costs, and in-sample analysis. Their results demonstrate the software and are not calibrated city-wide estimates.

## Release validation

The public-source candidate passed the complete release workflow on 2026-09-17:

- Python: **289 passed, 1 sandbox-only loopback skip**; the installed loopback workflow passed separately.
- Frontend: **42 passed across 9 test files**; TypeScript checking and the 991-module production build passed.
- Ruff, Black, dependency consistency, Poetry lock validation, generated-contract equality, and package construction passed.
- Every member of both example archives was checked against its manifest: 307 Lausanne files and 154 San Francisco files.
- A Node-free installed-wheel check loaded the packaged browser assets and both example manifests. An empty installed workspace imported both examples, and the Project API reported each with `status="results"`.
- The documented Ctrl+C shutdown path exits successfully without a traceback and releases the coordinator lock.

The complete wheel contains 163 members: 26 browser files and four example manifest/archive files. Release SHA-256 values:

```text
30e7473b958a429dced024e79486fafcbe8e37f2251e24a08c98ec0b5be23776  mobile_sensing-0.1.0-py3-none-any.whl
b46749c044aa0ac1f4fa13d3f10e564deb2741915415217667325158c4a1270d  mobile_sensing-0.1.0.tar.gz
4522bc35a532d932fc0484f6a542123ccafc71f2f45bb0ac45345f3250385691  lausanne.zip
0b2429e7938dbc6162e9e91d4876798109e2779c63a67452e9c84b1e70132725  san-francisco.zip
```

The release files and complete checksum list are generated under `release/0.1.0/` by `scripts/prepare_release.sh`; that local staging directory is excluded from Git.

## Known limitations

- Local single-user operation only; no authentication, cloud service, or distributed queue.
- Public Overpass latency and availability are external dependencies for live OSM acquisition.
- macOS on Apple Silicon has the strongest browser validation. Linux coverage is narrower and native Windows launch is unsupported.
- Large city preparation and full example recomputation require substantial memory, disk space, and time.
- Large visualization chunks and first portfolio-map queries remain performance-maintenance areas.
- The locked frontend dependency graph currently reports deprecation notices for indirect `mumath` and `@plotly/mapbox-gl`; the application uses MapLibre for maps, and migration to a future Plotly major version requires separate compatibility testing.

Historical milestone reports and machine-specific acceptance evidence are archived outside the public documentation tree. Current regression baselines are test-owned fixtures under `tests/v2/fixtures/`.
