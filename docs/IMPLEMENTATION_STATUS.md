# Implementation status

## Current candidate

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
