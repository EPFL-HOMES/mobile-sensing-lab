"""Baseline stationary, random-cruise, and consumable depot policies."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from mobile_sensing.contracts import (
    ExecutionPlan,
    FleetConfig,
    LocationRef,
    MovementInterval,
    NamedRngStreamProvider,
    StationaryInterval,
    Task,
    TaskMilestone,
    TaskStep,
    VehicleKey,
    VehicleSpec,
    VehicleState,
    stable_id,
)
from mobile_sensing.simulation.executor import GenericTaskExecutor, SharedRoutingCache
from mobile_sensing.simulation.kernel import DecisionSnapshot, PlannedAssignment


RANDOM_CRUISE_VERSION = "operational.random_cruise@2"
DEPOT_RETURN_VERSION = "operational.depot_return@1"
STATIONARY_IDLE_VERSION = "operational.stationary_idle@1"

VehicleTuple = tuple[str, str]
PairEvaluator = Callable[[VehicleState, Task, float], object | None]
CandidateTasks = Callable[[VehicleState, Sequence[Task]], tuple[Task, ...]]
LocationRegistrar = Callable[[LocationRef], None]


@dataclass(frozen=True, slots=True)
class OperationalDiagnostic:
    time_s: float | None
    vehicle: VehicleKey
    code: str
    message: str


class OperationalPolicyEngine:
    """Create bounded operational tasks after the service-dispatch phase."""

    def __init__(
        self,
        *,
        replication_id: str,
        simulation_end_s: float,
        fleets: Mapping[str, FleetConfig],
        vehicle_specs: Mapping[VehicleTuple, VehicleSpec],
        availability_end_s: Mapping[VehicleTuple, float],
        locations: dict[str, LocationRef],
        routing: SharedRoutingCache,
        executor: GenericTaskExecutor,
        rng: NamedRngStreamProvider,
        pair_evaluator: PairEvaluator,
        candidate_tasks: CandidateTasks,
        register_location: LocationRegistrar,
    ) -> None:
        if replication_id == "":
            raise ValueError("replication_id must be nonempty")
        self.replication_id = replication_id
        self.simulation_end_s = float(simulation_end_s)
        self.fleets = dict(fleets)
        self.specs = dict(vehicle_specs)
        self.availability_end_s = dict(availability_end_s)
        self.locations = locations
        self.routing = routing
        self.executor = executor
        self.rng = rng
        self.pair_evaluator = pair_evaluator
        self.candidate_tasks = candidate_tasks
        self.register_location = register_location
        self.selection_counters: dict[VehicleTuple, int] = {key: 0 for key in self.specs}
        self.previous_cruise_legs: dict[VehicleTuple, tuple[str, str]] = {}
        self.node_locations = {
            location.node_id: location
            for location in self.locations.values()
            if location.node_id is not None
        }
        self.diagnostics: list[OperationalDiagnostic] = []
        self._diagnostic_keys: set[tuple[float | None, VehicleTuple, str]] = set()

    def _diagnose(
        self, vehicle: VehicleState, *, time_s: float | None, code: str, message: str
    ) -> None:
        key = (time_s, (vehicle.key.fleet_id, vehicle.key.vehicle_id), code)
        if key in self._diagnostic_keys:
            return
        self._diagnostic_keys.add(key)
        self.diagnostics.append(
            OperationalDiagnostic(
                time_s=time_s,
                vehicle=vehicle.key,
                code=code,
                message=message,
            )
        )

    def _remaining_time(self, vehicle: VehicleState, time_s: float) -> float:
        key = (vehicle.key.fleet_id, vehicle.key.vehicle_id)
        return min(self.availability_end_s[key], self.simulation_end_s) - time_s

    def _register_node_location(self, node_id: str) -> LocationRef:
        existing = self.node_locations.get(node_id)
        if existing is not None:
            return existing
        location = self.routing.location_for_node(node_id)
        self.register_location(location)
        self.node_locations[node_id] = location
        return location

    def _depot_return(
        self, vehicle: VehicleState, waiting_tasks: Sequence[Task], *, time_s: float
    ) -> PlannedAssignment | None:
        key = (vehicle.key.fleet_id, vehicle.key.vehicle_id)
        spec = self.specs[key]
        fleet = self.fleets[key[0]]
        if spec.capacity_mode != "consumable" or fleet.supply.depot_policy is None:
            return None
        assert spec.capacity is not None and vehicle.remaining_capacity is not None
        if vehicle.remaining_capacity >= spec.capacity:
            return None
        relevant = self.candidate_tasks(vehicle, waiting_tasks)
        blocked = []
        full_state = vehicle.model_copy(update={"remaining_capacity": spec.capacity})
        for task in relevant:
            full_pair = self.pair_evaluator(full_state, task, time_s)
            current_pair = self.pair_evaluator(vehicle, task, time_s)
            if full_pair is not None and current_pair is None:
                blocked.append(task)
        if not blocked:
            return None
        assert spec.depot_location_id is not None
        depot = self.locations[spec.depot_location_id]
        current = self.locations[vehicle.committed_location_id]
        assert depot.node_id is not None and current.node_id is not None
        profile = fleet.routing.profile_id
        depot_route = self.routing.route(profile, current.node_id, depot.node_id)
        if not depot_route.reachable:
            self._diagnose(
                vehicle,
                time_s=time_s,
                code="DEPOT_UNREACHABLE",
                message="Configured depot is unreachable from the current directed node.",
            )
            return None
        if not any(
            self.routing.route(
                profile,
                depot.node_id,
                self.locations[task.steps[0].location_id].node_id,  # type: ignore[arg-type]
            ).reachable
            for task in blocked
        ):
            self._diagnose(
                vehicle,
                time_s=time_s,
                code="DEPOT_CANNOT_REACH_BLOCKED_TASK",
                message="Replenishment would not make a blocked task reachable from the depot.",
            )
            return None
        replenishment_s = float(fleet.supply.capacity.replenishment_duration_s)
        task = Task(
            task_id=stable_id(
                "operational_task",
                {
                    "policy": DEPOT_RETURN_VERSION,
                    "replication_id": self.replication_id,
                    "vehicle": vehicle.key,
                    "time_s": time_s,
                    "execution_generation": vehicle.execution_generation + 1,
                },
            ),
            fleet_id=vehicle.key.fleet_id,
            release_s=time_s,
            steps=(
                TaskStep(
                    step_index=1,
                    location_id=depot.location_id,
                    service_duration_s=replenishment_s,
                ),
            ),
            kind="depot_return",
            source_policy=DEPOT_RETURN_VERSION,
        )
        plan = self.executor.plan(
            vehicle,
            task,
            assigned_at_s=time_s,
            execution_token=vehicle.execution_generation + 1,
        )
        return PlannedAssignment(task=task, plan=plan)

    def _random_cruise(
        self,
        vehicle: VehicleState,
        *,
        time_s: float,
        next_decision_s: float | None,
    ) -> PlannedAssignment | None:
        key = (vehicle.key.fleet_id, vehicle.key.vehicle_id)
        fleet = self.fleets[key[0]]
        if fleet.supply.idle_policy.policy != "random_cruise":
            return None
        current_location = self.locations[vehicle.committed_location_id]
        assert current_location.node_id is not None
        current_node = current_location.node_id
        profile = fleet.routing.profile_id
        window_end_s = min(self.availability_end_s[key], self.simulation_end_s)
        decision_s = min(next_decision_s or window_end_s, window_end_s)
        clock = float(time_s)
        intervals = []
        first_counter = self.selection_counters[key]
        leg_count = 0
        # Continue the random walk until it reaches or crosses the next
        # decision epoch. A long idle period therefore creates one execution,
        # while an edge already in progress remains non-preemptive.
        while clock < decision_s:
            candidates = []
            routes = {}
            for node_id in self.routing.outgoing_neighbor_node_ids(profile, current_node):
                route = self.routing.route(profile, current_node, node_id)
                if (
                    route.reachable
                    and route.total_duration_s > 0.0
                    and clock + route.total_duration_s > clock
                    and route.total_duration_s <= window_end_s - clock
                ):
                    candidates.append(node_id)
                    routes[node_id] = route
            candidates = sorted(set(candidates))
            previous_leg = self.previous_cruise_legs.get(key)
            previous = (
                previous_leg[0]
                if previous_leg is not None and previous_leg[1] == current_node
                else None
            )
            if previous in candidates and len(candidates) > 1:
                candidates.remove(previous)
            if not candidates:
                break
            counter = self.selection_counters[key]
            generator = self.rng.stream(
                "operational.cruise",
                self.replication_id,
                vehicle.key.fleet_id,
                vehicle.key.vehicle_id,
                RANDOM_CRUISE_VERSION,
                str(counter),
            )
            target_node = candidates[int(generator.integers(0, len(candidates)))]  # type: ignore[attr-defined]
            route = routes[target_node]
            for edge in route.edges:
                edge_end = clock + edge.duration_s
                intervals.append(
                    MovementInterval(
                        kind="movement",
                        start_s=clock,
                        end_s=edge_end,
                        movement_kind="cruise",
                        edge_id=edge.edge_id,
                        edge_start_fraction=0.0,
                        edge_end_fraction=1.0,
                    )
                )
                clock = edge_end
            self.selection_counters[key] = counter + 1
            self.previous_cruise_legs[key] = (current_node, target_node)
            current_node = target_node
            leg_count += 1
            if leg_count > 100_000:
                raise RuntimeError("cruise walk exceeds the bounded leg count")
        if not intervals:
            self._diagnose(
                vehicle,
                time_s=time_s,
                code="CRUISE_NO_FEASIBLE_NEIGHBOR",
                message="No positive-duration outgoing cruise leg fits the remaining window.",
            )
            return None
        target = self._register_node_location(current_node)
        # If the walk reaches a dead end before the next decision, retain the
        # powered-on stationary remainder in the same operating execution.
        if clock < decision_s:
            intervals.append(
                StationaryInterval(
                    kind="wait",
                    start_s=clock,
                    end_s=decision_s,
                    location_id=target.location_id,
                    step_index=1,
                )
            )
            clock = decision_s
        task = Task(
            task_id=stable_id(
                "operational_task",
                {
                    "policy": RANDOM_CRUISE_VERSION,
                    "replication_id": self.replication_id,
                    "vehicle": vehicle.key,
                    "first_transition_counter": first_counter,
                    "transition_count": leg_count,
                    "source_node_id": current_location.node_id,
                    "target_node_id": current_node,
                    "end_s": clock,
                },
            ),
            fleet_id=vehicle.key.fleet_id,
            release_s=time_s,
            steps=(TaskStep(step_index=1, location_id=target.location_id),),
            kind="cruise",
            source_policy=RANDOM_CRUISE_VERSION,
        )
        arrival_s = next(
            (
                interval.end_s
                for interval in reversed(intervals)
                if isinstance(interval, MovementInterval)
            ),
            time_s,
        )
        plan = ExecutionPlan(
            execution_id=stable_id(
                "execution",
                {
                    "policy": RANDOM_CRUISE_VERSION,
                    "task_id": task.task_id,
                    "vehicle": vehicle.key,
                    "start_s": time_s,
                    "end_s": clock,
                    "execution_token": vehicle.execution_generation + 1,
                },
            ),
            execution_token=vehicle.execution_generation + 1,
            vehicle=vehicle.key,
            task_id=task.task_id,
            start_s=time_s,
            end_s=clock,
            end_location_id=target.location_id,
            intervals=tuple(intervals),
            task_milestones=(
                TaskMilestone(
                    step_index=1,
                    arrival_s=arrival_s,
                    service_start_s=arrival_s,
                    departure_s=clock,
                ),
            ),
        )
        if plan.end_s > min(self.availability_end_s[key], self.simulation_end_s):
            raise RuntimeError("cruise plan exceeds its prevalidated operational window")
        return PlannedAssignment(task=task, plan=plan)

    def assignments(self, snapshot: DecisionSnapshot) -> tuple[PlannedAssignment, ...]:
        assignments = []
        for vehicle in snapshot.idle_vehicles:
            depot = self._depot_return(
                vehicle,
                snapshot.waiting_tasks,
                time_s=snapshot.time_s,
            )
            if depot is not None:
                assignments.append(depot)
                continue
            cruise = self._random_cruise(
                vehicle,
                time_s=snapshot.time_s,
                next_decision_s=snapshot.next_decision_s,
            )
            if cruise is not None:
                assignments.append(cruise)
                continue
            self._diagnose(
                vehicle,
                time_s=snapshot.time_s,
                code="STATIONARY_IDLE",
                message="Vehicle remains at its current node until the next meaningful event.",
            )
        return tuple(assignments)
