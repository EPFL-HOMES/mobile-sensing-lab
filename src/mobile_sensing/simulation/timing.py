"""Shared deterministic task-timing primitive for GTFS estimation and M05B execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol

from mobile_sensing.contracts import LocationRef, RouteResult, Task


TIMING_ALGORITHM_VERSION = "scheduled-task-timing@1"


class RoutingService(Protocol):
    def route(self, profile_id: str, source_node_id: str, target_node_id: str) -> RouteResult: ...


class UnreachableTaskLeg(ValueError):
    """Raised when an ordered task contains an unroutable physical leg."""


@dataclass(frozen=True, slots=True)
class StepTiming:
    step_index: int
    location_id: str
    arrival_s: float
    service_start_s: float
    departure_s: float
    lateness_s: float
    incoming_route: RouteResult


@dataclass(frozen=True, slots=True)
class TaskTiming:
    start_s: float
    end_s: float
    start_location_id: str
    end_location_id: str
    steps: tuple[StepTiming, ...]


def _route(
    source: LocationRef,
    target: LocationRef,
    *,
    routing: RoutingService,
    profile_id: str,
) -> RouteResult:
    if source.node_id is None or target.node_id is None:
        raise ValueError("task timing requires resolved locations")
    result = routing.route(profile_id, source.node_id, target.node_id)
    if not result.reachable:
        raise UnreachableTaskLeg(
            f"unreachable task leg {source.location_id!r}->{target.location_id!r}: {result.reason}"
        )
    return result


def estimate_task_timing(
    task: Task,
    *,
    start_location_id: str,
    start_s: float,
    locations: Mapping[str, LocationRef],
    routing: RoutingService,
    profile_id: str,
) -> TaskTiming:
    """Apply route, target-time waiting, and dwell semantics to one ordered task.

    This intentionally contains no event scheduling or vehicle-state mutation. M05B
    consumes the same primitive when materializing an execution plan.
    """

    try:
        current = locations[start_location_id]
    except KeyError as exc:
        raise ValueError(f"unknown start location {start_location_id!r}") from exc
    clock = float(start_s)
    rows: list[StepTiming] = []
    for step in task.steps:
        try:
            target = locations[step.location_id]
        except KeyError as exc:
            raise ValueError(f"unknown task location {step.location_id!r}") from exc
        incoming = _route(current, target, routing=routing, profile_id=profile_id)
        arrival = clock + incoming.total_duration_s
        target_s = step.scheduled_time_s
        service_start = max(arrival, target_s if target_s is not None else arrival)
        departure = service_start + step.service_duration_s
        rows.append(
            StepTiming(
                step_index=step.step_index,
                location_id=step.location_id,
                arrival_s=arrival,
                service_start_s=service_start,
                departure_s=departure,
                lateness_s=max(0.0, arrival - target_s) if target_s is not None else 0.0,
                incoming_route=incoming,
            )
        )
        current = target
        clock = departure
    return TaskTiming(
        start_s=float(start_s),
        end_s=clock,
        start_location_id=start_location_id,
        end_location_id=current.location_id,
        steps=tuple(rows),
    )


def task_with_release(task: Task, release_s: float) -> Task:
    """Return the same scientific task with its known-duty release time replaced."""

    payload = task.model_dump(mode="python")
    payload["release_s"] = float(release_s)
    return Task.model_validate(payload)
