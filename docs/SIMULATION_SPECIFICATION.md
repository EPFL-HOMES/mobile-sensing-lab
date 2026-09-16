# Simulation Specification


## MR26 equivalent execution acceleration

The waiting-task set is indexed explicitly and sorted by the same canonical keys at dispatch snapshots; task transitions update this index without changing event phases or semantic RNG streams. Routing uses integer node/edge ranks preserving lexical edge order. Bulk requests may recover minimum lexical paths from native directed shortest distances only when tight edges form a strictly increasing floating-point distance DAG. Any failed tightness, missing predecessor or degenerate increment uses the original heap algorithm. Route totals retain the same edge sequence and `fsum`; this is an equivalent implementation, not a new routing objective or an approximate cost matrix. Independent small-graph oracles and byte-identical real pilot output provide executed evidence.

Status: normative implementation baseline, 2026-09-07. This document operationalizes the immutable [framework](MOBILE_SENSING_FRAMEWORK.tex). Contract names are defined in [INTERFACES.md](INTERFACES.md); ingestion is defined separately in [DATA_INGESTION_SPECIFICATION.md](DATA_INGESTION_SPECIFICATION.md).

## 1. Model and first-release assumptions

One replication contains a common environment, all configured fleets, a fixed physical-vehicle catalog, released tasks, and one global event queue. Vehicles execute at most one immutable task plan at a time. Fleet labels have no kernel meaning. Tasks from one fleet are assigned only to that fleet's vehicles in v1; the shared dispatch mechanism and routing service are still common.

The baseline assumes static, positive directed-edge travel times, exogenous demand, no traffic feedback, no pooling, and non-preemptive service tasks except mandatory shift/horizon truncation. Route and service durations are known when a task is assigned. Operating-duration sensing includes powered movement, service, waiting, and idling while excluding stationary depot residence and inferred off-duty intervals. Legacy movement-duration artifacts retain their recorded interpretation.

Let the execution interval be \([H_s,H_1)\), with observation interval \([H_0,H_1)\), where \(H_s\le H_0<H_1\). Earlier execution supports scheduled services already in progress at \(H_0\). Reporting bins partition the observation interval independently of event times. Portfolio parameters are absent from the simulation configuration.

Initialize tasks released before H_s as waiting with their original release timestamps; never enqueue an event in the past. V1 has no arbitrary partially executed vehicle-state snapshot input: a vehicle whose required duty started before H_s requires an earlier simulation start, validated before execution. Scheduled reconstruction proposes an H_s covering complete selected duties; the user can retain a later observation start. This avoids teleporting a bus to its first stop at the start of an observation window. Vehicles wholly outside the execution interval remain catalog entries with no activity.

## 2. Tasks, vehicles, and capacity

Preserve the framework contract

\[
T_i=(r_i,[s_{i1},\ldots,s_{im_i}]),\qquad
s_{ij}=(\ell_{ij},\bar t_{ij},\tau_{ij}).
\]

\(m_i\ge1\), release time is finite, \(\tau_{ij}\ge0\), and scheduled times are optional. Release makes a task eligible to wait, not necessarily immediately executable. The first step is the service-entry location for area membership and pickup travel time. OD pickup/delivery and scheduled stop sequences preserve their submitted order. There is no hidden TSP or task reordering in the executor.

Each physical vehicle has stable key \((k,v)\), individual availability \([a_v,b_v)\), initial snapped location, and optional depot, capacity, and explicit area assignment. If a generated availability realization makes a vehicle inactive, retain it in the catalog and record `active=false`; its exposure is zero. Do not generate a new catalog in each replication.

Task states: `unreleased`, `waiting`, `assigned`, `completed`, `rejected`, `interrupted_shift`, `censored_horizon`, `unserved_horizon`. Vehicle states: `inactive`, `idle`, `executing_service`, `executing_operational`, `off_shift`. Travel/service/wait substates are derived from activity intervals. Only idle vehicles can receive a new task. Current location is not the planned task's endpoint until execution reaches that point.

Capacity modes:

- `none`: no quantity restriction.
- `consumable`: initial remaining stock \(c_v=C_v\); nonnegative task demand consumes stock at the declared service milestone. No partial service. A depot-return task can reset stock after a positive configured replenishment time.
- `occupancy`: each step has an explicit signed change in remaining free capacity, e.g. pickup \(-q\), drop-off \(+q\). No pooling; a task occupies the vehicle until its final step. Depot replenishment does not apply.

For a planned sequence of capacity deltas \(\Delta_{ij}\), require

\[
0\le c_v+\sum_{h=1}^{j}\Delta_{ih}\le C_v
\quad\text{at every capacity milestone }j.
\]

Quantity units must agree within a fleet. A task requiring more than every otherwise-compatible vehicle's full capacity is structurally infeasible. Report it explicitly; never repeatedly send vehicles to a depot for it. Capacity changes and resets take effect at their execution milestone, not at assignment. A busy vehicle is already reserved, so no second assignment can consume the same capacity.

## 3. Time and event ordering

Represent elapsed time in float64 seconds; imported timestamps resolve to explicit UTC-origin elapsed seconds. Never round event time to a reporting bin. Finite nonnegative durations are mandatory. Every strictly positive operational movement advances representable time; reject a duration that produces `start + duration == start` instead of spinning.

Use a binary heap with key `(time_s, phase, stable_entity_key, sequence)`. Stable keys derive from canonical IDs, not Python object hashes or process arrival order. At a given time, process the following phases:

1. Complete executions ending now and apply their milestones/capacity updates.
2. Apply vehicle exits. A task finishing exactly at \(b_v\) completes, then its vehicle leaves; no new assignment at \(b_v\).
3. Apply vehicle entries.
4. Release all tasks at this time and process valid policy wakeups.
5. Dispatch against the complete updated snapshot.
6. For remaining idle vehicles, evaluate operational policies.

Coalesce all exogenous events at a timestamp before dispatch. If assignment creates a zero-duration task completion, process completion and another decision micro-round at the same time until closure; do not rerun entry/release events or emit duplicate assignments. This closure is finite because each zero-duration service task leaves the waiting pool once. Require positive duration for generated cruising/replenishment actions and enforce a diagnostic micro-round limit for invalid plugin behavior.

Use execution generation tokens to ignore stale completion/wakeup events after truncation or cancellation. Do not use floating tolerances to merge genuinely distinct timestamps. Tolerances belong in numerical checks, not event priority.

At \(H_1\), perform a terminal finalization: complete tasks whose plan ends exactly at \(H_1\), clip active movements and activities, and classify unfinished/waiting tasks. Do not release new tasks, activate vehicles, or dispatch at \(H_1\). Time integrals remain half-open. Events strictly after \(H_1\) never execute.

The baseline kernel implementation is versioned as `event-kernel@1`. Heap records use the four `EventPhase` values above; dispatch and operational-policy evaluation are synchronous closure actions rather than queued event phases. Its execution outcome distinguishes `planned_end_s` from the cutoff-bounded `realized_end_s`. M05A establishes this lifecycle boundary; M05B is responsible for materializing exact partial movement/service state at a cutoff.

## 4. Eligibility and centralized dispatch

At time \(t\), feasible vehicle-task pairs require all of:

\[
v\text{ idle},\quad a_v\le t<b_v,\quad r_i\le t,\quad
k(v)=k(i),\quad \mathrm{AreaAllowed}(v,i),\quad
\mathrm{CapacityAllowed}(v,i),\quad \mathrm{Reachable}(v,i).
\]

An optional `max_pickup_time_s` restricts shortest travel time from the current vehicle node to the first task step. It is not a Euclidean radius and does not change area assignment. Task internal legs must also be routable; uploaded tasks are checked before execution, generated tasks are checked before acceptance. The baseline does not require completion before shift end as an eligibility rule; the hard-exit rule below governs truncation and its consequences are visible.

Area definitions are polygons/regions with IDs. Vehicle-area assignments are a separate mapping. A task belongs to every area covering its entry location, with deterministic handling of boundary points; it is eligible when its membership intersects the vehicle's assigned set. A fleet with area constraints rejects missing vehicle assignments. Unrestricted fleets ignore area membership. Depots can be outside all areas. Service areas constrain task entry locations, not every traversed road or the destination.

### Predefined

The assignment plan maps tasks to vehicles and gives a total order per vehicle. Only the next uncompleted planned task may start, and only after release and all ordinary eligibility rules. Wait if it is unreleased; do not skip it silently to serve a later task. Invalid IDs, duplicate owners, duplicate order indices, incompatible capacity, and impossible internal routes are pre-run errors. A vehicle is allowed to enter later than a task's release. Lateness from the executed schedule is recorded rather than hidden through forced timing.

### Nearest Matching

Define pair cost \(d_{vi}\) as the shared routing service's shortest pickup travel time. Baseline `nearest_matching@1` is centralized greedy edge matching: order feasible pairs by `(d_vi, task.release_s, task_id, fleet_id, vehicle_id)`; accept each pair if neither endpoint has already been assigned in this dispatch round. Assignments are one-to-one. This is an explicitly defined nearest-pair heuristic, not a minimum-total-cost bipartite solver and not FCFS nearest-vehicle dispatch.

Implement spatial/area/capacity pruning and one source-tree query per distinct vehicle node when beneficial. A materialized feasible-pair list is allowed only within the configured memory/pair limit. Exceeding the limit produces an actionable diagnostic rather than silently taking nearest top-k candidates, changing algorithms, or exhausting memory. A future streaming implementation must preserve the same pair ordering and results.

If \(Q\) waiting tasks, \(A\) available sources, \(P\le AQ\) feasible pairs, and graph size is \((N,M)\), an initial worst-case round costs approximately \(O(A(M+N)\log N+P\log P)\), with \(O(P)\) matching storage. State this limit in benchmark reports; no subquadratic guarantee is claimed. Empty pools simply produce no assignments.

## 5. Shared routing and network semantics

Prepare a directed multigraph with stable edge IDs, correctly oriented geometry, positive metric lengths, and positive static travel-time weights. Parallel arcs remain distinct. Resolve equal-cost route choices deterministically using stable edge ordering. Dispatch estimates and executor routes must agree under the same profile hash.

For supplied travel times use the validated edge time directly and derive an implied speed for diagnostics. Otherwise use explicit speed metadata, then a declared class-speed mapping, then an explicitly configured fallback. Do not silently equate observed maximum speed with observed travel speed. Record speed/time source counts. The bundled encoded roads have no speed attribute; their baseline must declare an assumed speed model.

Snap all service/initial/depot locations to validated network nodes in metric CRS. v1 uses nearest-node execution with a maximum snap distance. Original and snapped positions and displacement are retained. No off-network travel or sensing is fabricated. A location beyond tolerance is rejected or quarantined according to the explicit import policy. The user may change the tolerance and rerun preprocessing; the implementation may not automatically increase it until a route succeeds.

Unreachable routes return an explicit result, never a straight-line fallback, teleport, infinite-time scheduled event, or zero-time movement. Same-node routing returns a valid empty leg. A same-node task with positive service time still occupies the vehicle.

Keep complete network arcs within the chosen routing extent; do not clip arcs to the sensing boundary and retain their old endpoint IDs. If trimming is necessary, rebuild topologically valid segments/nodes. The routing extent may exceed the sensing region. Directed reverse arcs reverse coordinate order as well as endpoints. Normalize contiguous MultiLineStrings into oriented LineStrings. An explicit conservative policy may split connected, bidirectional, two-terminal component linework at existing component endpoints and may split a reused source node ID into deterministic diameter-bounded coordinate clusters. It must quarantine disconnected, nonlineal, zero-length, directionally ambiguous, or terminal-unresolved records; it never bridges a gap. Do not infer connectivity at interior geometric road crossings without evidence of an actual junction. Repair policy, tolerance, raw hash, actions, lineage, quarantine and scenario-impact evidence are part of scientific identity.

Route-cache keys include network hash, profile hash, and ordered origin/destination. Bound caches by measured memory. A future time-dependent profile must extend this contract with departure time and FIFO/feasibility semantics; v1 must reject it rather than pretend its costs are static.

## 6. Task execution and scheduled timing

At assignment time \(t\), construct the complete plan using current location and the immutable ordered steps. For step \(j\), let routed arrival be \(A_j\), target be \(\bar t_j\) if supplied, service start \(B_j\), and departure \(D_j\):

\[
B_j=\max(A_j,\bar t_j)\quad\text{or }B_j=A_j\text{ without a target},
\qquad D_j=B_j+\tau_j,
\qquad A_{j+1}=D_j+\operatorname{TravelTime}(\ell_j,\ell_{j+1}).
\]

Early arrival creates stationary waiting; late arrival propagates lateness. Preserve the configured service duration even when late. For GTFS, `scheduled_time_s` is scheduled arrival and service duration is departure minus arrival. Route timing comes from the shared travel-time profile. Do not compress travel times to make every timetable feasible. Scheduled stop times and actual executed times are distinct output columns.

The plan contains pickup/deadhead movement, inter-step movement, scheduled waits, service intervals, and capacity milestones. Normally schedule only its end/next availability event; detailed milestones are persisted and applied analytically as needed. Decision-irrelevant stop arrivals do not require global events. Materialize vehicle state at an intervening exit by locating that time in the plan, never by reading its future endpoint.

Baseline shift handling is `hard_exit`: truncate at \(b_v\), including a fractional edge when necessary. A noncompleted service task becomes `interrupted_shift` and is not requeued automatically. At horizon end use `censored_horizon`; still-waiting tasks become `unserved_horizon`. Precompute the effective plan cutoff \(\min(b_v,H_1)\) for committed movement records, and never publish planned travel beyond it as realized exposure. An optional finish-current-task policy is deferred, not an undocumented default.

## 7. Operational policies

Operational tasks use exactly the same routing, execution, logging, and exposure contracts as service tasks. They are created only after service dispatch at a decision timestamp. Their `kind` and source policy version are recorded.

### Stationary idling

Remain at the current node until a release, entry, or other meaningful decision event. Do not generate periodic idle ticks. At finalization, close the idle activity interval for utilization accounting.

### Random cruising

When idle and no service assignment is available, select uniformly from the canonically sorted reachable outgoing neighboring nodes that have a positive-duration route fitting the remaining shift/horizon time. Continue this random walk until it reaches or crosses the next kernel decision epoch, then retain the complete walk as one `cruise` operating execution. Exclude the immediately previous node if another candidate exists; otherwise allow reversal. This is a simple reproducible baseline, not an attraction or calibrated taxi-search model.

Cruising is non-preemptive within the current directed edge. A task released mid-edge waits until that edge ends; the vehicle is reconsidered immediately afterward. Internal edge transitions do not create separate demand tasks or persisted operational events. Exact edge movement intervals remain the source for maps and exposure. If the walk reaches a dead end before the next decision epoch, the powered-on stationary remainder is retained in the same operating execution. If no positive feasible first leg exists, remain idle; do not spin at the same timestamp. Cruising may travel beyond an assigned service area because areas restrict task entry, not paths. Selection uses a named stream keyed by replication, fleet, vehicle, policy version, and transition counter.

### Depot return

Only `consumable` vehicles with a valid depot can replenish. Trigger after service matching when there is at least one waiting task compatible with fleet/area/full capacity, no such currently assignable task fits remaining capacity, and a reachable depot exists. Generate depot travel plus positive replenishment service, then reset to \(C_v\) at its completion. If already at the depot, travel is empty but replenishment is still positive. If current stock already equals full capacity, a return cannot fix the blockage and must not be generated. Inactive/full-stock/unreachable/too-large-task cases are diagnostic, not repeated return loops. A capacity-constrained consumable fleet without a depot may operate until stock is exhausted; its configuration warns that no replenishment is available.

Priority after dispatch: required depot return, then cruising, then stationary idling. Do not auto-return merely because no demand exists, and do not inject end-of-day depot movements unless configured as explicit tasks.

## 8. Demand generation and randomness

The initial generated process is a piecewise-constant Poisson process. For interval \([b_j,b_{j+1})\), duration \(\Delta_j\), and intensity \(\lambda_j\) tasks/second, draw \(N_j\sim\operatorname{Poisson}(\lambda_j\Delta_j)\), then sorted uniform release times conditional on \(N_j\). Locations are drawn from normalized spatial weights or an explicit sparse OD table; service durations and quantities are nonnegative configurable constants in the baseline. A later installed distribution adapter may extend these without kernel changes.

Offline and online modes use the same interval realization and event IDs. Online mode materializes at most the required interval/chunk and merges its release stream into the event queue; it must produce identical tasks to offline mode under identical config/seed. This is not a periodic dispatch or fixed-step simulation. Zero intensity yields no tasks, all-zero spatial weights are a validation error, and zero-distance OD tasks remain valid service tasks.

Use a root master seed and semantic stream derivation from stable serialized labels. Stream separation includes supply, demand arrivals, locations/OD, service, shared scenario factors, and vehicle cruising. Do not include sensor count, candidate ID, reporting resolution, worker number, job ID, preview count, or execution order. Preview uses a separate named preview stream and cannot consume a simulation stream. Preserve seed manifests and normalized sampled inputs sufficient to replay each replication.

Replication \(r\) is a joint scenario across all fleets. Baseline fleet generators are independent conditional on fixed environment unless `joint_scenario_model` explicitly supplies shared factors. Record that model assumption; the portfolio evaluator never reconstructs covariance from independence assumptions. IID replication statistics require independent semantic replication streams with the same model; calendar-day heterogeneity must be treated as a declared scenario mixture, not silently labeled IID.

## 9. Exact interval exposure

For an oriented edge, precompute disjoint ordered pieces \((g,f_0,f_1)\), \(0\le f_0<f_1\le1\), where fractions measure distance from its source. Repeated entry into the same cell produces multiple pieces. Include outside-grid pieces with null cell ID for conservation diagnostics. Overlapping uploaded grid interiors are invalid. A positive-length segment on a shared grid boundary belongs to one cell, deterministically the minimum canonical cell ID; point intersections contribute zero duration.

The `positive-length-road-grid@1` exposure domain retains a prepared cell iff its clipped sensing geometry intersects at least one prepared road edge with length greater than the geometry tolerance. A point-only contact cannot generate positive exposure and is excluded. This filters the exposure/utility axis and spatial-coverage denominator only; it does not remove the cell from the prepared environment, feature tables or demand-generation domain. The artifact records both prepared and eligible cell counts. Existing artifacts keep their original axis and semantics.

Assume constant speed within an edge. For movement interval \([a,b)\) traversing edge fractions \([p_0,p_1]\), a fraction \(f\) maps to

\[
\theta(f)=a+(b-a)\frac{f-p_0}{p_1-p_0}.
\]

Intersect a grid piece with \([p_0,p_1]\) before mapping it to time. For each bin \([h_t,h_{t+1})\), its contribution is

\[
\max\{0,\min(\theta(f_1),h_{t+1},H_1)
-\max(\theta(f_0),h_t,H_0)\}.
\]

Sum these contributions into sparse \(E^{(r)}_{k,v,g,t}\). No one-second GPS sampling, independent length-fraction allocation to every crossed time bin, or normalization by only the in-region length is allowed. Those methods can misplace time or inflate exposure near boundaries.

Default active kinds include all road movement: service pickup/deadhead, inter-step service movement, cruise, depot return, and reposition. The mask is an exposure setting. Waiting/service durations have zero exposure under the v1 sensing definition. Concurrent selected vehicles may contribute more than one bin's duration in total; per individual vehicle, however,

\[
0\le\sum_g E^{(r)}_{k,v,g,t}\le h_{t+1}-h_t,
\qquad
\sum_{g,t}E^{(r)}_{k,v,g,t}
=\text{in-grid sensor-active moving time in the observation horizon}.
\]

Use float64 accumulation and deterministic reduction order. Default verification tolerance for duration conservation is \(10^{-6}\) seconds absolute plus \(10^{-10}\) relative; record any revised justified tolerance. Geometry tolerances are separately metric and must not repair a physically material gap silently. Never round small positive exposures to integers.

Retained movement permits a new grid or arbitrary new bins to be allocated without rerunning mobility. Summing existing bins is exact only for aligned coarsening. Unaligned or finer bins require movement intervals. Nonlinear portfolio utility must be reevaluated after any exposure-axis change.

The required logical output is a complete matrix for every `(replication_id, fleet_id, vehicle_id)`, including vehicles with all-zero exposure. Physical storage is the sparse long table plus complete catalog/grid/bin/replication manifests; absence of a standalone dense matrix file does not mean a vehicle is missing. Provide matrix readers/exports for individual vehicles and aggregate scopes. For an aggregate scope A, compute `sum(v in A, E[r,v])` within r before taking mean/std across replications. This same immutable E artifact feeds the portfolio sampler; aggregated dashboard tables cannot replace it.

## 10. Operational metrics and acceptance

Report counts separately for tasks released in the observation window and carry-in tasks. For the observation cohort, partition outcomes into completed, rejected, interrupted shift, censored active, and unserved waiting; denominators are explicit. Mean assignment wait uses assigned tasks, mean first-service wait uses tasks reaching first service, and neither silently treats unserved tasks as zero wait. Completion rate includes the selected cohort denominator. Utilization is busy active time divided by active available time in the observation horizon; report service and operational travel separately. Zero denominators produce null with an explanation.

Required tests:

1. A common executor handles location, OD, and ordered tasks; changing a fleet display label changes no output.
2. Simultaneous release/entry/completion/exit is invariant to input row order; zero-time service closure terminates.
3. A vehicle cannot serve before entry, receive two assignments, or produce realized motion after exit/horizon; exact-boundary completion follows the declared convention.
4. Directed reachability, parallel arcs, reverse geometry, same-node legs, and snap failures have hand-checkable cases.
5. Predefined order, nearest-pair tie-breaking, area assignment independent of depot, quantity prefixes, and depot-loop prevention are verified.
6. Random cruising is repeatable, groups edge transitions between decision epochs, stops at a dead end, emits no demand-task outcomes, and preserves edge-level movement/exposure; mid-edge demand follows non-preemption semantics.
7. A traversal crossing both cell and time boundaries matches analytic exposure. Repeat crossings, shared boundaries, outside-grid pieces, partial edges, and empty movement conserve duration.
8. Different reporting bins/grid change no movement; different portfolio subsets change no simulation artifact.
9. Offline/online demand, shuffled input, preview calls, and workers 1 versus multiple preserve normalized scientific tables under one locked runtime.
10. Cancelled/failed replications never appear in completed exposure datasets; empty complete replications are legitimate zero-exposure observations.

The release must pass tiny analytic fixtures before a bounded Lausanne smoke. Large benchmark results report events, routing/cache work, matching-pair counts, output rows, elapsed time, and peak memory; do not infer model validity from runtime alone.


## MR20 extension: operating sensing and planned depot replenishment

Exposure includes on-duty movement, service, wait and idle intervals, excluding every stationary interval at the fleet's declared depot. A declared timetable long-idle threshold marks an entire unassigned idle interval off duty, preserving the same physical vehicle across daily trips; this is an explicit inferred power schedule, not observed ignition data. Half-open temporal clipping, deterministic point-to-cell ownership and per-vehicle conservation apply. Depot residence does not suppress sensing elsewhere in the same cell.

Single-depot One-shot optionally inserts reload visits under a bounded visit count. Initial stock is full without an initial loading delay. Each reload consumes the configured minimum depot residence before resetting stock. Final depot return carries no capacity reset and no artificial loading delay. All tasks remain mandatory and separate; equal-location requests are preferentially made consecutive when time/capacity/area constraints permit. The planner and executor share route times and stock milestone semantics.
