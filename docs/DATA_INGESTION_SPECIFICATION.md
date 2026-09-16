# Data Ingestion, Lausanne Examples, and GTFS Reconstruction

Status: current public data-ingestion specification. It implements the data boundaries in [INTERFACES.md](INTERFACES.md) and feeds the common kernel in [SIMULATION_SPECIFICATION.md](SIMULATION_SPECIFICATION.md).

## 1. Local data audit

The following facts were inspected directly from file headers and GeoPackage metadata. Counts are raw source counts before spatial, calendar, and route filtering.

| Source under `data/Lausanne/` | Observed contents | Required adapter behavior |
|---|---|---|
| `boundary_lausanne.gpkg` | Layer `boundaries_swissBOUNDARIES3D_sim`, 28 named MultiPolygon rows, EPSG:4326 | Select explicit municipality names and dissolve; do not assume this is a single Lausanne polygon. |
| `grid_100m.gpkg` | Layer `grid_100m`, 17,553 polygons, EPSG:2056; easting/northing fields, no supplied `cell_id` | Create stable `easting_northing` IDs after uniqueness checks; preserve original geometry and selected boundary fractions. |
| `lausanne_roads_encoded.gpkg` | Layer `roads_encoded`, 54,517 MultiLineString rows, EPSG:4326; `u,v,key,one_way`, modal flags; no speed field | Validate topology and line continuity/orientation, project to metric CRS, apply only an explicit versioned repair/quarantine policy, retain lineage, and assign explicit baseline speeds. Raw bytes remain immutable. |
| Encoded road directions | 53,310 rows with `one_way=0`, 1,207 with `one_way=1`; `(u,v,key)` unique in inspected table | Decode booleans explicitly. Add a reversed arc only for two-way roads, with reversed geometry and stable distinct ID. All inspected rows have road=true and other modal flags false. |
| `population_swiss.csv` | `year,easting,northing,residents,households`; inspected rows use 2024 | Treat as population features, never observed task demand. Validate declared Swiss projected coordinates and year coverage before joining. |
| `gtfs/buses.csv` | 46 route records: `route_id,agency_id,route_short_name,route_desc,route_type` | This is a local alias for the routes table, not 46 physical buses. |
| `gtfs/trips.csv` | 31,546 trips across 46 routes; `block_id` present but empty in all 31,546 rows | No observed vehicle duty assignment is available; infer a stable synthetic duty catalog or accept a user-supplied assignment. |
| `gtfs/stop_times.csv` | Approximately 36 MiB; standard trip/stop/order/arrival/departure fields; times above 24 hours present | Stream/filter by retained trip IDs; preserve full stop order and service-day times. |
| `gtfs/calendar.csv`, `calendar_dates.csv` | Calendar and exception tables; exceptions approximately 111 MiB | Read selected columns/chunks and filter the chosen date before joining. Never load all exception rows repeatedly per route. |
| `gtfs/agency.csv` | Declares `Europe/Berlin` | Use the feed timezone when decoding GTFS; preserve it even when the UI displays Europe/Zurich. |

The road geometry's endpoint consistency, snapping success, active trips on a selected date, and routing feasibility were not measured during this architecture review. They are required outputs of M02/M04, not assumed facts.

## 2. Transactional import workflow

Use a common pipeline for UI, CLI, and Python:

1. Register immutable raw bytes, SHA-256, original filename, size, format, and source provenance.
2. Detect only structural properties (header, delimiter, layers, candidate columns), then request/resolve an explicit mapping. Detection is a proposal, not silent semantic conversion.
3. Validate mapping, units, CRS, timezone/origin, row keys, declared quantity meaning, and selected adapter version.
4. Normalize in chunks into staging tables with source row references. Resolve coordinates to canonical locations and network nodes against an identified prepared network.
5. Perform whole-dataset foreign-key/order/uniqueness checks, route diagnostics, and accepted/rejected task accounting. Chunk boundaries must not split a logical task's validation.
6. Publish canonical tables and a validation report atomically. A dataset is usable only when this phase succeeds under the selected error policy.

Default error policy is `strict`: any invalid task blocks publication. An explicit `quarantine_invalid_tasks` option may publish the valid subset, with rejected tasks, reasons, counts, and a changed content fingerprint. Quarantine the entire multi-step task if a required row fails; never silently drop a middle stop and connect its neighbors. Extra unused source columns may be retained as provenance, but unknown normalized schema fields are rejected.

The importer must distinguish `observed_tasks` from `rate_model`. An observed OD request is one realized task. An OD rate row is a parameter for a future random process. A quantity of 5 is not five tasks unless an explicit expansion adapter requests that interpretation.

Initial supported formats: UTF-8 CSV and Parquet for task/vehicle/assignment data; GeoJSON and GeoPackage for geometry; standard GTFS text files in a ZIP/directory and the documented local CSV aliases. Excel import is deferred; offer CSV templates. ZIP extraction enforces entry/path, expanded-size and file-count limits. No paths outside managed staging, symlinks, executable imports, or runtime code evaluation from upload fields. Upload byte limits are configurable; initial defaults should allow the bundled GTFS feed (1 GiB compressed input, 2 GiB expanded total, with a visible estimate and configurable local override). Stream files to disk rather than holding them in API memory.

## 3. Demand upload schemas

The UI wizard selects a source layout and maps its columns into the following logical fields. Files need not use these exact source names; exported templates do.

| Adapter | Required fields | Optional fields / normalization |
|---|---|---|
| Location task, one row/task | `task_id,release_s,x,y` with CRS, or a valid location ID | `service_duration_s,quantity`; produce one step, consumable quantity as an explicit negative capacity delta |
| OD task, one row/task | `task_id,release_s,origin_x,origin_y,destination_x,destination_y` with CRS, or paired location IDs | pickup/drop-off durations, quantity; produce two ordered steps; occupancy quantity becomes `-q,+q` when occupancy mode is selected |
| Ordered task, long form | `task_id,step_index,release_s,location_id` or `x,y` | target time, service duration, signed quantity delta; release must agree across rows; normalize positive contiguous order without silently renumbering duplicates |
| Canonical two-table tasks | Tasks table + steps table using the interface keys | Strict FK and order validation; preferred lossless interchange/export |
| Sparse OD-rate model | interval start/end, origin/destination location IDs, nonnegative rate and explicit rate unit | Normalize rate to tasks/second; nonoverlapping intervals per OD key unless explicit aggregation is selected |

The fleet is selected during import or supplied per row with an explicit mapping. IDs are strings, preserving leading zeros and punctuation. Generated fallback task IDs use immutable dataset fingerprint plus logical source record index; reordering an ID-less source changes its identity and must be disclosed. Explicit-ID inputs are stable under row reordering.

Time mapping supports elapsed seconds, timestamp with timezone/offset, or clock time plus service date/timezone. Rate tables require rate units and interval width. Missing release time is allowed only with an explicit constant mapping (e.g. all deliveries released at 08:00). Service duration defaults to zero only when the user-visible adapter configuration declares that default. Never infer a timezone from the workstation.

Validate finite coordinates, geographic bounds where applicable, known CRS, nonnegative durations, release/target consistency, valid sequence, compatible quantity units, and locations against snap tolerance. Geometry preview includes original-to-snapped displacement and rejected points. Longitude/latitude swapping and metre/degree errors must produce targeted diagnostics rather than automatic repair. Network disconnectedness is reported separately from geometric distance.

Warnings such as outside observation window, implied zero-length OD, duplicate-looking source records with different IDs, or large lateness do not disappear from the manifest. Exact duplicate logical keys are errors, not silently deduplicated rows.

### Backend requirements beyond file parsing

The demand-upload milestone is complete only when persisted mappings can be reapplied, chunked normalization is equivalent to whole-file normalization, errors retain source rows, uploads link to fleet configuration through dataset IDs, and a normalized upload runs through the same kernel as generated/GTFS tasks. A frontend upload button or an hourly rate reader alone does not satisfy this requirement.

## 4. Supply and service-area uploads

Vehicle rows require vehicle ID, start/end, and initial location; optional depot, capacity, and capacity mode are fleet-consistent. Separate area geometry and mapping tables use `area_id` and `(fleet_id,vehicle_id,area_id)`. Do not infer assignments from initial position or shared depot. Validate every referenced location and reject conflicting duplicate vehicles or overlapping shifts for a single physical vehicle. Multiple daily shifts are deferred; do not represent them as different sensor candidates unless they are actually different vehicles.

Generated supply first creates stable IDs and a fixed catalog. Simultaneous shifts and uniform bounded activation are baseline sources; duration is constant or a declared bounded model, clipped to the experiment execution interval only with provenance. `Auto` in the UI must resolve to a named concrete method and show its result, not remain a magic runtime value.

Generated Voronoi/polygon service areas are a provider extension. v1 can use uploaded polygons or explicit grid-cell groups. Generated examples may assign areas deterministically from an explicit region rule; record it. No arbitrary partition inferred from a depot coordinate.

## 5. GTFS conversion and physical-vehicle reconstruction

### 5.1 Standards boundary

Support `agency`, `routes`, `trips`, `stops`, `stop_times`, and calendar/exception tables required for the selected service. Standard files use `.txt`; the Lausanne adapter explicitly aliases `buses.csv → routes`, plus the other CSV files. Reject ambiguous duplicate aliases. Keep identifiers as strings.

Apply the selected date's weekday calendar and date range, then add/remove services using calendar exceptions. Keep every active selected-route trip, not the one service ID with the most trips. Parse hours above 24 without modulo arithmetic. GTFS times use the service day's local noon minus 12 elapsed hours; construct that origin with timezone-aware conversion before adding parsed seconds. These requirements follow the [GTFS Schedule Reference](https://gtfs.org/documentation/schedule/reference/). Preserve the source timezone and test DST transition dates.

Use one service date per v1 reconstruction. This is a service-day experiment, not automatically a complete civil-day collection of spillovers from neighboring service dates. If a user requests civil-day completeness, detect the need for adjacent service dates and reject that unsupported import scope until multi-service-day merging is implemented; never silently omit preceding-day after-midnight service. A single service day may legitimately extend past 24:00.

`frequencies`, continuous pickup, linked-trip/transfer vehicle semantics, and missing required intermediate stop timing are not silently flattened. Initially detect these features and return explicit unsupported-capability diagnostics if selected trips depend on them. Missing arrival/departure may be copied from the other when the standard allows an instantaneous stop and the mapping declares it; arbitrary interpolation of absent times is deferred. Shapes, if supplied, may be retained as reference geometry for map comparison; v1 motion remains network shortest paths.

### 5.2 Normalize trips before spatial cropping

Filter by service date and selected route IDs first, then retain the complete ordered stop sequence of each selected trip. Do not remove stops outside the sensing polygon and join the remaining stops. Require network coverage for every needed leg, including deadheads, or reject/quarantine the whole affected trip under the selected policy. The routing region and sensing region are separate selections.

Check stop FKs, strictly increasing sequence, arrival ≤ departure at each stop, and nondecreasing scheduled time. Equal successive scheduled times at distinct stops are retained as timetable infeasibility evidence; the executor propagates lateness using positive network travel time rather than deleting the leg. Repeated visits to a stop are valid if sequence positions differ.

Resolve each unique stop once per network/snap policy; cache unique directed stop-pair routes. Convert a trip to one ordered service Task. Its service-time tuple follows the generic executor: scheduled arrival target and dwell equal to scheduled departure minus arrival. The nominal execution estimate uses exactly that executor's timing semantics, so duty feasibility and simulated timing do not use incompatible models.

### 5.3 Vehicle identity hierarchy

1. A validated user-supplied physical vehicle assignment is preferred; retain external vehicle ID and source.
2. A nonempty `block_id` is a service-day duty reference. Validate temporal/deadhead feasibility and preserve it as `identity_provenance=gtfs_block`. It is not proof of a globally persistent real vehicle registration across dates.
3. Without assignments/blocks, infer synthetic duties using the deterministic procedure below and mark `identity_provenance=inferred_duty`. Lausanne requires this path because all inspected block IDs are empty.

Never treat route IDs, trip IDs, or vehicle counts as physical vehicle identities. A vehicle may execute many trips and possibly multiple routes if the allowed duty-group configuration permits it. A single route normally needs multiple vehicles. Freeze the inferred catalog once before Monte Carlo replications; do not reshuffle which duty an instrumented vehicle represents in each replication.

### 5.4 Baseline duty inference

Compute each trip's nominal earliest executed finish \(e_i\) and first service-start target \(s_i\) under the common routing/dwell model. Within each declared compatible duty group, process trips sorted by `(s_i,trip_id)`. An existing duty ending at stop \(d\) and time \(e\) can accept the trip starting at \(o_i\) when

\[
e+\tau_{\mathrm{turnaround}}+\operatorname{TravelTime}(d,o_i)\le s_i.
\]

Among feasible duties choose minimum deadhead travel time, then minimum idle gap, then stable duty ID. If none is feasible, create a new duty. This is a transparent heuristic; do not claim a minimum fleet count. Default groups are route IDs, because cross-route compatibility is not known from this local feed. A user-supplied grouping may permit cross-route chaining.

The existing framework represents all prescribed movements as tasks. Emit a generic reposition/deadhead task between trips when needed, including a stationary turnaround step of the configured duration before movement. Assign trip and reposition tasks in a predefined order. All tasks in a known duty may be released at its activation time; scheduled targets still prevent early service. Task availability being known in advance does not make the vehicle active before its own entry.

Set duty activation to the first trip's scheduled first arrival at its first stop, unless an explicit depot/earlier start is supplied. Set end to the nominal executed finish of its final task; retain the full duty even if the observation window starts later. No depot trips are inferred without depot data. IDs derive from duty grouping and canonical ordered trip IDs, and are stable for the same input/date/parameters. Different route/date/network/duty parameters may legitimately produce a different catalog and cannot be merged as if the same physical vehicles were observed.

Publish tasks, steps, vehicle catalog, availability, predefined assignments, stop-snap diagnostics, active-service/trip counts, inferred duty chains, deadhead distances/times, route failures, scheduled versus executed lateness, and retained/rejected source counts. For every accepted selected trip, exactly one service task and one duty owner must exist.

### 5.5 How to use the reference

`ref/GTFS-reconstruct/Function_project.py` is a notebook export with import-time file reads/global configuration. Read it for the trip-cleaning → duty construction → road-grid exposure workflow. Independently implement the required steps in the new namespace.

Do not preserve its largest-trip-count service-ID selection, average-trip-time relocation multipliers, automatic online network retrieval, silently skipped route pairs, or allocation normalized by only the retained in-grid path length. Those are incompatible with calendar-complete imports, explicit shared routing, and duration conservation. Its combinatorial bus-selection experiments are conceptual reference only; the new portfolio module evaluates the common replication exposure artifact.

## 6. Geographic feature and network providers

Reserve the following provider request: region as uploaded polygon, drawn polygon/bbox, or future geocoded administrative selection; target metric CRS; grid cell size and alignment origin; network mode; requested geographic features; routing buffer/extent; retrieval cache policy. Result: raw geographic bundle with boundary/network/features, source CRS, original query, fetch time, checksums, provider version, attribution and any source coverage limits.

Available v1 providers: local files and a generated regular grid. The latter constructs square cells in a metric CRS with declared side length and grid origin; stable IDs use lattice indices plus the grid-definition hash. Keep cells intersecting the study area and store their intersection geometry/fraction for sensing. Changing grid size never rebuilds mobility if normalized task locations/network are unchanged. Uploaded regular grids keep their supplied alignment rather than silently regenerating it.

Supplied road preparation defaults to `strict@1`. `conservative_repair@1` merges ordinary contiguous records, splits only connected bidirectional two-terminal component linework at existing endpoints, and separates reused source node IDs with deterministic diameter-bounded coordinate clusters. `quarantine_invalid@1` does not reinterpret non-mergeable linework. Both non-strict policies retain complete repair/action/node/edge lineage and quarantine tables. A derived network with quarantined rows is `scenario_ready_with_quarantine` only after all finite directed node pairs required by the declared scenario are reachable; this label is not a claim that omitted roads are irrelevant to another scenario.

Reserved provider: `osm`. A future job retrieves roads and selected OSM features for the chosen region and routing extent, using an OSM adapter such as OSMnx behind `EnvironmentProvider`. It must support query caching, retrieval cancellation/timeouts, rate-limit-aware retries with a bound, explicit network-mode filters, projection/topology normalization, and source attribution. Features need declared tag-to-feature mappings and timestamps; absence of a POI is not evidence of zero demand. Every retrieved snapshot enters the same raw-dataset/validation pipeline as an upload.

No OSM implementation, API key workflow, geocoder, or online fetching is required for the first release. Advertise `available=false` for the reserved capability; UI can show it as planned without allowing a fake run. Later addition must not alter the Task kernel or create an online-only example. Grid generation and arbitrary uploaded-region selection are useful local v1 capabilities independently of OSM.

The OSM provider uses OSMnx behind a bounded acquisition adapter with normal DNS, alternate endpoints, complete-response caching, adaptive subdivision, request-admission limits, and explicit failure diagnostics. Public Overpass requests remain serial. A failed acquisition publishes no partial network; a local registered road file is the reproducible offline alternative.

## 7. Lausanne demonstration specification

Provide two configuration-only examples using the common modules:

- `lausanne_smoke`: one selected municipal region, retained local routing extent, 100 m grid, a two-hour observation window, two replications, a bounded GTFS route/trip selection plus small synthetic location-service and OD fleets, and a coarse portfolio count grid. Initial candidate date is 2026-01-14; verify actual active service before saving the example. Record the final selected route IDs explicitly rather than choosing a different route on each execution.
- `lausanne_research`: user-selected municipality set, complete selected service-day duties with a declared observation horizon, configurable generated fleet populations/demand intensities, reporting bins, replications, and enumeration granularity. Do not launch the full-size example as a unit test.

Use population-weighted synthetic logistics destinations and an explicit simple OD generator as examples. Population aggregation uses the declared year and cell alignment. For the supplied 100 m lattice prefer exact easting/northing joins after validating alignment; for a new grid, use a documented spatial allocation rule and report lost/unmatched mass. Missing population is distinct from observed zero until the user chooses a missing-data policy. If all usable weights are zero, require an explicit uniform-proxy choice rather than silently changing the model.

Supply sizes and task intensity are configurable illustrative values, not estimates inferred from population or trip count. Costs are user-declared illustrative costs. Label the demo `synthetic_demand`, `inferred_gtfs_duties`, and `assumed_static_speeds`. Provide source/count/coverage diagnostics beside the example; no claim of observed operations, sensing validity, or policy recommendation follows from successful execution.

## 8. Acceptance gates

- CSV/Parquet imports yield identical canonical tasks; mapping replay and chunk-size changes preserve records.
- Invalid task rows are traceable; quarantine removes whole tasks and changes the manifest/counts.
- Calendar exceptions add/remove service correctly, leading-zero IDs survive, >24-hour and DST times are verified, inactive services are excluded.
- Trips crossing the sensing boundary retain all operational stops or fail explicitly for missing network coverage.
- Duty inference is stable under source-row shuffling; no trip duplication, impossible chaining, route-as-vehicle identity, or unrecorded deadhead occurs.
- Generated grids have deterministic IDs/alignment and valid nonoverlapping interiors; projected metric cell sizes are checked.
- Provider output contracts can accept a synthetic in-memory geographic bundle with OSM unavailable; no network call occurs in the offline smoke.
- New upload → normalized dataset → common simulation → exposure → portfolio path runs without importing `ref/` or legacy simulators.
