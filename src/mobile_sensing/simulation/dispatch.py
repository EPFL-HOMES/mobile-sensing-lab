"""M05B eligibility and deterministic centralized dispatch policies."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from mobile_sensing.contracts import (
    AssignmentPlan,
    FleetConfig,
    LocationRef,
    NamedRngStreamProvider,
    Task,
    VehicleAvailability,
    VehicleKey,
    VehicleSpec,
    VehicleState,
    VehicleStatus,
)
from mobile_sensing.contracts.protocols import RoutingService
from mobile_sensing.simulation.executor import GenericTaskExecutor, SharedRoutingCache
from mobile_sensing.simulation.kernel import DecisionSnapshot, PlannedAssignment
from mobile_sensing.simulation.operational import (
    OperationalDiagnostic,
    OperationalPolicyEngine,
)
from mobile_sensing.simulation.timing import UnreachableTaskLeg, estimate_task_timing


PREDEFINED_DISPATCH_VERSION = "dispatch.predefined@2"
NEAREST_MATCHING_VERSION = "dispatch.nearest_matching@1"
DEFAULT_MAX_FEASIBLE_PAIRS = 250_000

VehicleTuple = tuple[str, str]
TaskTuple = tuple[str, str]


class ScenarioDispatchError(ValueError):
    """Raised before simulation when dispatch inputs are structurally invalid."""


class MatchingPairLimitExceeded(RuntimeError):
    """Raised instead of allocating an unbounded feasible-pair matrix."""


@dataclass(frozen=True, slots=True)
class FeasiblePair:
    vehicle: VehicleKey
    task: Task
    pickup_time_s: float

    @property
    def order_key(self) -> tuple[float, float, str, str, str]:
        return (
            self.pickup_time_s,
            self.task.release_s,
            self.task.task_id,
            self.task.fleet_id,
            self.vehicle.vehicle_id,
        )


def _vehicle_tuple(key: VehicleKey) -> VehicleTuple:
    return (key.fleet_id, key.vehicle_id)


def _capacity_allowed(vehicle: VehicleState, spec: VehicleSpec, task: Task) -> bool:
    if spec.capacity_mode == "none":
        return True
    if vehicle.remaining_capacity is None or spec.capacity is None:
        raise ScenarioDispatchError("finite-capacity vehicle lacks runtime capacity")
    if spec.capacity_mode == "consumable":
        return float(task.required_capacity or 0.0) <= vehicle.remaining_capacity
    remaining = vehicle.remaining_capacity
    for step in task.steps:
        remaining += float(step.quantity_delta or 0.0)
        if remaining < 0.0 or remaining > spec.capacity:
            return False
    return True


class EligibilityEngine:
    """Evaluate the complete v1 eligibility predicate using shared route queries."""

    def __init__(
        self,
        *,
        fleet_configs: Mapping[str, FleetConfig],
        vehicle_specs: Mapping[VehicleTuple, VehicleSpec],
        locations: Mapping[str, LocationRef],
        location_area_ids: Mapping[str, Sequence[str]],
        routing: RoutingService,
    ) -> None:
        self.fleets = dict(fleet_configs)
        self.specs = dict(vehicle_specs)
        self.locations = dict(locations)
        self.location_area_ids = {
            location_id: frozenset(area_ids) for location_id, area_ids in location_area_ids.items()
        }
        self.routing = routing

    def _area_allowed(self, spec: VehicleSpec, task: Task) -> bool:
        fleet = self.fleets[task.fleet_id]
        constrained = fleet.supply.area_assignments_ref is not None
        if not constrained:
            return True
        # Service areas constrain customer demand. Depot returns and reloads are
        # common operational facilities and must remain reachable by every
        # assigned subfleet; the depot never determines a vehicle's area.
        if task.kind == "depot_return":
            return True
        membership = self.location_area_ids.get(task.steps[0].location_id, frozenset())
        return bool(membership.intersection(spec.assigned_area_ids))

    def eligible_without_route(self, vehicle: VehicleState, task: Task, *, time_s: float) -> bool:
        """Apply all inexpensive eligibility predicates before shortest-path work."""

        key = _vehicle_tuple(vehicle.key)
        spec = self.specs[key]
        return (
            vehicle.status == VehicleStatus.IDLE
            and spec.availability_start_s <= time_s < spec.availability_end_s
            and task.release_s <= time_s
            and task.fleet_id == vehicle.key.fleet_id
            and self._area_allowed(spec, task)
            and _capacity_allowed(vehicle, spec, task)
        )

    def pair(self, vehicle: VehicleState, task: Task, *, time_s: float) -> FeasiblePair | None:
        if not self.eligible_without_route(vehicle, task, time_s=time_s):
            return None
        source = self.locations[vehicle.committed_location_id]
        target = self.locations[task.steps[0].location_id]
        assert source.node_id is not None and target.node_id is not None
        fleet = self.fleets[task.fleet_id]
        route = self.routing.route(fleet.routing.profile_id, source.node_id, target.node_id)
        if not route.reachable:
            return None
        limit = getattr(fleet.dispatch, "max_pickup_time_s", None)
        if limit is not None and route.total_duration_s > limit:
            return None
        return FeasiblePair(vehicle=vehicle.key, task=task, pickup_time_s=route.total_duration_s)


class SimulationDecisionAdapter:
    """Central service-dispatch and post-dispatch operational-policy adapter."""

    def __init__(
        self,
        *,
        fleets: Sequence[FleetConfig],
        tasks: Sequence[Task],
        vehicle_specs: Sequence[VehicleSpec],
        locations: Mapping[str, LocationRef],
        routing: RoutingService,
        assignment_plans: Mapping[str, AssignmentPlan] | None = None,
        location_area_ids: Mapping[str, Sequence[str]] | None = None,
        max_feasible_pairs: int = DEFAULT_MAX_FEASIBLE_PAIRS,
        route_cache_entries: int = 4096,
        replication_id: str | None = None,
        simulation_end_s: float | None = None,
        availability: Sequence[VehicleAvailability] | None = None,
        rng: NamedRngStreamProvider | None = None,
    ) -> None:
        if isinstance(max_feasible_pairs, bool) or max_feasible_pairs <= 0:
            raise ValueError("max_feasible_pairs must be a positive integer")
        self.max_feasible_pairs = max_feasible_pairs
        self.fleets = {fleet.fleet_id: fleet for fleet in fleets}
        if len(self.fleets) != len(fleets):
            raise ScenarioDispatchError("fleet IDs must be unique")
        self.tasks = {(task.fleet_id, task.task_id): task for task in tasks}
        if len(self.tasks) != len(tasks):
            raise ScenarioDispatchError("task keys must be unique")
        self.specs = {_vehicle_tuple(spec.key): spec for spec in vehicle_specs}
        if len(self.specs) != len(vehicle_specs):
            raise ScenarioDispatchError("vehicle keys must be unique")
        self.locations = dict(locations)
        self.routing = SharedRoutingCache(routing, max_entries=route_cache_entries)
        self.assignment_plans = dict(assignment_plans or {})
        self.location_area_ids = location_area_ids or {}
        self._predefined: dict[VehicleTuple, tuple[TaskTuple, ...]] = {}
        self._predefined_cursor: dict[VehicleTuple, int] = {}
        self._batch_consumed_time: dict[str, float] = {}
        self._matching_snapshots = {}
        self._configuration_diagnostics: list[OperationalDiagnostic] = []
        self._validate_inputs()
        self.eligibility = EligibilityEngine(
            fleet_configs=self.fleets,
            vehicle_specs=self.specs,
            locations=self.locations,
            location_area_ids=self.location_area_ids,
            routing=self.routing,
        )
        self.executor = GenericTaskExecutor(
            locations=self.locations,
            routing=self.routing,
            profile_ids={key: fleet.routing.profile_id for key, fleet in self.fleets.items()},
            capacity_modes={key: spec.capacity_mode for key, spec in self.specs.items()},
        )
        self.operational = self._build_operational_engine(
            replication_id=replication_id,
            simulation_end_s=simulation_end_s,
            availability=availability,
            rng=rng,
        )

    def _build_operational_engine(
        self,
        *,
        replication_id: str | None,
        simulation_end_s: float | None,
        availability: Sequence[VehicleAvailability] | None,
        rng: NamedRngStreamProvider | None,
    ) -> OperationalPolicyEngine | None:
        context = (replication_id, simulation_end_s, availability, rng)
        requested = any(
            fleet.supply.idle_policy.policy == "random_cruise"
            or fleet.supply.depot_policy is not None
            for fleet in self.fleets.values()
        )
        if all(value is None for value in context):
            if requested:
                raise ScenarioDispatchError(
                    "operational policies require replication_id, simulation_end_s, "
                    "availability, and semantic RNG context"
                )
            return None
        if any(value is None for value in context):
            raise ScenarioDispatchError("operational policy context must be supplied completely")
        if (
            any(
                fleet.supply.idle_policy.policy == "random_cruise" for fleet in self.fleets.values()
            )
            and not self.routing.supports_operational_cruise
        ):
            raise ScenarioDispatchError(
                "random cruising requires routing neighbor queries and prepared node locations"
            )
        assert replication_id is not None
        assert simulation_end_s is not None
        assert availability is not None
        assert rng is not None
        rows = {(row.vehicle.fleet_id, row.vehicle.vehicle_id): row for row in availability}
        if set(rows) != set(self.specs):
            raise ScenarioDispatchError("operational availability must cover the catalog exactly")
        availability_end_s = {}
        for key, row in rows.items():
            if row.replication_id != replication_id:
                raise ScenarioDispatchError("operational availability replication_id mismatch")
            if row.active:
                assert row.availability_end_s is not None
                availability_end_s[key] = row.availability_end_s
        return OperationalPolicyEngine(
            replication_id=replication_id,
            simulation_end_s=simulation_end_s,
            fleets=self.fleets,
            vehicle_specs=self.specs,
            availability_end_s=availability_end_s,
            locations=self.locations,
            routing=self.routing,
            executor=self.executor,
            rng=rng,
            pair_evaluator=lambda vehicle, task, time_s: self.eligibility.pair(
                vehicle, task, time_s=time_s
            ),
            candidate_tasks=self._operational_candidate_tasks,
            register_location=self._register_operational_location,
        )

    def _register_operational_location(self, location: LocationRef) -> None:
        """Publish a generated network-node location to every shared planning view."""

        self.locations[location.location_id] = location
        self.eligibility.locations[location.location_id] = location
        self.executor.locations[location.location_id] = location

    def _validate_inputs(self) -> None:
        for task in self.tasks.values():
            if task.fleet_id not in self.fleets:
                raise ScenarioDispatchError(f"task references unknown fleet {task.fleet_id!r}")
            for step in task.steps:
                location = self.locations.get(step.location_id)
                if location is None or location.node_id is None:
                    raise ScenarioDispatchError(
                        f"task {task.task_id!r} references an unresolved location"
                    )
            fleet = self.fleets[task.fleet_id]
            if fleet.supply.capacity.mode == "consumable":
                amount = float(task.required_capacity or 0.0)
                deltas = tuple(
                    float(step.quantity_delta)
                    for step in task.steps
                    if step.quantity_delta not in (None, 0.0)
                )
                if deltas and (
                    any(delta > 0.0 for delta in deltas)
                    or not math.isclose(-math.fsum(deltas), amount, rel_tol=1e-12, abs_tol=1e-9)
                ):
                    raise ScenarioDispatchError(
                        f"task {task.task_id!r} has inconsistent consumable milestones"
                    )
            if (
                fleet.dispatch.policy in {"nearest_matching", "batch_nearest_matching"}
                and task.kind != "service"
            ):
                raise ScenarioDispatchError(
                    "nearest matching accepts service tasks only; operational tasks are generated "
                    "after service dispatch"
                )
            try:
                if self.routing.supports_reachability_queries:
                    for left, right in zip(task.steps, task.steps[1:]):
                        if not self.routing.reachable(
                            fleet.routing.profile_id,
                            self.locations[left.location_id].node_id,
                            self.locations[right.location_id].node_id,
                        ):
                            raise ScenarioDispatchError(
                                f"task {task.task_id!r} has an unreachable internal route"
                            )
                else:
                    estimate_task_timing(
                        task,
                        start_location_id=task.steps[0].location_id,
                        start_s=task.release_s,
                        locations=self.locations,
                        routing=self.routing,
                        profile_id=fleet.routing.profile_id,
                    )
            except UnreachableTaskLeg as exc:
                raise ScenarioDispatchError(
                    f"task {task.task_id!r} has an unreachable internal route: {exc}"
                ) from exc
        for key, spec in self.specs.items():
            if key[0] not in self.fleets:
                raise ScenarioDispatchError(f"vehicle references unknown fleet {key[0]!r}")
            location = self.locations.get(spec.initial_location_id)
            if location is None or location.node_id is None:
                raise ScenarioDispatchError(f"vehicle {key!r} has an unresolved initial location")
            fleet = self.fleets[key[0]]
            if spec.capacity_mode != fleet.supply.capacity.mode:
                raise ScenarioDispatchError(
                    f"vehicle {key!r} capacity mode disagrees with its fleet configuration"
                )
            configured_unit = getattr(fleet.supply.capacity, "unit", None)
            if spec.quantity_unit != configured_unit:
                raise ScenarioDispatchError(
                    f"vehicle {key!r} quantity unit disagrees with its fleet configuration"
                )
            if fleet.supply.area_assignments_ref is not None and not spec.assigned_area_ids:
                raise ScenarioDispatchError(
                    f"area-constrained vehicle {key!r} has no explicit area assignment"
                )
            if fleet.supply.depot_policy is not None:
                if spec.depot_location_id is None:
                    raise ScenarioDispatchError(
                        f"depot policy vehicle {key!r} requires an explicit depot location"
                    )
                depot = self.locations.get(spec.depot_location_id)
                if depot is None or depot.node_id is None:
                    raise ScenarioDispatchError(
                        f"depot policy vehicle {key!r} has an unresolved depot location"
                    )
            elif spec.capacity_mode == "consumable" and spec.depot_location_id is None:
                self._configuration_diagnostics.append(
                    OperationalDiagnostic(
                        time_s=None,
                        vehicle=spec.key,
                        code="REPLENISHMENT_NOT_CONFIGURED",
                        message=(
                            "Consumable vehicle has no depot policy/location and cannot replenish."
                        ),
                    )
                )
        self._validate_structural_capacity()
        self._validate_predefined_plans()

    def _validate_structural_capacity(self) -> None:
        for task_key, task in self.tasks.items():
            specs = [spec for key, spec in self.specs.items() if key[0] == task.fleet_id]
            if not specs:
                raise ScenarioDispatchError(f"task fleet {task.fleet_id!r} has no vehicles")
            if any(
                _capacity_allowed(
                    VehicleState(
                        key=spec.key,
                        status=VehicleStatus.IDLE,
                        current_execution_id=None,
                        committed_location_id=spec.initial_location_id,
                        available_at_s=spec.availability_start_s,
                        remaining_capacity=spec.capacity,
                        execution_generation=0,
                    ),
                    spec,
                    task,
                )
                for spec in specs
            ):
                continue
            raise ScenarioDispatchError(
                f"task {task_key!r} violates every compatible vehicle's full-capacity bounds"
            )

    def _validate_predefined_plans(self) -> None:
        planned_tasks: set[TaskTuple] = set()
        for fleet_id, fleet in sorted(self.fleets.items()):
            if fleet.dispatch.policy not in {"predefined", "one_shot"}:
                continue
            try:
                plan = self.assignment_plans[fleet.dispatch.assignment_plan_ref]
            except KeyError as exc:
                raise ScenarioDispatchError(
                    f"missing assignment plan {fleet.dispatch.assignment_plan_ref!r}"
                ) from exc
            if plan.assignment_plan_id != fleet.dispatch.assignment_plan_ref:
                raise ScenarioDispatchError("assignment plan reference and ID disagree")
            by_vehicle: dict[VehicleTuple, list[tuple[int, TaskTuple]]] = {}
            for row in plan.rows:
                key = _vehicle_tuple(row.vehicle)
                task_key = (fleet_id, row.task_id)
                if key[0] != fleet_id or key not in self.specs:
                    raise ScenarioDispatchError("predefined row references an invalid vehicle")
                if task_key not in self.tasks:
                    raise ScenarioDispatchError("predefined row references an invalid task")
                by_vehicle.setdefault(key, []).append((row.order_index, task_key))
                planned_tasks.add(task_key)
            for key, rows in by_vehicle.items():
                rows.sort()
                if [index for index, _ in rows] != list(range(len(rows))):
                    raise ScenarioDispatchError(
                        f"predefined order for vehicle {key!r} must be contiguous from zero"
                    )
                sequence = tuple(task_key for _, task_key in rows)
                self._validate_predefined_reachability(key, sequence)
                self._predefined[key] = sequence
                self._predefined_cursor[key] = 0
            expected = {key for key in self.tasks if key[0] == fleet_id}
            if {key for key in planned_tasks if key[0] == fleet_id} != expected:
                raise ScenarioDispatchError(
                    f"predefined plan for fleet {fleet_id!r} must own every task exactly once"
                )

    def _validate_predefined_reachability(
        self, vehicle_key: VehicleTuple, sequence: Sequence[TaskTuple]
    ) -> None:
        spec = self.specs[vehicle_key]
        current = spec.initial_location_id
        remaining = spec.capacity
        profile = self.fleets[vehicle_key[0]].routing.profile_id
        for task_key in sequence:
            task = self.tasks[task_key]
            source = self.locations[current]
            target = self.locations[task.steps[0].location_id]
            assert source.node_id is not None and target.node_id is not None
            if not self.routing.reachable(profile, source.node_id, target.node_id):
                raise ScenarioDispatchError(
                    f"predefined task {task.task_id!r} is unreachable from its predecessor"
                )
            fleet = self.fleets[vehicle_key[0]]
            available_capacity = (
                spec.capacity
                if spec.capacity_mode == "consumable" and fleet.supply.depot_policy is not None
                else remaining
            )
            state = VehicleState(
                key=spec.key,
                status=VehicleStatus.IDLE,
                current_execution_id=None,
                committed_location_id=current,
                available_at_s=spec.availability_start_s,
                remaining_capacity=available_capacity,
                execution_generation=0,
            )
            if not _capacity_allowed(state, spec, task):
                raise ScenarioDispatchError(
                    f"predefined task {task.task_id!r} violates capacity after its predecessors"
                )
            if remaining is not None and not (
                spec.capacity_mode == "consumable" and fleet.supply.depot_policy is not None
            ):
                if task.kind == "depot_return" and task.capacity_reset_at_end is not False:
                    if (
                        spec.depot_location_id is None
                        or self.locations[task.steps[-1].location_id].node_id
                        != self.locations[spec.depot_location_id].node_id
                    ):
                        raise ScenarioDispatchError(
                            "A replenishment task must finish at its vehicle depot"
                        )
                    minimum = float(fleet.supply.capacity.replenishment_duration_s)
                    if sum(step.service_duration_s for step in task.steps) < minimum:
                        raise ScenarioDispatchError(
                            "A replenishment task must retain the minimum depot service time"
                        )
                    remaining = spec.capacity
                elif spec.capacity_mode == "consumable":
                    remaining -= float(task.required_capacity or 0.0)
                elif spec.capacity_mode == "occupancy":
                    remaining += sum(float(step.quantity_delta or 0.0) for step in task.steps)
            current = task.steps[-1].location_id

    def _planned_assignment(
        self, vehicle: VehicleState, task: Task, *, time_s: float
    ) -> PlannedAssignment:
        plan = self.executor.plan(
            vehicle,
            task,
            assigned_at_s=time_s,
            execution_token=vehicle.execution_generation + 1,
        )
        return PlannedAssignment(task=task, plan=plan)

    def service_assignments(self, snapshot: DecisionSnapshot) -> Sequence[PlannedAssignment]:
        waiting = {(task.fleet_id, task.task_id): task for task in snapshot.waiting_tasks}
        idle = {_vehicle_tuple(vehicle.key): vehicle for vehicle in snapshot.idle_vehicles}
        assignments: list[PlannedAssignment] = []
        matching_fleets = set()
        for fleet_id, fleet in self.fleets.items():
            policy = fleet.dispatch
            if policy.policy == "nearest_matching":
                matching_fleets.add(fleet_id)
            elif policy.policy == "batch_nearest_matching":
                rank = round((snapshot.time_s - policy.batch_origin_s) / policy.batch_interval_s)
                if (
                    rank >= 0
                    and snapshot.time_s == policy.batch_origin_s + rank * policy.batch_interval_s
                    and self._batch_consumed_time.get(fleet_id) != snapshot.time_s
                ):
                    matching_fleets.add(fleet_id)
                    self._batch_consumed_time[fleet_id] = snapshot.time_s

        for fleet_id in tuple(matching_fleets):
            signature = (
                tuple(
                    (
                        key,
                        vehicle.committed_location_id,
                        vehicle.remaining_capacity,
                        self.specs[key].availability_start_s
                        <= snapshot.time_s
                        < self.specs[key].availability_end_s,
                    )
                    for key, vehicle in sorted(idle.items())
                    if key[0] == fleet_id
                ),
                tuple(
                    (key, task.release_s <= snapshot.time_s)
                    for key, task in sorted(waiting.items())
                    if key[0] == fleet_id
                ),
            )
            if self._matching_snapshots.get(fleet_id) == signature:
                matching_fleets.remove(fleet_id)
            else:
                self._matching_snapshots[fleet_id] = signature

        for vehicle_key in sorted(self._predefined):
            vehicle = idle.get(vehicle_key)
            cursor = self._predefined_cursor[vehicle_key]
            sequence = self._predefined[vehicle_key]
            if vehicle is None or cursor >= len(sequence):
                continue
            task_key = sequence[cursor]
            task = waiting.get(task_key)
            if task is None:
                continue
            pair = self.eligibility.pair(vehicle, task, time_s=snapshot.time_s)
            if pair is None:
                continue
            assignments.append(self._planned_assignment(vehicle, task, time_s=snapshot.time_s))
            self._predefined_cursor[vehicle_key] += 1

        pairs: list[FeasiblePair] = []
        nearest_vehicles = [
            vehicle for key, vehicle in sorted(idle.items()) if key[0] in matching_fleets
        ]
        nearest_tasks = [task for key, task in sorted(waiting.items()) if key[0] in matching_fleets]
        if self.routing.supports_lower_bound_queries:
            assignments.extend(
                self._nearest_with_bounds(nearest_vehicles, nearest_tasks, snapshot.time_s)
            )
            return tuple(assignments)
        source_targets: dict[tuple[str, str], set[str]] = {}
        for vehicle in nearest_vehicles:
            source = self.locations[vehicle.committed_location_id]
            assert source.node_id is not None
            profile = self.fleets[vehicle.key.fleet_id].routing.profile_id
            targets = source_targets.setdefault((profile, source.node_id), set())
            targets.update(
                self.locations[task.steps[0].location_id].node_id
                for task in nearest_tasks
                if task.fleet_id == vehicle.key.fleet_id
                and self.eligibility.eligible_without_route(vehicle, task, time_s=snapshot.time_s)
            )
        for (profile, source), targets in sorted(source_targets.items()):
            self.routing.routes_from(profile, source, tuple(targets))  # type: ignore[arg-type]
        for vehicle in nearest_vehicles:
            for task in nearest_tasks:
                if task.fleet_id != vehicle.key.fleet_id:
                    continue
                pair = self.eligibility.pair(vehicle, task, time_s=snapshot.time_s)
                if pair is None:
                    continue
                if len(pairs) >= self.max_feasible_pairs:
                    raise MatchingPairLimitExceeded(
                        "feasible pair limit exceeded; increase max_feasible_pairs or reduce "
                        "the dispatch pool using area/capacity/pickup-time constraints"
                    )
                pairs.append(pair)
        used_vehicles = {item.plan.vehicle for item in assignments}
        used_tasks = {(item.task.fleet_id, item.task.task_id) for item in assignments}
        for pair in sorted(pairs, key=lambda item: item.order_key):
            task_key = (pair.task.fleet_id, pair.task.task_id)
            if pair.vehicle in used_vehicles or task_key in used_tasks:
                continue
            vehicle = idle[_vehicle_tuple(pair.vehicle)]
            assignments.append(self._planned_assignment(vehicle, pair.task, time_s=snapshot.time_s))
            used_vehicles.add(pair.vehicle)
            used_tasks.add(task_key)
        return tuple(assignments)

    def _nearest_with_bounds(self, vehicles, tasks, time_s):
        """Refine conservative road-cost bounds until the exact next pair is known."""
        import heapq
        import math

        queue, by_vehicle, by_task = [], {}, {}
        candidates = 0
        for vehicle in vehicles:
            by_vehicle[_vehicle_tuple(vehicle.key)] = vehicle
            source = self.locations[vehicle.committed_location_id].node_id
            fleet = self.fleets[vehicle.key.fleet_id]
            eligible = [
                task
                for task in tasks
                if self.eligibility.eligible_without_route(vehicle, task, time_s=time_s)
            ]
            targets = {self.locations[task.steps[0].location_id].node_id for task in eligible}
            if not targets:
                continue
            bounds = self.routing.travel_time_lower_bounds_from(
                fleet.routing.profile_id, source, targets
            )
            limit = getattr(fleet.dispatch, "max_pickup_time_s", None)
            for task in eligible:
                bound = bounds[self.locations[task.steps[0].location_id].node_id]
                if not math.isfinite(bound) or (limit is not None and bound > limit):
                    continue
                candidates += 1
                if candidates > self.max_feasible_pairs:
                    raise MatchingPairLimitExceeded(
                        "feasible pair candidate bound exceeds the configured query limit"
                    )
                by_task[(task.fleet_id, task.task_id)] = task
                heapq.heappush(
                    queue,
                    (
                        bound,
                        task.release_s,
                        task.task_id,
                        task.fleet_id,
                        vehicle.key.vehicle_id,
                        False,
                    ),
                )
        used_vehicles, used_tasks, assignments = set(), set(), []
        while queue:
            bound, release, task_id, fleet_id, vehicle_id, resolved = heapq.heappop(queue)
            key, task_key = (fleet_id, vehicle_id), (fleet_id, task_id)
            if key in used_vehicles or task_key in used_tasks:
                continue
            vehicle, task = by_vehicle[key], by_task[task_key]
            if not resolved:
                pair = self.eligibility.pair(vehicle, task, time_s=time_s)
                if pair is not None:
                    if pair.pickup_time_s < bound:
                        raise ScenarioDispatchError(
                            "Routing lower bound exceeded the exact road time"
                        )
                    heapq.heappush(queue, (*pair.order_key, True))
            else:
                assignments.append(self._planned_assignment(vehicle, task, time_s=time_s))
                used_vehicles.add(key)
                used_tasks.add(task_key)
        return assignments

    def batch_wakeup_specs(self):
        return tuple(
            (fleet_id, fleet.dispatch.batch_origin_s, fleet.dispatch.batch_interval_s)
            for fleet_id, fleet in sorted(self.fleets.items())
            if fleet.dispatch.policy == "batch_nearest_matching"
        )

    def _operational_candidate_tasks(
        self, vehicle: VehicleState, waiting_tasks: Sequence[Task]
    ) -> tuple[Task, ...]:
        key = _vehicle_tuple(vehicle.key)
        if key in self._predefined:
            cursor = self._predefined_cursor[key]
            sequence = self._predefined[key]
            if cursor >= len(sequence):
                return ()
            task_key = sequence[cursor]
            return tuple(
                task for task in waiting_tasks if (task.fleet_id, task.task_id) == task_key
            )
        return tuple(task for task in waiting_tasks if task.fleet_id == vehicle.key.fleet_id)

    def operational_assignments(self, snapshot: DecisionSnapshot) -> Sequence[PlannedAssignment]:
        if self.operational is None:
            return ()
        return self.operational.assignments(snapshot)

    @property
    def operational_diagnostics(self) -> tuple[OperationalDiagnostic, ...]:
        runtime = () if self.operational is None else tuple(self.operational.diagnostics)
        return tuple(self._configuration_diagnostics) + runtime
