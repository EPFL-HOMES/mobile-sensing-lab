# Interfaces and Data Contracts

This document defines current serialized names and compatibility behavior. The introductory entries record additive contract evolution; scientific meaning is owned by the simulation, data-ingestion, and portfolio specifications.

MR28: `ProjectResolutionResult.artifact` is optional. `validation_level` is `configuration` for the lightweight workbench Validate action and `resolved` for historical complete resolutions. Configuration checks never certify realized task counts, inferred vehicle counts, route feasibility or a solved plan; `reports.deferred_checks` names the remaining Run stages. The existing `studio_resolve` job kind remains readable, with operation version `configuration-check@1` preventing reuse of old complete-resolution jobs as configuration checks. Headless `resolve_project` and Run retain full validation and immutable publication.

MR29–MR30: `preparation-stages@1` stores bounded, checksummed compressed JSON per stage and scientific input identity. Cache reads restore typed values, added resolved locations and validated semantic RNG records; failures recompute from original inputs. `exact-directed-travel-times@1` shards exact route totals by network hash, speed-profile hash and source node, with explicit null for unreachable targets. Only missing ordered pairs are searched. Source shards use atomic replacement and per-source POSIX locks; each decoded shard is limited to 16 MiB and stage entries to 128 MiB. `RunOptions.workers` also bounds concurrent source searches, capped at four and one per 512 MiB allowance. These bounds are working-set estimates, not an OS RSS limit. No new scientific configuration field or artifact rewrite is introduced. Caches are optional local acceleration data, excluded from portable scientific dependency closures.

MR27 packaging extension: example/project archives may include bounded `analysis_cache/` receipts only when every referenced immutable result is in the packaged dependency closure. Missing caches affect speed, never scientific completeness. The current Lausanne bundle links exactly one simulation run and one analysis; upgrades replace the current reference links instead of merging historical example lists.

MR26 performance extension: `PortfolioConfig.sensing_statistics_mode` accepts `eager` (historical/default headless interpretation) or `on_demand` (new web/tutorial analyses). `portfolio-analysis-parquet@3` adds this field to analysis metadata; on-demand analyses retain complete scalar utilities/statistics/frontiers, sample selections and the exposure dependency, while the optional cell/bin statistics table is empty and explicitly not certified as a zero matrix. Bounded matrix queries compute requested statistics from retained selections. Readers continue accepting storage versions 1 and 2. `sample-utility@2` permits bounded indexed fleet-prefix aggregation and vectorized pointwise utility with deterministic float64 reductions; random selections and within-sample nonlinear semantics are unchanged. The reference fsum path remains available for materialized or working-set-limited cases. Numerical equivalence is tested separately from byte identity.

MR24 desktop startup: `GET /api/v1/workspace-info` identifies a workspace hub with `application="mobile-sensing"` and its canonical absolute `workspace` path. The double-click bootstrap reuses a local server only when both match; it never stops an unrelated server. The private workspace-control `server.json` records the last port, workspace and PID as a discovery hint, not proof of liveness. Scientific identities and generated configuration contracts are unchanged.

MR25 prepared-network display: the environment preview includes every directed road's geometry, deduplicates exactly coincident reverse geometry for display, simplifies individual lines by at most two working-CRS metres while retaining endpoints, and merges degree-two line chains. GeoJSON coordinates are rounded to six WGS84 decimal places and grouped into bounded MultiLineString features. `preview_road_count` equals the full represented directed-edge count; `display_line_count`, `simplification_m` and `display_geometry_version="complete-road-lines@1"` describe presentation only. The configured map byte/feature limits remain enforced. Stored routing geometry, directed identities and disconnected components are unchanged.

MR17 additive extension: `PortfolioEditor.risk_metric` and `PortfolioConfig.risk_metric` accept `std` or `p05`. Missing historical fields resolve to `std`; new web defaults and the revised Lausanne example explicitly select `p05`. `ObjectiveComparisonResolution.p05_utility` defaults to 1e-12. New analysis storage `portfolio-analysis-parquet@2` records `risk_metric` in metadata and `risk_comparison_key` in budget memberships, retaining the original standard-deviation keys. Readers accept both storage versions without rewriting old artifacts. `PortfolioFrontierView.risk_metric` identifies the immutable saved objective; `PortfolioFrontierPoint.risk_comparison_key` is optional for historical results. `quantized-budget-frontier@2` maximizes mean/P05 or maximizes mean and minimizes standard deviation according to that field. P05 is the empirical linearly interpolated 0.05 quantile. Scientific schema 2.0 and authoring schema 3.1 gain backward-compatible optional fields; old artifacts retain raw identity and are never relabeled.

MR11 interface refinement: `GET /api/v1/portfolio-frontiers/{analysis_id}` now has the generated `PortfolioFrontierView` response. Each point additionally contains `cost_by_fleet` and `total_cost` in its saved budget's cost unit, calculated from the immutable analysis configuration; existing integer costs and exact utility fields remain unchanged. Data registers new population and weight files as `feature`; legacy `population` and `weight` roles are accepted aliases in spatial and sparse-OD readers. No artifact or ProjectConfig migration is required for these presentation changes.

For the current authoring workflow, `ProjectRecord.status` is a derived display state. `WorkspaceInfo` exposes the active directory and initial example job; the default CLI launch root is `project/`. `project-folder@1` indexes reference shared root-relative immutable artifacts.

Authoring 3.1 adds `SimulationEditor.time_mode` (`relative`, `weekday`, `calendar`), `weekday`, optional calendar period bounds and `warmup_hours`; the resolved `service_date` is retained in executed records. `DemandEditor.temporal_mode` is `window`, `shares` or `rates`; `time_profile` contains `start_time`, `end_time`, `value`, with an optional `time_profile_input` table using the same columns. `ShiftGroup` partitions generated physical vehicles by count, local start/latest-start, day offset and fixed work hours. `PortfolioEditor.budget_range` and `PortfolioFleetEditor.count_range` are optional inclusive min/max/step ranges expanded by the backend; stored explicit budgets/counts remain the resolved scientific inputs. Version 3.0 remains readable with its original calendar semantics; migration creates a new 3.1 revision, never a rewritten artifact.

MR06 adds `ExampleBundle` (`lausanne-example@1`), an exact relative file inventory with byte sizes and SHA-256 checksums, a complete `ProjectConfig`, saved views and named source records. `/api/v1/examples/lausanne` reports availability; `/open` creates a reference or editable project and queues cancellable `example_import` when necessary; `/finalize/{job_id}` registers named input metadata and the initial project revision after verified installation. `ProjectConfig.example_bundle_id`, `read_only`, and `saved_views` are project metadata excluded from mobility identity. Reference configurations require an editable copy before mutation. Immutable resources are shared; deletion only tombstones project metadata. Bundles include valid movement-cache entries so changing temporal resolution in a fresh copy reuses the source simulation.

`DemandEditor.location_condition` is `all_resolved` by default. The explicit `depot_roundtrip` extension applies only to generated location tasks with a single declared depot; sampling uses that depot's directed strongly connected component and records excluded cells and feature mass before drawing tasks. Full sensing and utility axes remain unchanged. Resolved input artifacts retain availability, the complete physical catalog, task JSON, spatial rejection diagnostics, per-replication plans and measured planning times. `RunView.realization_source_run_id` identifies exact-input Sequential/Batch comparisons; changes to demand, supply, environment, civil time, seed or replication count cannot use comparison replay. New realization metadata also retains service-area memberships and assumptions, while old no-area realizations remain readable.

MR05 adds named `RunView` and `AnalysisView` records. `studio_run` and `studio_analysis` jobs execute in independent workers; submission saves the complete `ProjectConfig` as a new revision when needed. `/workbench/runs`, `/workbench/analyses`, `/workbench/runs/{id}/portfolio-defaults`, and `/workbench/utility-curve` expose backend-resolved choices. Root configuration is version 3.0. Migration returns explicit notices and a draft new revision; unmappable legacy input references require named input registration, while source revisions/artifacts remain intact. Project copies retain `linked_run_ids` and `linked_analysis_ids`; source records retain the revision that actually produced them. Retry preserves the original payload and increments the attempt history.

`ExposureConfig.fleet_movement_kinds` optionally overrides movement categories by exact fleet identity. A changed reporting resolution or sensing filter reuses immutable movements. `PortfolioConfig.sample_matrix_storage=reconstruct` retains sample selections, joint replication and matrix identities, nonlinear sample utility, and the original sparse physical exposure; combination matrices are reconstructed on demand. Legacy materialized sample artifacts remain readable. `UtilityWeightResource.kind=spatial_duration_temporal` factors complete cell weights and bin-duration weights without a dense cell-by-time configuration. `linear_capped` and `binary` supplement exponential saturation; curve values are computed by the same Python utility function.

`MatrixQueryRequest.temporal_aggregation=sum` sums selected reporting bins inside each replication/sample before empirical statistics. This preserves temporal covariance and certified zero observations. Responses are bounded by cells, not cells multiplied by reporting bins, and retain source R, source J, selected observation count, and source bin identities. Movement maps accept `aggregation=edge_usage`: directed edge intervals are unioned for geometry while traversal duration is summed; gaps are retained. Ordinary operation maps and summaries default to the source observation window.

MR04 extends `DispatchConfig.policy` with `batch_nearest_matching` and `one_shot`. Batch uses `batch_interval_s`, `batch_origin_s`, and the same `max_pickup_time_s` and canonical nearest-pair ordering as Sequential. A fleet-level `dispatch_batch` wakeup is processed after all equal-time state updates; exactly one matching snapshot is consumed per boundary. Public authoring uses `DispatchEditor.batch_minutes`. One-shot binds a replication-specific `AssignmentPlan` through a fleet-scoped alias; plans and measured search times are retained in separate realization tables. `Task.capacity_reset_at_end=false` marks a terminal depot return without replenishment; absent values preserve legacy depot-return semantics. OR-Tools 9.11.4210 is supplied by the optional `optimization` extra. Sorted tasks/vehicles, one worker, parallel cheapest insertion, greedy descent and a deterministic solution-count limit define accepted heuristic searches. A wall-time guard rejects any time-dependent incumbent. Every location task is mandatory; routes start/end at one depot, enforce vehicle windows, delivery capacity, task release before departure and explicit vehicle-area eligibility. Cost queries are bounded before allocation. Travel/service durations round upward to integer microseconds and window ends downward; execution uses original road times. No minimum-fleet or global-optimum claim is made.

## MR authoring extensions

MR03 introduces the public authoring `ProjectConfig` with `schema_version: "3.0"`, `FleetEditor`, `DemandEditor`, `SupplyEditor`, `DispatchEditor`, `SimulationEditor`, and `PortfolioEditor`. `/api/v1/workbench/resolve` publishes an immutable realization dataset and returns named fleet counts through `ProjectResolutionResult`. Runtime Task/TaskStep and vehicle/exposure contracts remain compatible with version 2.0; authoring resolution uses the same headless runner and event kernel. No legacy artifact is rewritten. Explicit root-workspace migration is delivered with MR05.

`DemandEditor.content` is `instances`, `counts`, or `rates`. Counts require `count_semantics` (`task_count` or `service_quantity`); numeric source rows cannot silently alternate between these meanings. `volume_mode` is `fixed` or `expected`. Per-day rates use the actual civil-day duration. `generation_timing` applies only to generators. Realizations are retained as an audit tape; online tasks enter the kernel at their release time, with the entire equal-time group injected before decisions. Simulation randomness is keyed by seed, civil origin, replication index, fleet, and semantic stream, independently of dispatch, display, sensing resolution, or process count.

GTFS service dates qualify trip instances. Adjacent days are converted using their own GTFS service origins; duties are inferred anew over the merged timeline. Pre-run uses the common executor. Inferred identities are labeled synthetic, and fixed-fleet chaining failure is not a mathematical infeasibility certificate. Spatial support retains a per-cell acceptance/rejection table, feature mass totals, and OD rejection records. Numeric weights never imply population unless a population source was supplied. Service-area IDs are scoped by their source; vehicle assignments are explicit and independent of depot location.

MR02 adds `EnvironmentEditor` and `EnvironmentResult` at `/api/v1/workbench/environments`. Named immutable inputs resolve into the existing prepared environment; generic features are a dependent dataset with `grid_features(cell_id, feature, value, unit)`, including zero-valued cells. Numeric source values are totals: points use canonical cell ownership and lines/polygons allocate by intersected length/area fraction. Unweighted geometry yields distinct count, length (m), and area (m2) feature names. Metadata records input provenance, alignment counts, aggregation rules, working CRS, and speed assumptions. OSM requests cache response bytes, query geometry, timestamps, and attribution. `environment.studio_osm@1` is the installed authoring capability; the legacy `environment.osm@1` provider contract remains reserved. Environment maps exchange EPSG:4326 and report deterministic preview subsampling explicitly.

The public workflow exposes `InputRegistration` and `InputDescriptor`, `/api/v1/inputs` list/register/upload, and logical `DELETE /api/v1/projects/{project_id}`. Input roles are boundary/network/speed/grid/population/feature/weight and fleet-owned demand/supply/assignment/gtfs/service_area/area_assignment. Snapshots contain source bytes, CRS/layer, hash, columns and bounded preview. Deleted projects preserve revisions/resources and cannot accept new jobs. Active jobs must finish or be cancelled before project deletion.

Status: current public contract specification for version 0.1.0. This document owns serialized names. Scientific semantics are specified in the simulation, data, and portfolio specifications.

## 1. Versioning and scalar conventions

New persisted scientific contracts use `schema_version: "2.0"`, deliberately distinct from legacy moment artifacts. HTTP uses `/api/v1`; HTTP version and artifact schema version are different namespaces. All external models reject unknown fields, invalid discriminators, non-finite numbers, and inconsistent references. JSON/YAML input must be parsed safely and resolve to the same canonical JSON representation.

| Type | Definition |
|---|---|
| ID | Nonempty opaque UTF-8 string, preserved as a string; no numeric coercion or row-position identity. Generated IDs use a documented stable encoding; filenames use separately sanitized opaque IDs. |
| VehicleKey | Pair `(fleet_id, vehicle_id)`, stable across all replications in one experiment. |
| Time | Finite float64 elapsed seconds relative to the experiment's explicit UTC origin. `*_s` suffix. No local datetime strings in runtime records. |
| Interval | Half-open `[start_s, end_s)` with positive duration for movement/service rows. Point events may share a timestamp. |
| Length / speed | Metres / metres per second. External km/h values converted explicitly. |
| Capacity / quantity | Nonnegative finite scalar in one declared unit per fleet; quantity semantics selected explicitly. |
| Currency | One declared currency or abstract cost unit and scale per analysis; integer minor units internally. Exact decimal parsing at the UI/API boundary. |
| Missing | Null only when permitted. Zero is data. Missing exposure rows mean zero only for complete certified partitions. |
| Geometry | Metric project CRS for processing; WKB with CRS metadata in GeoParquet; WGS84 GeoJSON for UI queries. |

IDs and categorical axes are sorted canonically for reduction/export. Display labels may change independently. JSON hashes exclude whitespace, mapping order, timestamps, local paths, worker counts, and UI settings; include normalized scientific values and all consumed dataset/algorithm hashes. A manifest stores the resolved scientific configuration beside its verified hash. Float serialization and semantic seed encoding must be versioned.

## 2. Configuration ownership

| Contract | Required fields and decisions |
|---|---|
| `ProjectRevision` | `project_id`, `revision_id`, `schema_version`, `name`, `description`, `scenario`, `default_exposure`, optional `default_portfolio`, dataset references. Saved revisions immutable; edits create a new revision. |
| `ScenarioConfig` | `environment_id`, `clock`, nonempty `fleets`, `replications`, `master_seed`, `joint_scenario_model`. Operational settings only; no sensor counts or costs. |
| `ClockConfig` | `origin_utc`, `display_timezone`, `simulation_start_s`, `observation_start_s`, `end_s`; simulation start ≤ observation start < end. |
| `EnvironmentBuildConfig` | `provider`, boundary selection/reference, `network_source`, `working_crs`, travel-time profiles, snapping tolerances, grid definition, optional population features. A supplied network declares `topology_policy=strict@1|conservative_repair@1|quarantine_invalid@1` and a positive metric `endpoint_tolerance_m`. Produces factored network and sensing hashes. |
| `ExposureConfig` | `simulation_id`, `sensing_geometry_id`, `bin_edges_s`, `active_movement_kinds`; v1 measurement is `movement_duration`. |
| `FleetConfig` | `fleet_id`, `label`, `demand`, `supply`, `dispatch`, `routing`. No required fleet taxonomy. |
| `DemandConfig` | `source` = `upload` / `generator` / `gtfs`; structure = `location` / `od` / `ordered`; adapter key/version and typed parameters. Generator has `generation_timing` = `offline` / `online`. Uploaded records are realized tasks, never implicit rates. |
| `SupplyConfig` | `source` = `upload` / `generated` / `gtfs_duties`; catalog source, availability source, capacity mode, paired area definitions/mapping refs, `idle_policy` = `stationary` / `random_cruise`, optional depot policy. Generated finite-capacity supply also requires scalar `capacity_value`; `none` forbids it. Capacity remains a common module for every supply source. |
| `DispatchConfig` | `policy` = `predefined` / `nearest_matching` / `batch_nearest_matching` / `one_shot`; see the MR04 amendment above for batch and planning contracts. |
| `RoutingConfig` | `profile_id` referencing the shared environment routing service; v1 static shortest travel time. No private graph or independent routing implementation per fleet. |
| `ExecutionOptions` | `workers`, memory limit, job timeout, progress frequency. Separate from scientific configuration. |
| `PortfolioConfig` | `exposure_id`, utility and weight references, per-fleet costs, budget levels, count-level enumeration, `sampling_rounds`, `sampling_seed`, `sampling_design=joint_replication_uniform_vehicle`, objective comparison resolution. All complete replication IDs and all catalog vehicles are eligible in v1. |

Generated availability may vary by replication, but the vehicle universe and its durable IDs are frozen before replications. Cross-replication physical metadata used for portfolio cost and group membership cannot vary. Replication-specific activity belongs in a separate table. Uniform profile names do not imply uniform speeds; each profile resolves to one versioned edge-weight vector.

Portfolio subcontracts use these canonical names: `count_enumeration.fleets` maps fleet IDs to either `count_levels` or `min_count/max_count/step/include_max`; `budgets` holds either `levels_minor` or `min_minor/max_minor/step_minor/include_max`; `costs` holds `unit`, `minor_unit_scale` and `by_fleet_minor`; `utility` holds `kind=exponential_saturation|linear_diagnostic`, `saturation_s` and `weights_ref`. The resolved scientific config stores explicit count/budget levels and integer costs. `comparison_resolution` contains positive `mean_utility` and `std_utility` values. `sampling_rounds` is a positive integer and `sampling_seed` a nonnegative 64-bit integer. Resource limits belong to execution options and cannot silently alter these scientific parameters.

Each travel-time profile selects exactly one discriminated source: `edge_travel_time`, `edge_speed`, `road_class_speed`, or `constant_speed`. Edge-speed fallback is optional; road-class speed requires an explicit fallback. Missing edge travel time without a configured fallback is an error. Implementations do not infer precedence among multiple fields.

## 3. Runtime contracts

These are protocol contracts, not source-code skeletons.

| Contract | Fields / operation |
|---|---|
| `LocationRef` | `location_id`, original coordinates/CRS, resolved `node_id`, snapped coordinates, snap distance, resolution status. v1 executes at the snapped node; no invisible connector leg. |
| `TaskStep` | Positive contiguous one-based `step_index`, `location_id`, optional `scheduled_time_s`, `service_duration_s` default 0, optional `quantity_delta`, provenance attributes. Scheduled time is a service-start target/no-earlier-than time. |
| `Task` | `task_id`, `fleet_id`, `release_s`, ordered nonempty steps, `kind` = `service` / `depot_return` / `cruise` / `reposition`, optional required capacity, source record references. Optional metadata never drives undocumented kernel branches. |
| `VehicleSpec` | `VehicleKey`, base availability window, initial location, optional depot, capacity, explicit set of assigned area IDs. |
| `VehicleState` | VehicleKey, status, current execution reference, committed location, availability time, capacity state, execution generation token. Future location is queried from the execution timeline, not written as a current location. |
| `AssignmentPlan` | Ordered rows `(VehicleKey, order_index, task_id)`; one owner per service task, nonduplicate sequence positions. |
| `Event` | `time_s`, phase, stable tie key, event type, entity references, generation token. Types: task release, vehicle entry, execution completion/availability, vehicle exit, policy wakeup. |
| `RouteResult` | `reachable`, ordered directed edge IDs with length/duration, total duration/distance, network/profile hash, reason if unreachable. Equal-node route is reachable with zero edges/time. |
| `ExecutionPlan` | Immutable ordered movement/service/wait intervals, task milestones, capacity deltas, end time/location, execution token. No vehicle owns two active plans. |
| `ReplicationContext` | Stable replication ID, scenario realization reference, named RNG stream provider, immutable environment/catalog, cancellation token, progress sink. |
| `DecisionSnapshot` | Exact decision time plus canonically ordered idle `VehicleState` and waiting `Task` tuples. It is immutable and contains no fleet display labels. |
| `PlannedAssignment` | Exact `Task` plus complete `ExecutionPlan`; plan references, start time, fleet, and next execution-generation token must agree with the decision snapshot. |
| `KernelResult` | In-memory simulation handoff containing canonical task/vehicle/execution outcomes, ordered lifecycle records, processed heap-event count, and maximum queue size. Each execution outcome separates planned/realized end, retains planned task milestones and applied capacity milestones, contains cutoff-clipped realized intervals, and identifies realized position as exactly one location or directed-edge fraction. Persistent table publication remains M06. |

### Extension boundaries

| Provider / service | Input → output | Mandatory behavior |
|---|---|---|
| `EnvironmentProvider` | Region request, requested features, network mode, cache policy → versioned raw geographic bundle + provenance | Separate retrieval from normalization; offline local provider available in v1, OSM provider reserved. |
| `EnvironmentBuilder` | Raw geographic bundle + metric/grid/network settings → prepared environment | Common validation irrespective of data source. |
| `NetworkRepairService` | Selected metric roads + explicit policy/tolerance → diagnosis, repair plan, derived roads, lineage, quarantine and readiness grade | Never mutates raw data, bridges gaps, infers geometric-crossing junctions, or silently discards records. |
| `ScenarioImpactValidator` | Prepared routing service + finite required directed node pairs → reachability and route-edge evidence | `scenario_ready_with_quarantine` is valid only for the checked scenario scope; it is not full-network source certification. |
| `DemandAdapter` | Dataset + column/time/location mapping → normalized tasks + validation report | Transactional, no simulation side effects. |
| `DemandSource` | Config + replication RNG → release-ordered task stream | No dependence on UI previews or worker scheduling. |
| `SupplySource` | Config → fixed catalog; catalog + replication RNG → availability realization | Stable physical IDs; absent vehicle remains in catalog with zero activity. |
| `EligibilityFilter` | Vehicles, waiting tasks, prepared area/capacity indices → feasible-pair iterator | Fleet membership, status, shift, area, capacity, reachability, configured pickup-time cap. |
| `DispatchPolicy` | Snapshot + routing queries → disjoint assignments | No movement/exposure mutation. Deterministic tie handling. |
| `RoutingService` | Profile + source/target nodes → travel time or ordered route | Same weights and tie-breaks for estimates and execution. |
| `TaskExecutor` | Vehicle state + task + route service → execution plan | Construct timeline without advancing global event time. |
| `KernelDecisionAdapter` | Canonical decision snapshot → pre-existing task assignments, then generated operational assignments | `SimulationDecisionAdapter` implements Predefined and Nearest Matching over existing tasks, followed by M05C depot return, random cruise, or stationary fallback. The event kernel validates disjoint plans and lifecycle rules. |
| `OperationalPolicy` | Idle state + waiting-task snapshot + next decision time + RNG → optional operational execution | `operational.depot_return@1` has priority over `operational.random_cruise@2`; no generated action means stationary idle until a meaningful event. Random cruise groups its directed-edge walk into one execution between decision epochs and is absent from demand task outcomes and operational-event output. Exact movement intervals remain retained. The common kernel remains `event-kernel@2`. |
| `OperationalRoutingService` | `RoutingService` + canonical outgoing neighbor IDs + stable prepared-node `LocationRef` | Required and prevalidated only for `random_cruise`; depot and service routing retain the base route contract. |
| `ExposureAllocator` | Movement + edge-grid pieces + bins → sparse exposure | Exact interval intersections under the defined within-edge model. |
| `ExposureReader` | Artifact + replication/vehicle filters → aligned sparse chunks | Validate completeness/axes before treating absent rows as zero. |
| `SensingQueryService` | Exposure + vehicle scope + replication/statistic + cells/bins → matrix slice | For aggregate statistics, sum chosen vehicles inside each replication before mean/variance. Retain explicit axes and zero/missing semantics. |
| `UtilityFunction` | Aggregated exposure and weights → scalar utility | Pure per-replication calculation, with versioned parameters. |
| `CountEnumerator` | Catalog + count levels/resolution + fleet costs/budgets → feasible count vectors | Predict scale and enforce limits before enumerating. |
| `AllocationSampler` | Catalog + complete replication IDs + sampling seed/round ID → joint replication and uniform fleet permutations | One joint replication per round, sampling without replacement within a fleet; common random numbers across count vectors. |
| `PortfolioEvaluator` | Count vector + round draw + exposure reader → sparse sample matrix and utility | Never invokes simulation. Summaries are over sampling rounds, keeping R and J distinct. |
| `FrontierBuilder` | Count-portfolio statistics + budgets → budget-specific membership rows | Maximize mean, minimize combined sample std among count portfolios with cost ≤ budget. |

## 4. Dataset and artifact tables

Each table includes schema metadata and a manifest reference. Key columns are non-null unless explicitly described. Table keys below are logical uniqueness constraints, checked before publication; no reliance on Parquet enforcing them. A completed artifact declares a nonempty canonical expected-table inventory. Each table declares canonical partition axes and the exact Cartesian partition set, including explicit empty partitions; publication rejects missing, duplicate, extra, or reordered partitions.

| Table | Key | Essential columns |
|---|---|---|
| `locations` | `location_id` | Original/projected/snapped coordinates, CRS, node ID, snap distance, source row |
| `tasks` | `(replication_id, fleet_id, task_id)` | release, kind, demand provenance; deterministic uploads may reference a shared table without copying |
| `task_steps` | task key + `step_index` | location, scheduled target, service duration, quantity delta |
| `od_rates` | `(fleet_id, origin_location_id, destination_location_id, interval_start_s)` | Half-open interval end and rate normalized to tasks per second; this table is a `rate_model`, never observed tasks. |
| `vehicle_catalog` | VehicleKey | stable catalog index, physical identity provenance, capacity, depot, area set |
| `vehicle_availability` | `(replication_id, VehicleKey)` | `active`, start/end, initial location; v1 at most one continuous shift per replication |
| `area_assignments` | `(fleet_id, vehicle_id, area_id)` | Explicit foreign keys to a separate area-definition input; never inferred from depot. |
| `import_mapping` | `mapping_id` | Canonical saved mapping JSON used for replay; source chunk size is excluded. |
| `validation_issues` | source/table/row/task/code | Structured import diagnostics; quarantined task errors remain auditable. |
| `catalog_identity` | `catalog_id` | Fixed catalog hash and physical-metadata hash used across replications. |
| `assignments` | `(fleet_id, task_id)` | vehicle ID, sequence order for predefined plans |
| `duties` | `vehicle_id` | identity provenance, canonical ordered trip IDs, activation, nominal end |
| `deadheads` | `(vehicle_id, from_trip_id, to_trip_id)` | routed distance/time, stationary turnaround, feasible idle slack |
| `trip_stops` | `(trip_id, step_index)` | original stop sequence and ID, resolved location, GTFS arrival/departure elapsed seconds; complete before sensing-boundary classification |
| `trip_diagnostics` | `trip_id` | route, retained/rejected status, complete stop count, nominal finish/lateness, rejection reason |
| `route_failures` | `trip_id` | explicit failed internal/deadhead route reason; empty table retained in a complete artifact |
| `gtfs_metadata` | selected service date | final route IDs, agency timezone, UTC service origin, immutable GTFS source hash |
| `diagnostics` | artifact singleton | source/active/selected/accepted/rejected counts, per-route trip counts, crossing count, task/duty counts, identity assumption |
| `grid_cells` | `cell_id` | canonical cell index, geometry, included area fraction, optional feature references |
| `time_bins` | `time_bin_id` | start/end, canonical index; last bin may be shorter |
| `road_edges` | `edge_id` | derived source/target node, raw source `(u,v,key)`, component index, repair action, oriented geometry, length, profile costs/provenance |
| `repair_actions` | raw source `(u,v,key)` | retained/merged/split/quarantined action, component counts and reason |
| `edge_lineage` | `edge_id` | raw source identity, component index, direction and repair action |
| `node_lineage` | derived node ID | source node ID when present, coordinate cluster, node kind and observation count |
| `quarantined_edges` | raw source `(u,v,key)` | original geometry, issue code and reason; never enters routing |
| `repair_issues` | issue/source record/source node | severity and bounded diagnostic message |
| `repair_summary` | artifact singleton | policy/version/tolerance, action counts, repair hash and readiness grade |
| `repaired_roads` | raw source `(u,v,key)`, component index | topology-preserving derived LineString components before directed expansion |
| `edge_grid_pieces` | `(edge_id, piece_index)` | `cell_id` nullable outside, `start_fraction`, `end_fraction`; fractions in edge traversal order |
| `movements` | `(replication_id, VehicleKey, movement_index)` | task/execution/leg references, directed edge ID, start/end, edge start/end fractions, movement kind |
| `activity_intervals` | `(replication_id, VehicleKey, activity_index)` | start/end, movement/service/wait/idle status, execution/task references, location for stationary activity; M05C exposes positive half-open idle intervals in `KernelResult`, while M06 owns persistent indexing/publication |
| `task_outcomes` | `(replication_id, fleet_id, task_id)` | release, assignment, first service, completion, terminal/censored status, waiting/lateness, reason |
| `operational_events` | `(replication_id, event_index)` | event time/type, vehicle/task IDs, state changes and quantity deltas |
| `exposure` | `(replication_id, VehicleKey, cell_id, time_bin_id)` | positive `duration_s`; zero rows omitted |
| `replication_status` | `replication_id` | complete flag, seed manifest, catalog hash, row counts, exposure conservation diagnostics |
| `portfolio_counts` | `portfolio_id` | catalog hash, count vector, total cost minor units |
| `portfolio_metadata` | exposure artifact | explicit simulation `R`, sampling `J`, count-portfolio `P`, retained sample/matrix counts, completeness and variability interpretation |
| `sampling_rounds` | `round_id` | selected joint replication ID, sampling seed, seed-manifest hash, complete replication-set hash, and verified sampling-design hash |
| `sampling_orderings` | `(round_id, fleet_id, rank)` | vehicle ID; full random catalog ordering shared across count vectors |
| `portfolio_samples` | `(portfolio_id, round_id)` | `sample_id`, selected replication ID, `matrix_id`, utility, total exposure; preserves repeated draws |
| `sample_selection` | `(sample_id, VehicleKey)` | explicit selected vehicles; may be a lossless view of round orderings + counts rather than duplicated storage |
| `sample_matrices` | `matrix_id` | joint replication, selected-set hash, nonzero count, total exposure, per-sample utility, complete flag; certifies sparse empty matrices |
| `sample_exposure` | `(matrix_id, cell_id, time_bin_id)` | positive `duration_s`; sparse unique sample matrices with manifests for empty matrices |
| `portfolio_analysis_metadata` | source sample artifact | explicit R/J/P, complete grid/bin shape, sparse-zero convention, variance/quantile methods, variability interpretation, frontier availability and inference scope |
| `portfolio_statistics` | `portfolio_id` | R, J, mean, sample variance/std, quantiles, conditional Monte Carlo mean SE, cost |
| `portfolio_sensing_statistics` | `(portfolio_id, cell_id, time_bin_id)` | mean duration, sample variance/std over J, with sparse-zero convention |
| `budget_levels` | `budget_id` | exact budget/currency scale, feasible/frontier counts, frontier availability and disabled reason |
| `budget_frontiers` | `(budget_id, portfolio_id)` | budget, feasibility, nondominated status, tie group; default export may retain only feasible rows |

Movement geometry is joined from road edges, not repeated in every row. Store edge start/end fractions so horizon-truncated movements preserve correct spatial timing. Default movement retention is mandatory in v1. Sparse exposure is partitioned by replication and optionally fleet; use coarse files, not one file per vehicle/cell. Record empty complete partitions explicitly. Tables must support predicate pushdown and streaming writes.

M06 uses one coarse file per nonempty replication/table partition. Empty complete partitions have a manifest row count of zero, null checksum and no physical file; this state is distinct from a missing declared file, which invalidates the artifact. Simulation publication requires the full catalog in every complete `KernelResult`. Exposure publication copies the simulation catalog and joint replication identity, records actual grid/time-axis hashes and conservation diagnostics, and depends on the immutable movement artifact. Reader verification precedes sparse-zero interpretation.

`SensingMatrixSlice` records the selected replication, vehicle, cell and ordered reporting-bin axes, `replication_count` (simulation `R`), expected sparse matrix shape, unit (`s`, or `s^2` for variance), completeness, and `absent_sparse_rows_are_zero`. It never reports portfolio sampling rounds `J`. Aggregate exposure statistics are formed from within-replication vehicle sums before empirical reduction across `R`.

## 5. HTTP contract

IDs in responses are opaque. Long-running creation returns `{job_id, resource_id, status_url, events_url}` with HTTP 202; cached immutable results may return HTTP 200 with `cache_hit: true`. Idempotency keys are scoped by project and operation; same key with a different request digest yields 409. Semantic duplicates with different keys are linked by fingerprint, not mistaken for the same execution attempt.

| Endpoint | Contract |
|---|---|
| `GET /api/v1/health`, `/capabilities`, `/schemas/{name}` | Health; implemented capabilities and parameter schemas; versioned configuration schemas |
| `GET, POST /api/v1/projects` | List/create project |
| `GET /api/v1/projects/{id}` | Project and current revision reference |
| `GET /api/v1/projects/{id}/revisions`, `/projects/{id}/revisions/{revision_id}` | Ordered immutable revision history and one full revision payload |
| `POST /api/v1/projects/{id}/revisions` | Save full revision; `base_revision_id` detects conflicting edits (409) |
| `POST /api/v1/uploads` | Stream multipart file to staging; return upload ID and bounded structural preview |
| `POST /api/v1/imports` | Upload refs + adapter + field mapping + declared units/CRS/time → persistent normalization job |
| `GET /api/v1/datasets/{id}`, `/preview`, `/issues` | Metadata, paginated normalized preview, paginated validation report |
| `GET /api/v1/datasets` | Filtered dataset catalog |
| `DELETE /api/v1/datasets/{id}` | Delete only unreferenced artifacts; otherwise 409 with referring IDs |
| `POST /api/v1/environments` | Local provider request + preparation config → environment job |
| `GET /api/v1/environments/{id}` | Prepared status, network/grid diagnostics, resolved provider provenance |
| `POST /api/v1/gtfs-reconstructions` | Dataset + service date/routes + duty/timing settings → tasks, supply, assignments and diagnostics |
| `POST /api/v1/scenarios/validate` | Full scenario + exposure settings → readiness report and immutable resolved-input reference |
| `POST /api/v1/simulations` | Resolved scenario/revision + execution options → simulation job |
| `GET /api/v1/simulations`, `/simulations/{id}` | Run list/manifest/completeness |
| `POST /api/v1/exposures` | Simulation + grid + bins + movement-kind mask → allocation/reaggregation job |
| `GET /api/v1/exposures/{id}` | Axes, candidate catalog, complete replication set, sensing definition |
| `POST /api/v1/portfolio-enumerations/preview` | Count levels, sampling rounds, budgets and fleet costs → P, P×J, matrix storage/work estimates and blocking limits; no utility computation |
| `POST /api/v1/portfolio-analyses` | PortfolioConfig → enumeration/evaluation/frontier job |
| `GET /api/v1/portfolio-analyses/{id}` | Artifact manifest, enumeration scope, progress/result references |
| `GET /api/v1/portfolio-samples/{id}` | Verified immutable sample-artifact manifest used for point-to-round lineage |
| `GET /api/v1/portfolio-frontiers/{analysis_id}?budget_id=...` | One bounded budget membership joined to exact count statistics, comparison keys, tie IDs, and separate `R`/`J` metadata |
| `GET /api/v1/jobs`, `/jobs/{id}` | Project-filtered durable job history; individual snapshot, counters, cancellation flag and failure details |
| `GET /api/v1/jobs/{id}/events` | Resumable SSE stream |
| `POST /api/v1/jobs/{id}/cancel` | Idempotent cancellation request; terminal jobs return their existing state |
| `GET /api/v1/results/{resource_id}/{table}` | Allowlisted table query with typed equality filters, optional half-open elapsed-time bounds, and a query-bound cursor; no arbitrary SQL |
| `GET /api/v1/maps/{resource_id}/{layer}` | Bbox/time/replication/fleet/vehicle-filtered WGS84 GeoJSON with explicit completeness/aggregation metadata. Simulation `movements` requires one replication and clips recorded directed-edge intervals only for display. |
| `GET /api/v1/operation-summaries/{simulation_id}` | One replication plus optional fleet/vehicle/half-open time filters → server-side task status counts, explicit wait denominators, carry-in count, utilization, and service/operational movement durations |
| `POST /api/v1/matrix-queries` | Typed immutable resource, matrix scope, axes/filters and statistic → bounded sparse matrix slice, backend time/overall summaries, or a managed export requirement |
| `POST /api/v1/exports` | Artifact/table selection + format → export job; small existing files may be linked directly |
| `GET /api/v1/artifacts/{id}/download` | Managed downloadable file, checksum, MIME type; never an arbitrary filesystem path |

The simulation application service may chain default exposure allocation as a second job. `simulation.completed` means mobility artifacts are complete; portfolio remains disabled until an exposure artifact is complete. The UI displays both phases and never conflates them.

M11 matrix responses serialize `replications_R` as the complete operational pool size and, for an exposure realization, `selected_replication_count` as the number selected by that query. Exposure responses always serialize `sampling_rounds_J=null`. Portfolio responses serialize the source operational `R` and the retained sampling-round count `J` independently. `time_summary` and `overall_value` have semantics `statistic_of_within_observation_cell_sum`: selected vehicles/cells are summed inside each replication or round before mean/variance/std reduction. Thus aggregate standard deviation preserves covariance and is never formed by adding marginal standard deviations. Sparse absent rows are zero only when the verified artifact states `absent_sparse_rows_are_zero`.

Browser JSON arrays for matrix axes and vehicle keys are normalized at the HTTP adapter into the strict tuple contracts. Result and map responses remain capped by configured row, feature, and byte limits; HTTP 413 requires narrower filters or a managed scientific export. No endpoint returns a dense `R × vehicle × grid × time` tensor or all trajectories by default.

Default table page size 100, maximum 1,000. Map requests have a configured feature/byte limit, default 20,000 features and 10 MiB. Large results must aggregate or require narrower filters; never silently truncate scientific summaries. A response states `returned_count`, `total_matching_count` where affordable, `is_complete`, and any visualization sampling/aggregation. Exports are the complete alternative. Do not put the full exposure tensor into REST JSON or SSE.

`MatrixQuery` specifies `resource_id`, `kind=vehicle_exposure|operational_aggregate|portfolio_sample|portfolio_summary`, optional fleet/vehicle/replication/portfolio/round IDs as required by kind, requested cells/bins and `statistic=realization|mean|variance|std`. `MatrixSlice` returns grid/time-axis references, selected axis IDs, unit (`s` or `s^2`), sparse `(cell_id,time_bin_id,value)` entries, expected shape, completeness, zero-fill convention, R/J where relevant and resolved filter provenance. Limit nonzero rows/response bytes; large slices return an explicit export/narrow-filter requirement rather than a misleading partial matrix. For a complete single-vehicle realization, this is exactly the vehicle's E matrix. The UI can reconstruct its zeros from the axis metadata without requiring dense physical storage.

### Validation and errors

Common error body: `code`, `message`, `issues`, `request_id`. An issue contains `severity` (error/warning/info), `code`, `field_path`, optional dataset/table/source row/task ID, `message`, and suggested corrective action. Error examples: `INVALID_TIME`, `UNRESOLVED_CRS`, `UNREACHABLE_LEG`, `DUPLICATE_TASK_STEP`, `INCOMPLETE_REPLICATIONS`, `CATALOG_MISMATCH`, `ENUMERATION_LIMIT_EXCEEDED`, `CAPABILITY_UNAVAILABLE`.

Use 422 for invalid scientific inputs, 409 for state/version/reference conflicts, 413 for upload limits, 404 for unknown resources. Internal exceptions become failed jobs with sanitized diagnostics; frontend errors link to field paths. An invalid upload stays inspectable in staging but does not become a usable dataset.

### SSE

Each persisted event has increasing integer `event_id` within a job, `job_id`, `type`, `timestamp_utc`, `phase`, and a bounded payload. Types: `status`, `progress`, `warning`, `completed`, `failed`, `cancelled`. Use event ID for SSE `id`; accept `Last-Event-ID`. Heartbeats do not advance progress. Completion/failure/cancellation always has a durable terminal event and snapshot. On an expired replay cursor, send an explicit snapshot/reset event; the client then reconciles from `GET /jobs/{id}`. A lost connection is a connection state, not a failed simulation. Polling is the fallback.

## 6. Python and CLI parity

Public application use cases: prepare environment, normalize demand, reconstruct GTFS, validate scenario, run simulation, allocate exposure, enumerate/evaluate portfolios, query/export results. Each accepts the same validated contracts as HTTP and returns typed artifact references, with optional progress/cancellation adapters. CLI commands map one-to-one to these use cases and accept configuration files plus explicit artifact root. Synchronous library execution must work without SQLite, an HTTP server, or a browser; it still writes complete manifests when persistence is requested.

The M07 headless boundary serializes `ScenarioResourceBundle`: one `ScenarioConfig` plus one `FleetRuntimeResource` per fleet. Runtime resources bind immutable demand/supply references to normalized tasks, resolved locations, fixed vehicle specifications or generated-catalog inputs, generator weights, predefined assignments, explicit location-area memberships, and canonical assumption labels. Validation checksum-verifies dataset dependencies, requires exact fleet alignment, rejects unbound locations/weights/catalog members, and publishes remediated `scenario-validation@2`. M07 initially restricted `run-simulation` to `workers=1`; M09 now owns the deterministic spawn runner. Worker count remains execution provenance and is excluded from scientific identity.

Artifact-backed runtime records are reconstructed from checksum-verified dataset tables. `selected_task_ids` and `selected_vehicle_ids` identify a bounded GTFS subset; uploaded task/catalog artifacts do not permit partial selection. Inline artifact-bound tasks, vehicles, locations, and GTFS assignments are verification claims, not authoritative replacements. Runtime resources are canonicalized before scenario identity is computed.

M08A serializes `UtilityWeightResource` and `PortfolioResourceLimits`, and exposes synchronous `preview-portfolios` and `evaluate-portfolio-samples` use cases. Preview returns explicit `replications_R`, `sampling_rounds_J`, `P0`, feasible `P`, `P*J`, and storage/working estimates. Evaluation publishes `portfolio-samples-parquet@1` with the eight M08A tables above. It executes exactly `J` rounds, samples one complete joint replication per round, uses fleet-permutation prefixes for all count vectors, and never invokes simulation.

M08B exposes synchronous `summarize-portfolios` and `HeadlessApplication.summarize_portfolios`. It consumes one checksum-verified `portfolio-samples-parquet@1` artifact and publishes `portfolio-analysis-parquet@1` with `portfolio_analysis_metadata`, `portfolio_statistics`, `portfolio_sensing_statistics`, `budget_levels`, and feasible-row `budget_frontiers`. Its immutable `portfolio_samples` dependency remains the authoritative full export for count, round, ordering, selection, sample and matrix tables; `PortfolioAnalysisArtifactReader` resolves both layers. Budget/cost/comparison changes reuse the same matrices when all newly feasible count vectors are present. If they are absent, analysis fails with an explicit request to expand M08A samples without rerunning mobility. R is the number of retained joint operational replications; J is the independent portfolio sampling-round count and is never inferred from R. J=1 produces null variance/std/SE and disabled frontier membership.

M09 assigns every accepted long-running request a durable output reservation. A completed reservation resolves to its immutable `ArtifactRef`; a semantic cache hit links a distinct job to the existing reservation. Simulation jobs submit complete replications to a bounded spawn pool and publish only after the coordinator receives every result. `MatrixQuery.kind=portfolio_sample` accepts either an M08A sample artifact or the sample dependency of an M08B analysis. Portfolio matrix responses contain distinct `replications_R` and `sampling_rounds_J`; exposure responses contain `replications_R` and `sampling_rounds_J=null`.

M10 adds full revision retrieval and project-scoped durable job recovery for the browser. The HTTP adapter validates nested scientific contracts in JSON mode so JSON arrays and ISO date/time strings cross the boundary before strict immutable Python models are handed to application services; this is representation decoding, not weakened scientific validation.

Frontend types are generated from backend OpenAPI/schema definitions during implementation. Check generated output in version control and verify regeneration produces no diff. Runtime semantic validation remains backend-owned; form validation is early feedback, not a second independent scientific implementation.

M12 adds no scientific serialized names. The installed `mobile-sensing launch --artifact-root <path>` entry point composes the existing durable coordinator and FastAPI interfaces, serves packaged browser assets on `/assets`, serves the SPA at `/` and non-API browser routes, and preserves JSON 404 behavior for unknown `/api/` routes. `serve-api` remains API-only. Scientific Python/CLI commands remain usable with core dependencies and do not require FastAPI, Uvicorn, Node, or a browser.

## MR07 installed workflow clarification

`ExampleInfo.results_path` is the verified primary Operations view used by the Project example entry. Saved example views include all four budgets and a concrete nonzero count portfolio; view selection is included in bundle identity. Export jobs expose their managed `resource_id` through `GET /api/v1/artifacts/{artifact_id}/download`. Successful project deletion returns HTTP 204 with no JSON body. These UI/metadata changes do not rewrite scientific artifacts or change mobility/sample identities.

### Feedback temporal preview endpoints

`POST /api/v1/workbench/calendar-preview` and `temporal-preview` accept `ProjectConfigRequest`. They return `CalendarPreview` and `TemporalPreview` respectively; previews do not consume RNG streams. `SimulationEditor.timezone` overrides the environment display timezone for exact days. Generic days use UTC internally. Representative GTFS weekdays use the agency timezone. Calendar preview records the resolved date, candidate count and selected-route trip counts.

`POST /api/v1/workbench/portfolio-preview` accepts `PortfolioEditor`, reads its completed source run, and returns bounded expanded levels. Range expansion includes the maximum endpoint even when the interval does not divide the range. It rejects fractional sensor counts, out-of-catalog counts and excessive enumeration.

Generated-demand warm-up currently requires a complete civil-day observation. Prior civil-day demand repeats the declared profile on separate semantic streams, is clipped to the requested warm-up interval, and shares the normal executor. This is an explicit initial-state experiment, not a stationary sampling claim. One contiguous shift per physical vehicle is supported; shift groups partition the catalog and are not repeated registrations of the same vehicle.
`GET /api/v1/fleet-summaries/{exposure_id}` returns `MeanFleetView` over all complete joint replications, service-task release cohorts and reporting-bin-aligned windows. Empty cohort rates are null and excluded from the rate denominator; active vehicles are time averages. `mean_edge_usage` map aggregation uses all replications and reports mean seconds per directed edge. Window matrix responses add `mean_coverage_fraction`, computed within observations including zero cells. For `positive-length-road-grid@1` exposure artifacts, the complete exposure grid axis and coverage denominator contain exactly the prepared sensing cells whose clipped geometry has a positive-length intersection with at least one prepared road edge; point-only contacts are excluded. The immutable full prepared grid remains the domain for environment features and demand authoring. Responses expose `coverage_denominator_cell_count` and a versioned `coverage_semantics`; historical artifacts retain their recorded prepared-grid domain. `GET /api/v1/workbench/features/{artifact_id}/map?feature=...` exposes complete prepared feature cells, units and source audit, never a draft-derived normalization.


## MR20 operating exposure and replenishment extension (2026-09-15)

Project authoring version 3.2 adds `FleetEditor.sensing_mode` (`operating_duration` for new fleets; historical 3.0/3.1 payloads retain `movement_duration`), `SupplyEditor.depot_min_stay_minutes` (positive; new default 10) and `timetable_idle_break_minutes` (new default 60, nullable). The latter declares a synthetic off-duty rule only for unassigned timetable idle intervals at least this long; service and scheduled waiting remain on-duty. Physical identities and operation records are unchanged. Depot stationary intervals are excluded by resolved node identity, not by cell identity.

## MR33 independent temporal intervals and fleet-level spatial mixtures

Authoring schema 3.3 separates `SimulationEditor.temporal_resolution_minutes` from `PortfolioEditor.utility_temporal_resolution_minutes`. The simulation value defines exposure/reporting bins used by time-series statistics. The portfolio value defines the bins on which nonlinear utility is evaluated; it does not change stored exposure or result-map resolution. Each utility interval must be a union of complete reporting bins. For reporting bins (t\in q), the evaluator computes (S_{g,q}=\sum_{t\in q}S_{g,t}) before applying the selected pointwise utility. Historical 3.0–3.2 configurations migrate with the utility interval equal to their simulation reporting interval, preserving their prior estimand. New examples use 60-minute reporting and a 1,440-minute utility interval.

`DemandEditor.spatial_weights`, `DemandEditor.destination_spatial_weights`, and `SupplyEditor.spatial_weights` contain ordered `SpatialFeatureWeight {feature, weight}` components. Empty lists preserve the legacy scalar feature field. On the complete prepared-grid domain (G), each component is normalized independently,

\[
q_{j,g}=x_{j,g}/\sum_{h\in G}x_{j,h},\qquad
p_g=\frac{\sum_j\alpha_j q_{j,g}}{\sum_j\alpha_j}.
\]

Coefficients are finite and nonnegative with positive total mass. Every positive-coefficient feature must have positive full-grid mass; failure is explicit. Routing/depot eligibility is applied to the combined distribution and the retained mass is then renormalized. The same resolver supplies generated task origins, OD destinations, generated vehicle initial locations, and synthetic-depot weighted centers. This prevents incomparable units such as square metres and POI counts from being added before normalization. The San Francisco example composes normalized residential area and commercial locations at 0.6/0.4 in Fleet Configuration; `activity_proxy` is no longer an Environment feature.

Schema 3.4 implements explicit `service_area_mode: none | uploaded | auto`. `uploaded` retains independent geometry and vehicle-assignment inputs. `auto` requires generated demand, an explicit area count, at least one positive-demand location per area and at least one physical vehicle per area. `demand-balanced-recursive-bisection@1` partitions the routing-eligible normalized origin mixture deterministically into spatially compact areas. Each area first receives one vehicle; the remaining fixed catalog uses largest-remainder allocation by expected origin-demand mass. Every vehicle and resolved task origin receives exactly one frozen area before replications. The depot does not determine membership, and depot-return or reload tasks remain eligible for every assigned subfleet. OD eligibility uses the origin and permits the destination to cross the boundary. The resolved report publishes expected mass, centroid, positive-cell count, resolved-location count and vehicle count for every area; invalid inputs fail rather than reverting to unrestricted dispatch.

`DispatchEditor.allow_replenishment` defaults true for new settings; historical payloads retain false. `max_replenishments_per_vehicle` bounds optional depot visits (default four). One-shot retains mandatory independent tasks, fixed physical vehicles and one shared directed routing profile; each refill resets stock only after its positive depot service interval. Reload nodes cannot follow a start or another reload, and task quantities larger than compatible vehicle capacity fail explicitly. The solver reports its bounded search and visit limit; it does not claim infeasibility merely because its heuristic fails.

`ExposureConfig.measurement` also accepts `operating_duration`. `activity_source_ref` references the immutable resolved location/catalog dataset; `operating_fleet_ids` selects fleets including stationary states, and `fleet_idle_break_seconds` records timetable off-duty inference. The allocator combines disjoint movement and stationary contributions and reports eligible duration, excluded depot/off-duty duration, outside-grid duration and conservation. Existing movement artifacts remain untouched and keep their original interpretation.

`operating-activity-exposure@2` corrects replication-level persisted diagnostics: the partition key is a one-element tuple, while diagnostic groups use the replication string. Eligible duration equals moving plus stationary duration and also in-grid plus outside-grid duration. The sparse allocation is unchanged from version 1. Corrected outputs receive new immutable identities; historical version-1 artifacts are retained, not rewritten.

## MR21 project ownership extension (2026-09-15)

The launcher artifact root becomes a directory of named self-contained projects. Each project owns `data/`, `environment/`, `settings/`, `results/`, `exports/`, and a hidden `.system/` database/worker store. Internal relative collection aliases in `.system/` point only inside that same project; all source/result bytes are owned by the project and aliases are recreated when loading a folder. A copied folder is independently loadable. No shared external artifact root is required. The headless flat artifact contract remains supported.

A local workspace hub routes existing endpoints to a project store using `X-Project-ID`, explicit project paths, or a unique job/resource identity. It never starts scientific work in the API process. One workspace coordinator supervises one heavy job at a time across project queues. Global project listing/create/copy/name validation is workspace-scoped; direct project data access is project-scoped. Historical flat workspaces migrate with checksum-verified dependency copies and metadata backups; original artifacts are not rewritten. Existing completed jobs/revisions remain associated with their owning project.

`project-folder@3` uses `settings/current.json`, `settings/revisions/`, `results/index.json`, and actual source files under `data/` and `environment/`. Project names remain unique normalized portable names; internal stable IDs remain unchanged. Rename is blocked while a project has active jobs. Export remains `portable-project@1`; internal aliases are serialized as ordinary contained files and reconstructed on load.

## MR22 run management and frontier presentation (2026-09-15)

Project-scoped `deleted_runs` metadata hides named runs without removing their immutable dependency data. `GET /workbench/runs/{id}/deletion-preview?project_id=...` returns the saved run name and dependent analyses/runs. `DELETE /workbench/runs/{id}?project_id=...` records a logical tombstone; `POST /workbench/runs/{id}/restore?project_id=...` removes it. `GET /workbench/deleted-runs?project_id=...` supports recovery. Old revisions and completed jobs cannot resurrect a tombstoned run. Existing analysis sources remain readable; deletion does not promise disk reclamation.

PortfolioFrontierView adds backend-derived `max_mean_utility`, `max_p05_utility`, and `frontier_portfolio_count`. These maxima can belong to different portfolios. The UI draws each feasible count portfolio once, colors it by the lowest configured budget that admits it, and overlays budget frontier lines. Frontier and selected-point outlines do not modify scientific values or introduce jitter.

### MR23 portable run-recovery metadata

`portable-project@1` inventories may additionally contain `deleted_runs`, an ordered list of `{run_id, name, deleted_at_utc}`. These records retain the named run and its complete scientific dependency closure while excluding it from visible `run_ids`. The current importer validates the retained run, remaps deletion ownership to the new project and preserves the deletion timestamp. Packages without this optional list remain readable. Folder duplication and ZIP transfer both preserve recovery; neither operation turns a logically deleted run into a visible result.

The workspace launcher exposes the same complete OpenAPI document as its delegated scientific adapter. Project folder-name reservation/publication is serialized across API threads and processes. An invalid upload releases its unpublished folder; a missing example bundle leaves own-data project creation usable.

### MR31: named city example bundles

The project workspace hub accepts `GET /api/v1/examples/{example_key}` and
`POST /api/v1/examples/{example_key}/open`, where `example_key` is `lausanne`
or `san-francisco`. Each city owns an independent project store and immutable
bundle inventory. `ExampleBundle.example_key` defaults to `lausanne` for old
manifests; `version` also accepts `city-example@1`. Archive filenames are
`{example_key}.json` and `{example_key}.zip`. Content identity includes the city
key. The internal per-project adapter retains its historical Lausanne route
names and selects its bundle from private `example-selection.json`; callers
of the workspace hub use the city-qualified public route.

`WorkspaceInfo` adds `example_project_ids` and `example_job_ids` arrays. Legacy
singular IDs remain populated with the first available example. Workspace
initialization installs all available bundled cities independently and remains
idempotent; the frontend monitors each pending import. The generated Python/API
schema and TypeScript contract snapshot is stored with the test-owned release baselines under
`tests/fixtures/release_baselines/openapi.json`.

### MR32 source feature preview

`POST /api/v1/workbench/feature-source/map` accepts `FeatureSelection` and returns bounded EPSG:4326 GeoJSON of immutable input geometry and original nonnegative values (or unit display values for unweighted geometry). It supports points, lines, polygons, mapped coordinate tables and year selection. Missing CRS, invalid geometry, inconsistent values and configured map limits fail explicitly. This visualization never changes prepared weights. Prepared feature maps discover their environment through dataset dependencies, including composite grid-feature artifacts; they are not restricted to one producing algorithm version.

MR41 road acquisition uses 25 km² initial query tiles, timeout-triggered polygon bisection (maximum depth three), cached complete raw responses and type/ID deduplication before whole-network construction. Tile boundaries do not become graph truncation boundaries. Road request admission is limited to 128 attempts and 600 elapsed seconds; an in-flight socket may exceed the admission deadline. Receipts retain subdivision/endpoint outcomes. Progress phases include `environment.network.osm.subdivide` and `environment.network.osm.assemble`. Successful final caches and scientific routing semantics remain compatible.

MR42 supersedes the fixed MR41 tile area with `max(25 km², buffered query convex-hull area / 8)`. OSMnx may emit more than eight initial tiles; receipts record the actual count. Road requests retain the `drive` filter and declare 64 MiB maximum server memory. Explicit dispatcher admission refusals try another endpoint without subdividing; computation timeouts/memory errors retain bounded subdivision. Endpoint failure counts determine subsequent attempt order within the acquisition. Progress additionally includes `environment.network.osm.tile_N_of_M[.server_N_of_M.HOST]`. Transport diagnostics preserve the server error text. Memory and polygon-planning hooks are restored on exit. No partial network is published.

MR43 replaces the road divisor 8 with 4. A multi-category OSM feature acquisition receipt adds ordered `categories` and stores the deterministic union `tags`; a single-category receipt retains the historical `category`/`tags` shape and cache identity. Combined responses retain only classification tags plus OSM element identity and geometry. Local category recovery applies the original OR-within-category predicates before the unchanged aggregation. Feature requests use the same bounded timeout subdivision and complete-response cache as roads. Progress uses `environment.features.osm.batch[.tile_N_of_M][.server_N_of_M.HOST]` for multi-category retrieval. No public API request or response model changes.
