# Implementation status

## Current candidate

Source synchronization validation on 2026-10-29 passed 382 Python tests with one sandbox-only loopback skip, all 86 frontend tests, TypeScript checking, and the production browser build. Repository-wide Ruff, Black, and Poetry lock validation passed. Formatting-only changes resolve the two earlier Black exceptions without changing test logic; all seven focused tests passed. See [VALIDATION.md](VALIDATION.md) for the executed checks.

Heatmaps use the selected query's positive-value 95th percentile as the default colour upper limit, with Data maximum and manual overrides in displayed units. Python supplies optional presentation metadata for maps and matrix queries; all heatmap joins propagate that metadata. Clipping changes colour only, leaves source values and scientific results intact, and marks the upper legend endpoint explicitly. Manual changes preserve the map camera. Historical responses without the new metadata retain an explicit maximum-based fallback.

Colour-scale validation on 2026-10-23: 382 Python tests passed with one sandbox-only loopback skip; all 86 frontend tests, TypeScript checking and the production build passed. Regression coverage verifies exclusion of zero values, temporal aggregation before percentiles, original-row weighting before geometry expansion, metadata propagation, unit conversion and camera/source preservation. Chrome checks at 1440 px and 768 px passed with real MapLibre rendering. A live query against the saved San Francisco weekday result returned P95 2,911.201676 s versus maximum 877,622.469614 s; independent recomputation matched exactly. The app was reloaded after checking all five projects had no active jobs.

OD distribution files can be registered in Data and explicitly selected in Fleet Configuration → Demand → Spatial distribution. The selected file name replaces the inactive origin/destination feature controls. Existing demand-role and legacy feature-role references remain visible without changing saved configurations or scientific sampling. Registration does not activate a file; missing or incompatible selections fail explicitly. New cell-ID-only CSV/Parquet OD tables require no CRS, while coordinate and geometry inputs retain CRS checks. Active checks reject stale OD references outside generated OD demand; historical configurations and run records remain readable.

OD authoring validation on 2026-10-22: 344 Python tests passed with one sandbox-only loopback skip; all 69 frontend tests and the production build passed. Regression coverage includes registration without CRS, explicit file selection, draft reload, Run request persistence, stale-selection clearing, metadata-only input checks, inactive feature validation and historical record readability. Ruff and Black on the six Python files changed for this update passed. No scientific simulation rerun or saved-case migration was required.

Grid selection is available in Environment → Grid as addable rows with any/must logic for each spatial feature or the road network. Retained cells satisfy every must rule and at least one any rule when alternatives exist. Feature values must be positive; road overlap must have positive length. Python applies the same selection to previews, generated spatial support, exposure axes, utility normalization and coverage denominators. Legacy global any/all choices plus a required road condition retain their meaning. Full prepared inputs and routing roads remain immutable; historical runs retain their source domains. Additive contracts default to no filtering and preserve unfiltered mobility cache keys and exposure identities.

Per-feature rule validation on 2026-10-22: 376 Python tests passed with one sandbox-only loopback skip; all 74 frontend tests and the production build passed. An independent Boolean oracle covers all 81 off/any/must assignments over three spatial features and roads. Integration verifies previews, generated support, exposure, persistence, Run payloads and source-run portfolio summaries. Generated contracts, Ruff, Black on eight changed Python files, and diff checks passed. Chrome checks at 1440 px and 768 px verified mixed-rule editing without changing saved cases.

Grid-filter validation on 2026-10-21: the complete Python suite passed 317 tests with one sandbox-only loopback skip; all 53 frontend tests and the production build, including TypeScript compilation, passed. Coverage includes positive-length versus point-only road contact, road-only and combined selection, preview/support/exposure agreement, fixed catalogs, saved revisions, source-run portfolio axes and inactive-filter cache compatibility. Zero-coefficient feature layers remain valid when filtering removes all their mass. Ruff and Black on the nine Python files changed for this extension pass; repository-wide Black still flags two unchanged tests (`test_city_examples.py`, `test_example_asset_download.py`). No city-scale road-preview benchmark or new scientific simulation campaign was run.

Simulation now reuses canonical waiting-pool/fleet indexes, prunes heap lookahead, reuses complete immutable execution intervals, and avoids repeated canonical-identity normalization. Bounded reverse pickup trees, neighbor caches and 16,384-entry shared route caches reduce routing work; local route searches avoid city-sized label initialization and distance-dominated path copies. Idle and blocked-cruise diagnostics are coalesced per idle episode. Scientific event ordering, routes, movements, cutoff rules and semantic RNG streams are preserved.

Full-day Lausanne measurements on 2026-10-03 used unchanged unfiltered inputs, 153 vehicles (53 bus, 20 postal, 80 taxi), the original offline/online release modes, one worker, macOS 26.6.2 arm64 and Python 3.12.14. Each measurement started in a separate process with fresh in-memory routing caches; timings below exclude profiling overhead.

| Retained replication | Input tasks | Execution time | Heap events | Movement rows | Peak process RSS through execution |
|---|---:|---:|---:|---:|---:|
| `replication_0dacad5301016d615c0e4e02bc69db37`, first measurement | 7,073 | 56.946 s | 232,318 | 836,995 | 2,556.03 MiB |
| Same replication, independent repeat | 7,073 | 56.922 s | 232,318 | 836,995 | 2,557.48 MiB |
| `replication_152d8693a3ab57a2e1963e774d7b6e2b` | 7,066 | 56.789 s | 239,434 | 839,392 | 2,578.45 MiB |

The first replication previously took 238.561 s with full queue scans and 204.419 s after heap pruning. Its two new measurements average 56.934 s: 3.59× throughput and 72.15% less elapsed time relative to the previous candidate, or 4.19× throughput relative to the original scan. Its complete normalized result hash remains `6a7c3a274850c216b024c26dbe6f2b8bfdc97ec5fc0a551995b0770887985fa9`, and its seed hash remains `ee36d66dc4267f8fa2680f4002bbcc52720dd3417e60c16c2f865629b5a38f16`. All 224,974 execution outcomes match the original reference. The second replication produced 232,098 outcomes, result hash `37d4620f499694987ece7337c3ffb7d83dd4e0c4244f1c02b634650c98bb8eb6`, and seed hash `f61a9a15247a397944511aa7da7fcf871577a0a22b9be31a68aa80354734c680`; it is an additional timing sample, not a before/after comparison.

The timer includes adapter initialization, kernel execution and seed-manifest construction. Input resolution/loading, result hashing/serialization/publication and exposure allocation are excluded; peak RSS includes input/environment loading. Three measurements are below 60 s, but the configured 50-replication/four-worker workflow was not rerun and no universal per-replication deadline is claimed. Source resolution: `dataset_c05cbc6a35c482611abd50ffabd1219d`. Reproduce with `python -m scripts.validation.benchmark_queue_lookahead report/results/lausanne_workspace report/results/lausanne_workspace/example-run.json --variant current --hours 24`; add `--replication-index 1` for the second replication. `--profile PATH` writes execution-only profiling data. `--variant reference` restores only the former snapshot scan; other current optimizations remain enabled, so it does not recreate the original full implementation.

Complete exposure reuse now checks artifact bytes, completeness, configuration and dependencies through a disposable index before skipping allocation. A temporary index pointing to the immutable current 50-replication Lausanne exposure (12,290,631 sparse rows; 13,667 cells; 24 bins) took **0.174 s** to verify and reuse. The retained run records **568.914 s** for its earlier uncached exposure build; this change avoids repeated builds and does not claim to accelerate initial allocation by that ratio. Validation: 313 Python tests passed, one sandbox-only loopback test skipped; 49 frontend tests and production build passed. Focused coverage includes waiting-pool invalidation, idle-episode diagnostics, cruise retry after a new execution, full/fractional/tolerance-sized cutoff materialization, reverse routing bounds, independent lexical-route oracles and canonical scientific identity. Ruff passes. Black passes changed Python files; its repository-wide check flags two unchanged existing test files (`test_city_examples.py`, `test_example_asset_download.py`).

The portfolio overview now stacks the frontier and budget-series bars in one equal-width left column beside a full-height Selected portfolio inspector. Bar composition is hover-only; paired median and empirical percentile whiskers use a distinct accent color. The Lausanne report section and figures are regenerated directly from the retained 50-replication/200-round artifacts, with all configured budget frontiers and five representative duration maps. Frontend validation: 47 tests and production build passed; a Chrome capture documents the updated layout.

Portfolio results now support conditional single-fleet, partial-fleet and all-fleet comparisons. The API recomputes Pareto membership after constraining excluded fleet counts to zero. The result page adds a maximum-mean portfolio series by sensor count with selectable utility or spatial coverage and empirical median/P05/P95 uncertainty.

The Lausanne research example was recomputed and audited for bus lines 1, 9, 21, 33 and 54, 50 joint replications with four workers, and one P05 portfolio analysis with 200 allocation rounds and 230 feasible count portfolios. Budgets and fleet counts use five-sensor increments through 50; population weighting, full-day utility, five-minute saturation and unit sensor costs are retained. The complete simulation and analysis artifacts are retained locally under `report/results/lausanne_workspace/` and excluded from Git; the optional example-data release provides the checksummed 531-file archive in two parts because the complete ZIP exceeds GitHub's single-asset limit. A 50-replication exposure-memory failure led to per-replication movement/activity loading and a streaming canonical sparse-exposure hash; the original hash semantics and immutable artifact contracts were preserved. The full Python suite passed 292 tests with one skip; all 47 frontend tests, TypeScript checking and production build passed.

All portfolio result views hide the zero-budget category and its portfolios, including nested appearances under positive budgets. Scientific artifacts and exports remain unchanged.

Portfolio result charts connect nondominated markers within their lowest-feasible-budget color category. Lines and markers share color and legend visibility. Singleton categories remain unconnected, and no cross-budget guide is drawn. Existing scientific frontier membership is unchanged.

Exponential utility now uses the 99%-attainment duration (`sample-utility@4`), with synchronized scalar/indexed evaluation and cache invalidation. Both city examples are reanalyzed at five minutes: population weights for Lausanne and uniform weights for San Francisco. Historical utility artifacts retain their original version and values.

Selected portfolio now displays the backend-computed mean spatial grid coverage over allocation rounds for the selected reporting window. Frontend regression coverage distinguishes this mean from the union of positive mean-duration cells. All 42 frontend tests, TypeScript compilation and the production build pass.

San Francisco now retains both P05 and standard-deviation portfolio analyses at five-minute saturation, using the existing ten-replication Taxi run, 100 shared allocation rounds, and sensor budgets 10–100 in increments of 10. The earlier ten-minute analysis remains historical; the current example links only the two five-minute analyses. The updated bundle has 163 files, passes the city audit, and has been installed in the existing Web App workspace; both analyses and the five-minute map threshold were verified in the browser. Fifteen focused application tests pass.

Existing-workspace example upgrades now reuse manifests differing only in creation time, while checking all other manifest fields and every payload checksum and preserving existing bytes. Reference names are synchronized after the matching bundle is installed. Regression coverage includes prior installations, preserved manifests, and rejection of scientific-content conflicts.

The city-example refresh changes Lausanne to one combined Bus fleet plus Postal and Taxi, adds OSM commercial/public-service features, and retains separate P05 and standard-deviation portfolio analyses. San Francisco now contains only a 100-vehicle Taxi fleet and adds transportation, public-service and leisure features. Duration heatmap legends use minutes; portfolio maps expose a strict above-saturation filter and selected-portfolio at-least-saturation percentage. Both examples were recomputed with ten joint replications and 100 portfolio sampling rounds, audited, packaged, checksum-verified and imported into fresh workspaces. The Lausanne bundle contains 222 files and the San Francisco bundle 152 files. The Python suite passed 289 tests with one environment skip; the frontend passed 42 tests across nine files, TypeScript checking and the 991-module production build. Both default and reduced-scale executable notebook workflows passed with four rendered figures each.

Repository organization maintenance: templates use functional directories, Python tests are grouped by behavior, shared helpers live in `tests/support/`, and manual validation tools live in `scripts/validation/`. All 56 test modules and 290 collected cases are preserved after applying the old-to-new path mapping. Scientific fixture content and serialized identities are unchanged; the retirement record only updates references to relocated replacement tests. The migrated local Python suite passed with 289 tests and one sandbox-only network skip. Ruff, Black, lock consistency and Markdown link checks passed. CI selectors, notebook commands and release tooling use the new paths.

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

The complete wheel includes the built browser files and both example manifest/archive pairs. The authoritative checksums are published as `SHA256SUMS` alongside the release assets.

The application TypeScript configuration explicitly enables incremental checking alongside its build-info path. The frontend type check, all 42 tests, and production build passed again before publication. Both the workspace TypeScript compiler and the editor's bundled TypeScript compiler reported no source diagnostics.

The two raw Lausanne diagnostic regressions explicitly skip when the local `data/Lausanne/` directory is absent, matching the public repository's documented data boundary. Their original assertions still run when the directory is present; incomplete or invalid local datasets continue to fail.

The release files and complete checksum list are generated under `release/0.1.0/` by `scripts/prepare_release.sh`; that local staging directory is excluded from Git.

## Known limitations

Platform verification on 2026-09-17: [GitHub CI](https://github.com/EPFL-HOMES/mobile-sensing-lab/actions/runs/35205732264) passed both the Ubuntu suite/build and native Windows headless job. The Windows job installs Python 3.12 dependencies, imports scientific services, and runs the small single-worker environment/validation/simulation test. A separate local check blocked `fcntl` imports and passed that same headless test. Neither check establishes native Windows App, tutorial, or multiprocessing support. The [installation guide](INSTALLATION.md) distinguishes these paths and provides terminal-specific commands.

- Local single-user operation only; no authentication, cloud service, or distributed queue.
- Public Overpass latency and availability are external dependencies for live OSM acquisition.
- macOS on Apple Silicon has the strongest browser validation. Linux desktop and WSL2 coverage remain narrower. Native Windows supports the scoped headless path above; managed App launch and current tutorial adapters remain blocked by Unix-only file locking.
- Large city preparation and full example recomputation require substantial memory, disk space, and time.
- Large visualization chunks and first portfolio-map queries remain performance-maintenance areas.
- The locked frontend dependency graph currently reports deprecation notices for indirect `mumath` and `@plotly/mapbox-gl`; the application uses MapLibre for maps, and migration to a future Plotly major version requires separate compatibility testing.

Historical milestone reports and machine-specific acceptance evidence are archived outside the public documentation tree. Current regression baselines are test-owned fixtures under `tests/fixtures/`.
