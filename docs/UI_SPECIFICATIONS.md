# User Interface Specification

The exponential saturation-duration field explains that local utility reaches 99% at the configured cumulative duration within each utility interval, with `u(d)=1-exp(-ln(100)*d/D)`. Capped-linear utility instead reaches 100% at its configured duration.

Selected portfolio displays mean spatial grid coverage from the backend window query: the fraction of exposure-domain cells with positive duration in each allocation round, averaged over all rounds. Zero-coverage cells remain in the denominator. The selected reporting window applies; saturation-map filtering does not change this metric.

Status: current public interface specification. It aligns with the immutable [simulation framework](MOBILE_SENSING_FRAMEWORK.tex). Architecture and endpoint names are owned by [ARCHITECTURE.md](ARCHITECTURE.md) and [INTERFACES.md](INTERFACES.md).

## 1. Product structure and terminology

Desktop-first local research application: **Environment → Fleet Configuration → Simulation → Portfolio Analysis → Results**. Use React, TypeScript, Vite, MUI, MapLibre GL JS, and Plotly.js; no Streamlit, Dash, or Panel. All displayed scientific values come from backend contracts/results. Forms and visual previews do not run scientific models implicitly.

The UI distinguishes:

- Operational vehicles: the fixed simulated population, configured in Supply.
- Instrumented vehicle counts: the portfolio count vector, configured in Portfolio.
- Operational replications R: retained mobility scenarios.
- Portfolio sampling rounds J: random joint-scenario/vehicle draws used to evaluate each count portfolio.
- Reporting interval: exposure aggregation, independent of simulation event timing.

Example fleet names are labels/presets. Every fleet uses the same Demand, Supply, Dispatch, and Routing forms. No special bus simulator page or hidden taxi engine.

## 2. Application shell

Persistent sidebar, approximately 232 px; lightweight header, approximately 56 px; primary content fills the remaining space. At 1440 px width configuration pages use a roughly 60/40 form/preview split; at narrower desktop widths the preview becomes a collapsible panel. Minimum supported target is 1280×800, with scrolling and accessible controls rather than clipped fields.

Sidebar routes:

| Item | Content |
|---|---|
| Project | Name, notes, revisions, input/output references |
| Environment | Study/routing regions, network, grid, clock and diagnostics |
| Fleet Configuration | Fleet selector plus Demand / Supply / Dispatch / Routing tabs |
| Simulation | Validation, execution settings, job history |
| Portfolio | Count enumeration, random sampling, budgets and analysis jobs |
| Results | Operations / Sensing / Portfolio |
| Examples | Configuration presets using the same forms |
| Data | Uploads, normalized datasets, diagnostics, artifact lineage |

Header: application/project name, active revision, dirty-state indicator, Save Revision, Open, Export Config, Help. Save creates an immutable revision. Results always show their source revision; editing the draft does not relabel previous results. A completed job remains accessible after navigation/reload.

Use neutral surfaces, compact headings, aligned controls and restrained color. Blue indicates primary/configuration, green valid/completed, amber warnings/waiting, red error/failed, gray inactive. Pair color with text/icons. Map fleet categories use distinct accessible colors, separate from status colors. Duration heatmaps use a perceptually ordered sequential scale with a displayed unit and legend.

## 3. Project and environment

Project supports new/open/save/duplicate and notes. The working directory is a managed artifact root, not an arbitrary server path entered into data endpoints. Show referenced datasets, source revision and recent jobs. No account, login, sharing, deployment, or permission screens in v1.

Environment sections:

1. **Region:** upload boundary or choose local Lausanne municipalities; show the dissolved sensing region. Future draw/bbox/geocoded selection can populate the same provider request.
2. **Routing extent and network:** local uploaded/supplied network; show extent separately from the sensing region. Travel times from validated upload or declared static speed model. Show nodes/arcs/components, one-way counts, missing/assumed speeds, topology errors, and source provenance.
3. **Grid:** uploaded grid or generated regular grid; resolution in metres, lattice origin/alignment, selected/intersecting cell count. Show working CRS and area fractions. Never imply a grid resolution in degrees is metres.
4. **Time:** service/reference date, origin and display timezone, simulation start, observation start, end; reporting-bin width or explicit edges. Allow after-midnight displays with date/day offset. The final bin may be shorter.
5. **Features:** optional population/year and weight-preview inputs, with missing coverage count and declared proxy status.

Actions: Preview, Validate, Prepare Environment. Each asynchronous action shows its own job state. Do not launch network downloads or rebuild geometry on every field change. Display a routing-region coverage error if selected GTFS trips require stops outside the available network; do not silently cut their sequences.

A source selector can show **OpenStreetMap — planned** as unavailable until the provider capability is implemented. Future controls reserve region selection, grid resolution, network mode, requested geographic features, cache/retrieval settings, attribution and source timestamp. Grid generation from a local region is available without an OSM connection. The offline example loads no remote map resources by default.

## 4. Fleet configuration

Keep the fleet selector and Add/Duplicate/Delete actions visible. Delete affects only the current draft and warns about draft references; persisted run catalogs remain immutable. A compact summary panel shows resolved modules, supply count/availability, constraints, and validation status.

### Demand

Controls: source (Upload / Generator / GTFS), task structure (Location / OD / Ordered), source adapter, parameters. Generation timing Offline/Online appears only for a generator. Installed capability schemas determine conditional fields.

Upload wizard:

1. Choose realized tasks versus rate-model parameters and source layout.
2. Upload file(s), inspect headers/sample rows, choose delimiter/layer if needed.
3. Map ID, time, location, duration, and quantity columns; explicitly declare CRS/timezone/units and defaults.
4. Preview proposed normalization and snapping on map/table. Show original and snapped points, missing values and invalid task groups.
5. Run full validation/normalization; strict errors block publication. An explicit quarantine policy shows accepted/rejected complete task counts and produces a downloadable issue report.
6. Save the normalized dataset reference and reusable mapping to the fleet draft.

A small preview is not full validation. Display `preview rows` versus `validated total rows/tasks` separately. Errors link to source row/task and mapped field. No spreadsheet formula execution or arbitrary transform expression field.

Generator controls: intensity intervals with units, spatial/OD weights, constant service duration and quantity, explicit source/seed preview information. Generate Preview uses a separate preview stream. Display synthetic-model labels and population as a proxy, not observed demand.

GTFS controls: feed reference, selected service date and routes, timezone resolved from feed, snap tolerance, duty source/grouping, turnaround time. Output preview shows active trips, full stop chains, actual inferred vehicle count, deadhead, lateness and rejected trips. Label inferred duties clearly; `buses.csv` is shown as a routes source. Completing reconstruction proposes linked Demand, Supply and Predefined assignment references in one reviewable draft update; do not silently replace another fleet's supply.

### Supply

Source: uploaded vehicles / generated population / GTFS duties. Show explicit operational vehicle count, individual windows, initial locations, and identity provenance. Generated settings include simultaneous or bounded distributed activation with explicit parameters. `Auto` resolves to a named source and a visible preview before execution.

Capacity: None / Consumable / Occupancy with units and per-vehicle or common capacity. Consumable mode exposes optional depot and replenishment time. Service areas have separate geometry and vehicle-assignment controls; show missing assignments and vehicles sharing a depot across areas.

Idle policy: Stationary / Random Cruise. Random Cruise help: “A random reachable neighboring node is selected. Service requests arriving during this short leg wait until it ends.” Advanced attraction/custom rules are absent or unavailable according to capabilities, not presented as working options.

### Dispatch

Only **Predefined** and **Nearest Matching** in v1.

- Predefined: assignment-plan dataset, task coverage, vehicle references, sequence and compatibility diagnostics.
- Nearest Matching: optional maximum pickup travel time in seconds/minutes. Explain centralized greedy nearest-pair matching, deterministic ties, and one task per currently idle vehicle. No radius field mislabeled as travel time and no claim of minimum-total-cost matching.

Show the conceptual inputs: waiting tasks + eligible idle vehicles → assignments. Capacity/area rules remain Supply-owned and can be linked from this page.

### Routing

Read the selected shared environment profile; choose among prepared supported travel-time profiles if more than one exists. Preview current node → ordered task steps and resulting directed network route, travel time, waits and service durations. Task order is retained. Editing a shared network/time profile occurs in Environment and invalidates affected mobility inputs.

Remove the old Scheduled/Sequential/Batch/One-Shot mode taxonomy and TSP/Auto sequencing controls. They are inconsistent with this first-release framework. Do not introduce unsupported options as disabled placeholders unless a capability explicitly describes the future extension.

## 5. Simulation execution

Configuration: R, master seed, reporting interval, and replication-worker count (default two). Worker count belongs on Simulation; there is no separate Settings page. Memory and timeout bounds remain managed implementation details. Mandatory retained outputs include movement intervals, operational records, and per-vehicle/per-replication exposure; do not offer an unchecked-by-default option that discards the scientific foundation. Explain sparse storage in help if needed, not in the main workflow.

Validate links to Environment, Demand, Supply, Dispatch, Routing and exposure settings. A Run action submits an immutable revision/input snapshot. Show queued/initializing/running/finalizing/terminal state, phase-specific counters, elapsed time, and estimated remaining time only when estimable. Replication completion drives mobility progress; exposure allocation is a separately identified phase/job after mobility completes.

Cancel submits a request, then displays Cancelling until durable cancellation is confirmed. A lost SSE connection displays Reconnecting while keeping the last known job state. Reconcile through the status endpoint; never declare failure because the tab disconnected. Reloading the page resumes the existing job. Failed/cancelled output is not offered as a completed portfolio input.

After completion, link Operations and Sensing, source revision, run and exposure IDs, R, diagnostics, elapsed time, and exports. `Run Again` preserves settings and creates an explicit request/cache result. Editing only sensor counts, budgets, J, utility, or map filters never triggers mobility simulation.

## 6. Portfolio configuration

Select one completed exposure artifact and show its simulation, fixed fleet catalog sizes, grid/bins/horizon, R, and sensing definition. State: “Each simulated vehicle's sensing-duration matrix is retained for every replication. Portfolio analysis reuses these records.”

Fleet table:

| Fleet | Operational vehicles (read-only) | Cost per instrumented vehicle | Minimum count | Maximum count | Count step |
|---|---|---|---|---|---|
| User fleet label | N_k | c_k | m_k | M_k | Δ_k |

Allow explicit count-level lists as an advanced alternative and display resolved levels. Do not set the simulated supply size with these controls. All physical vehicles are eligible for uniform random sampling; v1 exposes no manual best-vehicle picker, exclusions, vehicle-specific costs or optimization algorithm.

Separate sections:

- **Budgets:** explicit list or min/max/step, currency/abstract unit and resolved levels. Budget is a maximum expenditure.
- **Sampling:** sampling rounds J (default 100, configurable cap), sampling seed, and plain explanation of one joint replication draw plus uniform vehicle subsets per round. Show R and J side by side with distinct labels.
- **Utility:** exponential saturation, saturation seconds, weight source and evaluation domain; optional linear diagnostic mode.
- **Enumeration preview:** feasible count portfolios P, P×J samples, matrix storage/work estimate, limits and resolved assumptions.

Actions: Preview Enumeration → Evaluate Portfolios. Evaluation is disabled until preview/validation matches the current settings. Changing fields marks estimates stale, not a live recomputation request. If limits are exceeded, show specific ways to reduce counts/rounds or explicitly raise limits. Never silently coarsen resolution or drop sample matrices.

The frontiers summarize combined operational and unknown-installation variability. R=1 is permitted with a visible conditional-allocation label; J=1 cannot support a mean–std frontier. Do not imply J creates new mobility replications.

## 7. Results — Fleet operations and sensing

Persistent filters select run, fleet, optional vehicle, map layer, and observation time range. The primary result is the mean across complete replications; the ordinary UI does not expose a replication selector. KPIs distinguish released-window tasks from carry-in tasks, completed/rejected/interrupted/unserved, active vehicles, and sensing duration.

Map layers: boundary, network, selected task points/OD, selected vehicle routes, positions, depots and service areas. Initial state renders aggregated/filtered geometry; it does not load all trajectory rows. Vehicle positions are queried/interpolated from recorded intervals without rerunning simulation. Show the vehicle's state/time and the original/snapped task position when inspecting a discrepancy.

A table/timeline remains usable if WebGL is unavailable. A full animation is optional and lower priority than accurate time selection and replay inspection. Network geometry is static; changing time should not resend it unnecessarily.

## 8. Spatial coverage

Scope is one vehicle, one fleet's complete physical catalog, or all fleets. For each replication, sum the selected physical vehicles and all selected reporting bins first. Mean spatial grid coverage is the mean across replications of the fraction of road-intersecting grid cells with strictly positive accumulated duration anywhere in the selected time window. The denominator contains cells having a positive-length intersection with the prepared sensing road network; point-only contacts and roadless cells are excluded. A cell is counted once irrespective of visit time, repetition, or duration. Eligible zero-exposure cells remain in the denominator. Display the denominator count and this rule beside the percentage. Historical artifacts retain and identify their prepared-grid denominator.

Display duration seconds (optional display conversion to minutes), covered cells under an explicit threshold, total in-grid moving duration and observation domain. Normalization, if requested, must be named and units updated. Compare maps using locked scales by default; show whether a scale is local or shared.

Changing a display time filter is a query. Changing reporting bins/grid submits a new exposure job using retained movement, with the derived exposure ID and no mobility rerun. Aligned bin coarsening can reuse exposure; finer/unaligned bins require interval reallocation. The UI shows Pending until the new artifact is complete. Per-vehicle matrices remain accessible/exportable through explicit fleet/vehicle/replication filters even though physically stored sparsely.

## 9. Results — Portfolio

Primary visualization: mean utility on y and the configured risk statistic on x. Budget overlays use shared axes. Each point is a count portfolio summarized over J random draws. The default displays only nondominated points; a checkbox can reveal dominated evaluated portfolios. No frontier line is drawn.

Point inspector contains count vector, total cost, unspent budget, R/J, mean/std/SE/quantiles, utility distribution, and sensing matrix mean/std maps. A sample-round selector reveals selected joint replication, exact sampled vehicles, that sample's utility and matrix. Show these as sample details, not as an optimized recommended installation.

Duration heatmap legends display minutes while retained matrix values and exports remain in seconds. The portfolio mean map provides an explicit toggle that retains only cells whose displayed mean duration is strictly greater than the configured saturation duration. The selected-portfolio inspector reports the percentage of displayed space–time units satisfying the same strict threshold, with sparse certified zeros included in the denominator.

Coincident objective points open a tie list retaining all portfolios. The empty portfolio remains in underlying results, even if a visible toggle hides it. Connected frontier lines are explicitly a visual guide and not deployable interpolated mixtures. No single “best portfolio” is chosen without a user-specified preference model.

Exports include count grid and budget settings, sampling settings, count statistics, all sample utility records, selected vehicle recoverability, sparse sample matrices, source E matrices/references and frontier membership. CSV is suitable for filtered tables; Parquet and manifests are the complete scientific export.

## 10. Data, status and validation UX

Data catalog shows source/normalized type, size, checksum, created time, validation status, provenance and Used By links. Upload, inspect, download and delete-unreferenced actions map to backend contracts. Referenced immutable datasets cannot be removed accidentally; backend reports dependency conflicts.

Forms derive supported discriminators/parameter constraints from versioned backend schemas and use curated layout metadata. Do not generate an unusable raw JSON editor as the main experience or maintain a second scientific validation engine. Server field paths map errors to controls; cross-field errors appear in a linked validation panel.

Every page handles empty, loading, stale draft, validation error, failed job, cancelled job, disconnected progress and completed states. Disabled controls have a concrete explanation. Error messages preserve dataset/task IDs without leaking local stack traces. Keyboard navigation, focus restoration, labels, contrast and accessible tables are part of acceptance.

## 11. Map and chart performance

Use backend bbox/time/vehicle filtering, bounded GeoJSON responses, clustered task points and simplified display geometry. Preserve exact geometry/numerics in scientific artifacts. Never use a random map sample as a scientific aggregate. Responses identify incomplete/sample/aggregate visualization data and link to complete exports. Future vector tiles can replace large GeoJSON payloads without changing analysis contracts. These strategies are consistent with the [MapLibre large-data guidance](https://maplibre.org/maplibre-gl-js/docs/guides/large-data/).

MR15 defaults maps to Esri World Imagery with visible attribution, while keeping a local blank style plus boundary/grid/roads overlays available for offline use. A versioned presentation preference applies the new initial default once and remembers subsequent explicit choices. Failed imagery must not discard scientific layers. Online tiles are not a prerequisite for computation or offline result inspection. Charts are lazy-loaded and receive summaries/sample distributions, not full exposure tensors.

Scientific acceptance compares displayed values with backend exports. Visual testing checks the shell at 1280×800 and 1440×900, long IDs, invalid files, zero results, dense maps and tied frontier points. Performance targets are measured on a recorded machine: typical cached filtered queries should aim for <1 s, ordinary form responses <200 ms, with query caps enforced regardless of hardware. These are design targets until benchmarked.

## 12. Frontend implementation order

Build the shell and contract client after M01; then project/environment/import/fleet forms; then durable job monitoring; then Operations/Sensing; then random-enumeration configuration and budget frontiers. A page is complete only when connected to real application services and accepted result fixtures. Temporary development mocks must be explicit and absent from release behavior. Do not substitute a static dashboard for the end-to-end data upload and simulation workflow.
## MR33 temporal and spatial authoring controls

Simulation labels `temporal_resolution_minutes` as **Simulation reporting interval** and states that it controls exposure statistics and time-series charts. Portfolio separately exposes **Utility temporal interval** and states that exposure is summed inside this interval before nonlinear utility; it never changes the source exposure resolution.

Generated Demand origins/destinations and generated Supply initial locations use a repeated spatial-distribution editor. Each row selects a prepared feature and a nonnegative mixture coefficient. The editor states the evaluation order: normalize every feature across the full prepared grid, combine normalized layers, then apply routing eligibility. `uniform` remains an explicit selectable component. Environment lists source/prepared features only and does not materialize a fleet-specific activity proxy.

## MR34 service-area controls

Supply exposes one `Service areas` selector: no restrictions, uploaded areas and assignments, or Auto balanced expected demand. Uploaded mode shows the two named inputs. Auto shows an integer area count bounded by the physical catalog and explains that expected origin demand, areas and vehicle assignments are frozen before replications. The UI states that the depot does not assign an area and that an OD destination may cross its origin area.
