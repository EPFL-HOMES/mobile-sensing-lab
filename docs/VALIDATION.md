# Release validation

This document records the checks executed against the current source tree. It distinguishes automated verification from scientific assumptions and platform coverage.

## Source synchronization checks, 2026-10-29

The complete Python suite passed 382 tests with one sandbox-only loopback skip in 117.22 s. All 86 frontend tests across 11 files and TypeScript checking passed. The production browser build passed with 994 transformed modules; the existing large-chunk warning remains. Generated-contract equality was checked by the Python suite.

Repository-wide Ruff, Black across all 220 Python files, Poetry lock validation, and `git diff --check` passed. The two previously recorded Black exceptions were resolved by formatting only: both test modules retain identical parsed Python syntax trees, and all seven focused tests passed. No scientific configuration, retained result, or report was changed. The public source update includes implementation, regression tests, generated contracts, and documentation; local research data and validation scratch artifacts remain outside Git.

Wheel and source-distribution builds passed from a temporary public-source copy containing the production browser assets and excluding local research files and example ZIPs. Both packages contain the three new Python modules and all 27 browser files with matching hashes. Offline installation without dependency resolution, imports from the installed package, and packaged frontend validation passed. Temporary build and installation directories were removed after verification.

## Heatmap colour upper limits, 2026-10-23

Executed checks: `.venv/bin/python -m pytest -q tests` reported 382 passed and one sandbox-only loopback skip in 113.13 s. All 86 frontend tests across 11 files passed; TypeScript checking and the production build passed. Repository-wide Ruff, Black on the nine Python files changed for this update, and `git diff --check` passed. Generated-contract equality passed within the Python suite; the new presentation fields extend raw query responses and require no schema migration.

Backend checks cover positive-value linear percentiles with zero cells and an extreme outlier, empty/non-finite domains, original source-row weighting before multipart geometry expansion, filtered source queries, per-cell temporal aggregation before percentiles, matrices and road-activity values. Frontend checks inspect actual paint stops, preserved raw source data, minute conversion, custom/maximum/automatic modes, invalid and overflowing input retention, no camera/source reload on scale edits, context resets, and matrix metadata surviving the Portfolio saturation-only display filter.

Chrome exercised the production assets with read-only fixtures and real MapLibre rendering at 1440 px and 768 px. Seven screenshots and checks verified the selected-query P95 rather than a bounded-feature estimate, shared polygon/point/line caps, capped legends, matrix metadata overriding stale grid metadata, and road activity. No unexpected requests or scientific mutations occurred; both calibrated case settings hashes were unchanged. Evidence is in `outputs/heatmap_color_scale_20261023/`.

After confirming that all five workspace projects had no active jobs, the managed app was reloaded on port 8820. The live San Francisco weekday query returned 10,244 positive cell values, P95 2,911.201675914507 s and maximum 877,622.4696141661 s. Independent NumPy calculation from the returned values matched the metadata exactly; the cold query took 10.526 s and submitted no scientific jobs. Existing dependency deprecation and frontend large-chunk warnings remain. Scientific artifacts and saved case configurations were not migrated.

## Per-feature grid-filter rules, 2026-10-22

Executed checks: `.venv/bin/python -m pytest -q tests` reported 376 passed and one sandbox-only loopback skip in 107.98 s. All 74 frontend tests across 11 files and the production build passed, including TypeScript compilation. Generated OpenAPI/TypeScript equality and canonical contract fixtures passed within the Python suite. Repository-wide Ruff, Black on the eight Python files changed for this update, and `git diff --check` passed.

An independent Boolean oracle checks all 81 off/any/must assignments over three features plus roads against all 16 condition truth combinations. Coverage includes road-OR rules, mixed mandatory and alternative conditions, positive-length versus point-only road contact, invalid and duplicate rules, legacy projection byte shapes, artifact payload compatibility, preview/check/support/exposure agreement, and unchanged routing/catalog boundaries. Frontend integration covers add/remove and per-row logic, legacy display without implicit mutation, draft persistence, submitted Run parameters and Portfolio's source-run summary.

Chrome checks against the production build used a read-only synthetic feature fixture and mocked preview counts at 1440 px and 768 px. Mixed rows, road logic changes, duplicate prevention, removal and layout passed; source settings hashes were unchanged. Local screenshots and the browser record are in `outputs/grid_filter_rules_20261022/`. These checks did not contact the live backend or run a scientific campaign. Existing dependency deprecation and frontend large-chunk warnings remain; repository-wide Black exceptions recorded below are outside this update.

## Explicit OD data selection, 2026-10-22

Executed checks: `.venv/bin/python -m pytest -q tests` reported 344 passed and one sandbox-only loopback skip in 102.97 s. All 69 frontend tests across 11 files passed; the production build passed TypeScript compilation and bundled 994 modules. Generated contract equality passed within the Python suite. Repository-wide Ruff, Black on the six Python files changed for this update, and `git diff --check` passed.

Regression checks cover demand-role OD files in Data and Fleet, CSV/Parquet compatibility, explicit selection, draft reload, the submitted Run configuration, file registration without implicit activation, clearing stale selections, missing/incompatible references, and read-only controls. Backend checks verify cell-ID-only registration without CRS while preserving coordinate/geometry requirements, metadata-only configuration validation, both inactive spatial mixtures being ignored, and execution guards before cache reuse. Historical configurations and saved run views remain readable.

Chrome checks used the production build with read-only API fixtures from the actual saved San Francisco weekday/weekend case settings and input metadata. At 1440 px and 768 px, Data classified both files as OD distributions, Fleet displayed the correct selected file, inactive feature editors were hidden, and independent mode displayed both editors with a readable selector label. The full filename remains visible below the narrow selector. No mutation requests occurred; source-file hashes were unchanged. Local screenshots and the verification record are under `outputs/od_input_ui_20261022/`. These checks did not contact the live backend.

This update retains the existing OD field, file roles and joint-pair sampling algorithm. It does not migrate case data or rerun scientific results. The frontend retains its existing large-chunk warning; Python reports dependency deprecation warnings. The repository-wide Black exceptions recorded below remain outside this update.

## Grid-filter extension, 2026-10-21

Executed checks: `.venv/bin/python -m pytest -q tests` reported 317 passed and one skipped in 109.67 s. The skip is the loopback-socket test disallowed by the sandbox. All 53 frontend tests passed, and `npm --prefix frontend run build` passed TypeScript compilation and production bundling. Generated OpenAPI/TypeScript equality and canonical contract fixtures passed within the Python suite. Repository-wide Ruff, Black on the nine Python files changed for this extension, and `git diff --check` passed.

The regressions verify road-only and feature-plus-road filters, positive-length intersections excluding point contacts, consistent preview/support/exposure domains, project persistence, source-run portfolio axes, unchanged routing/catalog identities and default-off compatibility. A zero-coefficient feature whose retained mass is zero no longer invalidates the mixture. Residential geometry remains a proxy; positive population depends on the supplied data.

Remaining validation limits: repository-wide Black flags two unchanged tests (`test_city_examples.py`, `test_example_asset_download.py`). The frontend build retains the existing large-chunk warning. The Python suite emits dependency deprecation warnings. No new city-scale road-preview timing or scientific replication campaign was performed.

## Automated checks

The release gate includes:

- the complete Python suite in `tests`;
- frontend unit and interaction tests;
- TypeScript checking and the production browser build;
- Ruff, Black, dependency consistency, and Poetry lock validation;
- wheel and source-distribution construction;
- generated OpenAPI/TypeScript equality and canonical fixture checks;
- archive inventory and checksum verification for both example bundles.

For the 0.1.0 candidate, Python reported 289 passed and one sandbox-only loopback skip; the installed loopback workflow passed separately. Frontend testing reported 42 passed across nine files. TypeScript, production build, Ruff, Black, dependency checks, Poetry lock validation, and wheel/source-distribution construction passed. Tests requiring non-public Lausanne source data remain distinct from checks available in a clean source checkout.

## What the checks establish

On 2026-09-17, [Ubuntu and native Windows CI](https://github.com/EPFL-HOMES/mobile-sensing-lab/actions/runs/35205732264) passed. Ubuntu executes the public-source Python suite (284 passed, six unavailable-local-data skips), 42 frontend tests, type checking, source checks, and package builds. Windows installs the Python package with its optimization extra, checks scientific imports, and runs the single-worker headless environment/validation/simulation test. This is scoped Windows Python evidence, not certification of the App, bundled notebooks, all scientific workflows, or multiprocessing. WSL2 end-to-end execution has not been tested.

Automated tests cover contract strictness, semantic random streams, event ordering, directed routing, supply/catalog stability, task outcomes, exposure conservation, portfolio sampling, nonlinear utility, statistics, job recovery, bounded queries, project portability, and browser interactions.

Passing tests do not calibrate synthetic operating assumptions, prove global optimization, guarantee public Overpass availability, or establish city-scale performance on untested hardware.

## Release assets

Release assets must come from the same source candidate and include SHA-256 checksums for the wheel, source distribution, and both example manifest/archive pairs. A clean installation must start without Node.js from the wheel, import both examples into an empty workspace, open retained results, duplicate an example, and complete a bounded new run and portfolio analysis.
