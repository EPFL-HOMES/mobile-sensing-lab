"""Generic routed execution planning and cutoff materialization.

The executor is task-shape agnostic: a location task, an OD task, and an ordered
stop sequence are all the same ordered ``Task.steps`` contract.  It consumes the
same timing primitive as GTFS reconstruction and only expands its routes into
edge, wait, service, and capacity intervals.
"""

from __future__ import annotations

import math
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass

from mobile_sensing.contracts import (
    CapacityMilestone,
    ExecutionPlan,
    LocationRef,
    MovementInterval,
    RouteResult,
    RoutingService,
    StationaryInterval,
    Task,
    TaskMilestone,
    VehicleState,
    stable_id,
)
from mobile_sensing.simulation.timing import TaskTiming, estimate_task_timing


EXECUTOR_VERSION = "generic-task-executor@1"


class SharedRoutingCache:
    """Small deterministic LRU shared by eligibility and execution planning."""

    def __init__(self, routing: RoutingService, *, max_entries: int = 4096) -> None:
        if isinstance(max_entries, bool) or max_entries <= 0:
            raise ValueError("route-cache max_entries must be a positive integer")
        self._routing = routing
        self._max_entries = max_entries
        self._cache: OrderedDict[tuple[str, str, str], RouteResult] = OrderedDict()

    def route(self, profile_id: str, source_node_id: str, target_node_id: str) -> RouteResult:
        key = (profile_id, source_node_id, target_node_id)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached
        result = self._routing.route(profile_id, source_node_id, target_node_id)
        self._cache[key] = result
        self._cache.move_to_end(key)
        if len(self._cache) > self._max_entries:
            self._cache.popitem(last=False)
        return result

    def routes_from(
        self, profile_id: str, source_node_id: str, target_node_ids: tuple[str, ...]
    ) -> Mapping[str, RouteResult]:
        """Populate pair entries through an optional bulk source-tree operation."""

        targets = tuple(sorted(set(target_node_ids)))
        results: dict[str, RouteResult] = {}
        missing = []
        for target in targets:
            key = (profile_id, source_node_id, target)
            cached = self._cache.get(key)
            if cached is None:
                missing.append(target)
            else:
                self._cache.move_to_end(key)
                results[target] = cached
        if missing:
            bulk = getattr(self._routing, "routes_from", None)
            resolved = (
                bulk(profile_id, source_node_id, tuple(missing))
                if bulk is not None
                else {
                    target: self._routing.route(profile_id, source_node_id, target)
                    for target in missing
                }
            )
            for target in missing:
                result = resolved[target]
                results[target] = result
                key = (profile_id, source_node_id, target)
                self._cache[key] = result
                self._cache.move_to_end(key)
                if len(self._cache) > self._max_entries:
                    self._cache.popitem(last=False)
        return {target: results[target] for target in targets}

    def outgoing_neighbor_node_ids(self, profile_id: str, source_node_id: str) -> tuple[str, ...]:
        operation = getattr(self._routing, "outgoing_neighbor_node_ids", None)
        if operation is None:
            raise TypeError("routing service does not support operational neighbor queries")
        return tuple(operation(profile_id, source_node_id))

    def location_for_node(self, node_id: str) -> LocationRef:
        operation = getattr(self._routing, "location_for_node", None)
        if operation is None:
            raise TypeError("routing service does not expose prepared node locations")
        return operation(node_id)

    @property
    def supports_lower_bound_queries(self):
        return callable(getattr(self._routing, "travel_time_lower_bounds_from", None))

    def travel_time_lower_bounds_from(self, profile_id, source, targets):
        return self._routing.travel_time_lower_bounds_from(profile_id, source, targets)

    @property
    def supports_reachability_queries(self):
        return callable(getattr(self._routing, "reachable", None))

    def reachable(self, profile_id, source, target):
        if self.supports_reachability_queries:
            return self._routing.reachable(source, target)
        return self.route(profile_id, source, target).reachable

    @property
    def supports_operational_cruise(self) -> bool:
        """Whether the wrapped router exposes the M05C node-neighbor seam."""

        return callable(getattr(self._routing, "outgoing_neighbor_node_ids", None)) and callable(
            getattr(self._routing, "location_for_node", None)
        )

    @property
    def size(self) -> int:
        return len(self._cache)


def _movement_kind(task: Task, step_index: int) -> str:
    if task.kind == "service":
        return "service_pickup" if step_index == 1 else "service_inter_step"
    return task.kind


def _capacity_milestones(
    task: Task, timing: TaskTiming, mode: str
) -> tuple[CapacityMilestone, ...]:
    if task.kind == "depot_return":
        if task.capacity_reset_at_end is False:
            return ()
        if mode != "consumable":
            raise ValueError("depot return requires consumable capacity")
        if timing.end_s <= timing.start_s:
            raise ValueError("depot replenishment must have positive duration")
        return (
            CapacityMilestone(
                time_s=timing.end_s,
                step_index=timing.steps[-1].step_index,
                reset_to_capacity=True,
            ),
        )
    if mode == "none":
        return ()
    if mode == "consumable":
        amount = float(task.required_capacity or 0.0)
        if amount == 0.0:
            return ()
        declared = [
            (step, step_timing)
            for step, step_timing in zip(task.steps, timing.steps, strict=True)
            if step.quantity_delta not in (None, 0.0)
        ]
        if not declared:
            declared = [(task.steps[0], timing.steps[0])]
            deltas = (-amount,)
        else:
            deltas = tuple(float(step.quantity_delta) for step, _ in declared)
            if any(delta > 0.0 for delta in deltas) or not math.isclose(
                -math.fsum(deltas), amount, rel_tol=1e-12, abs_tol=1e-9
            ):
                raise ValueError(
                    "consumable step deltas must be nonpositive and sum to required_capacity"
                )
        return tuple(
            CapacityMilestone(
                time_s=step_timing.departure_s,
                step_index=step.step_index,
                quantity_delta=delta,
            )
            for (step, step_timing), delta in zip(declared, deltas, strict=True)
        )
    if mode == "occupancy":
        rows = []
        for step, step_timing in zip(task.steps, timing.steps, strict=True):
            delta = float(step.quantity_delta or 0.0)
            if delta != 0.0:
                rows.append(
                    CapacityMilestone(
                        time_s=step_timing.departure_s,
                        step_index=step.step_index,
                        quantity_delta=delta,
                    )
                )
        return tuple(rows)
    raise ValueError(f"unsupported capacity mode: {mode}")


class GenericTaskExecutor:
    """Construct one immutable, fully routed plan from current committed state."""

    def __init__(
        self,
        *,
        locations: Mapping[str, LocationRef],
        routing: RoutingService,
        profile_ids: Mapping[str, str],
        capacity_modes: Mapping[tuple[str, str], str],
    ) -> None:
        self.locations = dict(locations)
        self.routing = routing
        self.profile_ids = dict(profile_ids)
        self.capacity_modes = dict(capacity_modes)

    def timing(self, vehicle: VehicleState, task: Task, *, assigned_at_s: float) -> TaskTiming:
        try:
            profile_id = self.profile_ids[task.fleet_id]
        except KeyError as exc:
            raise ValueError(f"missing routing profile for fleet {task.fleet_id!r}") from exc
        return estimate_task_timing(
            task,
            start_location_id=vehicle.committed_location_id,
            start_s=assigned_at_s,
            locations=self.locations,
            routing=self.routing,
            profile_id=profile_id,
        )

    def plan(
        self,
        vehicle: VehicleState,
        task: Task,
        routing: RoutingService | None = None,
        *,
        assigned_at_s: float,
        execution_token: int,
    ) -> ExecutionPlan:
        if routing is not None and routing is not self.routing:
            raise ValueError("executor must use its shared routing service")
        if vehicle.key.fleet_id != task.fleet_id:
            raise ValueError("task and vehicle fleet IDs must match")
        timing = self.timing(vehicle, task, assigned_at_s=assigned_at_s)
        intervals = []
        clock = float(assigned_at_s)
        for step_timing, step in zip(timing.steps, task.steps, strict=True):
            movement_kind = _movement_kind(task, step.step_index)
            for edge in step_timing.incoming_route.edges:
                edge_end = clock + edge.duration_s
                if edge_end == clock:
                    raise ValueError("positive edge duration must advance representable time")
                intervals.append(
                    MovementInterval(
                        kind="movement",
                        start_s=clock,
                        end_s=edge_end,
                        movement_kind=movement_kind,
                        edge_id=edge.edge_id,
                        edge_start_fraction=0.0,
                        edge_end_fraction=1.0,
                    )
                )
                clock = edge_end
            if not math.isclose(clock, step_timing.arrival_s, rel_tol=1e-12, abs_tol=1e-9):
                raise RuntimeError("route expansion disagrees with shared task timing")
            if step_timing.service_start_s > clock:
                intervals.append(
                    StationaryInterval(
                        kind="wait",
                        start_s=clock,
                        end_s=step_timing.service_start_s,
                        location_id=step.location_id,
                        step_index=step.step_index,
                    )
                )
                clock = step_timing.service_start_s
            if step_timing.departure_s > clock:
                intervals.append(
                    StationaryInterval(
                        kind="service",
                        start_s=clock,
                        end_s=step_timing.departure_s,
                        location_id=step.location_id,
                        step_index=step.step_index,
                    )
                )
                clock = step_timing.departure_s
        milestones = tuple(
            TaskMilestone(
                step_index=row.step_index,
                arrival_s=row.arrival_s,
                service_start_s=row.service_start_s,
                departure_s=row.departure_s,
            )
            for row in timing.steps
        )
        return ExecutionPlan(
            execution_id=stable_id(
                "execution",
                {
                    "version": EXECUTOR_VERSION,
                    "vehicle": vehicle.key,
                    "task_id": task.task_id,
                    "assigned_at_s": float(assigned_at_s),
                    "execution_token": execution_token,
                    "profile_id": self.profile_ids[task.fleet_id],
                },
            ),
            execution_token=execution_token,
            vehicle=vehicle.key,
            task_id=task.task_id,
            start_s=float(assigned_at_s),
            end_s=timing.end_s,
            end_location_id=timing.end_location_id,
            intervals=tuple(intervals),
            task_milestones=milestones,
            capacity_milestones=_capacity_milestones(
                task,
                timing,
                self.capacity_modes[(vehicle.key.fleet_id, vehicle.key.vehicle_id)],
            ),
        )


@dataclass(frozen=True, slots=True)
class MaterializedPosition:
    """Physical position at a realized cutoff, separate from the planned endpoint."""

    location_id: str | None = None
    edge_id: str | None = None
    edge_fraction: float | None = None

    def __post_init__(self) -> None:
        at_location = self.location_id is not None
        on_edge = self.edge_id is not None or self.edge_fraction is not None
        if at_location == on_edge:
            raise ValueError("position requires exactly one location or edge coordinate")
        if on_edge and (
            self.edge_id is None
            or self.edge_fraction is None
            or not 0.0 <= self.edge_fraction <= 1.0
        ):
            raise ValueError("edge position requires an edge ID and fraction in [0, 1]")


@dataclass(frozen=True, slots=True)
class MaterializedExecution:
    intervals: tuple[MovementInterval | StationaryInterval, ...]
    position: MaterializedPosition


def materialize_execution(
    plan: ExecutionPlan,
    task: Task,
    *,
    start_location_id: str,
    cutoff_s: float,
) -> MaterializedExecution:
    """Clip a plan at ``cutoff_s`` and locate the vehicle without teleportation."""

    if cutoff_s < plan.start_s or cutoff_s > plan.end_s:
        raise ValueError("execution cutoff must lie within the plan")
    realized = []
    position = MaterializedPosition(location_id=start_location_id)
    for interval in plan.intervals:
        if interval.start_s >= cutoff_s:
            break
        end_s = min(interval.end_s, cutoff_s)
        if isinstance(interval, MovementInterval):
            ratio = (end_s - interval.start_s) / (interval.end_s - interval.start_s)
            end_fraction = interval.edge_start_fraction + ratio * (
                interval.edge_end_fraction - interval.edge_start_fraction
            )
            realized.append(
                interval.model_copy(update={"end_s": end_s, "edge_end_fraction": end_fraction})
            )
            position = MaterializedPosition(
                edge_id=interval.edge_id,
                edge_fraction=end_fraction,
            )
        else:
            realized.append(interval.model_copy(update={"end_s": end_s}))
            position = MaterializedPosition(location_id=interval.location_id)
        if end_s < interval.end_s:
            break
        for milestone, step in zip(plan.task_milestones, task.steps, strict=True):
            if milestone.arrival_s <= end_s:
                position = MaterializedPosition(location_id=step.location_id)
    if cutoff_s == plan.end_s:
        position = MaterializedPosition(location_id=plan.end_location_id)
    return MaterializedExecution(intervals=tuple(realized), position=position)
