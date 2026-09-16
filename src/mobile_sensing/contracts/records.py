"""Version-2 runtime record, event, route, and execution-plan contracts."""

from __future__ import annotations

import math
from enum import StrEnum
from typing import Annotated, Literal, Self, TypeAlias

from pydantic import Field, field_validator, model_validator

from mobile_sensing.contracts.base import (
    ContractModel,
    FiniteFloat,
    NonNegativeFloat,
    NonNegativeInt,
    OpaqueId,
    PositiveFloat,
    PositiveInt,
    Sha256,
)


class VehicleKey(ContractModel):
    fleet_id: OpaqueId
    vehicle_id: OpaqueId


class ResolutionStatus(StrEnum):
    RESOLVED = "resolved"
    REJECTED_DISTANCE = "rejected_distance"
    REJECTED_INVALID = "rejected_invalid"


class LocationRef(ContractModel):
    location_id: OpaqueId
    original_x: FiniteFloat
    original_y: FiniteFloat
    original_crs: str
    node_id: OpaqueId | None
    snapped_x: FiniteFloat | None
    snapped_y: FiniteFloat | None
    snap_distance_m: NonNegativeFloat | None
    resolution_status: ResolutionStatus

    @model_validator(mode="after")
    def validate_resolution(self) -> Self:
        resolved_fields = (
            self.node_id,
            self.snapped_x,
            self.snapped_y,
            self.snap_distance_m,
        )
        if self.resolution_status == ResolutionStatus.RESOLVED and any(
            item is None for item in resolved_fields
        ):
            raise ValueError("resolved location requires node, snapped coordinates, and distance")
        if self.resolution_status != ResolutionStatus.RESOLVED and any(
            item is not None for item in resolved_fields
        ):
            raise ValueError("rejected location must not contain a partial snapped result")
        return self


class TaskStep(ContractModel):
    step_index: PositiveInt
    location_id: OpaqueId
    scheduled_time_s: FiniteFloat | None = None
    service_duration_s: NonNegativeFloat = 0.0
    quantity_delta: FiniteFloat | None = None
    source_record_refs: tuple[OpaqueId, ...] = ()


class Task(ContractModel):
    task_id: OpaqueId
    fleet_id: OpaqueId
    release_s: FiniteFloat
    steps: tuple[TaskStep, ...]
    kind: Literal["service", "depot_return", "cruise", "reposition"] = "service"
    required_capacity: NonNegativeFloat | None = None
    source_record_refs: tuple[OpaqueId, ...] = ()
    source_policy: str | None = None
    capacity_reset_at_end: bool | None = None

    @field_validator("steps")
    @classmethod
    def validate_steps(cls, value: tuple[TaskStep, ...]) -> tuple[TaskStep, ...]:
        if not value:
            raise ValueError("task requires at least one step")
        indices = tuple(step.step_index for step in value)
        if indices != tuple(range(1, len(value) + 1)):
            raise ValueError("task steps must be ordered with contiguous positive indices")
        return value

    @model_validator(mode="after")
    def validate_task_kind(self) -> Self:
        if self.kind != "service" and self.source_policy is None:
            raise ValueError("operational tasks require source_policy provenance")
        if self.capacity_reset_at_end is not None and self.kind != "depot_return":
            raise ValueError("Explicit depot capacity reset applies only to depot-return tasks")
        return self


class VehicleAvailability(ContractModel):
    replication_id: OpaqueId
    vehicle: VehicleKey
    active: bool
    availability_start_s: FiniteFloat | None = None
    availability_end_s: FiniteFloat | None = None
    initial_location_id: OpaqueId | None = None

    @model_validator(mode="after")
    def validate_realization(self) -> Self:
        realized = (
            self.availability_start_s,
            self.availability_end_s,
            self.initial_location_id,
        )
        if self.active:
            if any(value is None for value in realized):
                raise ValueError("active vehicle requires a complete availability realization")
            assert self.availability_start_s is not None
            assert self.availability_end_s is not None
            if self.availability_end_s <= self.availability_start_s:
                raise ValueError("active vehicle availability must have positive duration")
        elif any(value is not None for value in realized):
            raise ValueError("inactive vehicle must not contain a realized availability window")
        return self


class VehicleSpec(ContractModel):
    key: VehicleKey
    availability_start_s: FiniteFloat
    availability_end_s: FiniteFloat
    initial_location_id: OpaqueId
    depot_location_id: OpaqueId | None = None
    capacity_mode: Literal["none", "consumable", "occupancy"] = "none"
    capacity: NonNegativeFloat | None = None
    quantity_unit: str | None = None
    assigned_area_ids: tuple[OpaqueId, ...] = ()
    identity_provenance: Literal["uploaded", "generated", "gtfs_block", "inferred_duty"]

    @model_validator(mode="after")
    def validate_vehicle(self) -> Self:
        if self.availability_end_s <= self.availability_start_s:
            raise ValueError("vehicle availability must have positive duration")
        if len(set(self.assigned_area_ids)) != len(self.assigned_area_ids):
            raise ValueError("assigned area IDs must be unique")
        if self.capacity_mode == "none":
            if self.capacity is not None or self.quantity_unit is not None:
                raise ValueError("capacity and quantity_unit must be absent in none mode")
        elif self.capacity is None or self.quantity_unit in (None, ""):
            raise ValueError("capacity modes require capacity and a quantity unit")
        if self.assigned_area_ids != tuple(sorted(self.assigned_area_ids)):
            raise ValueError("assigned area IDs must be canonically sorted")
        return self


class VehicleStatus(StrEnum):
    INACTIVE = "inactive"
    IDLE = "idle"
    EXECUTING_SERVICE = "executing_service"
    EXECUTING_OPERATIONAL = "executing_operational"
    OFF_SHIFT = "off_shift"


class VehicleState(ContractModel):
    key: VehicleKey
    status: VehicleStatus
    current_execution_id: OpaqueId | None
    committed_location_id: OpaqueId
    available_at_s: FiniteFloat
    remaining_capacity: NonNegativeFloat | None
    execution_generation: NonNegativeInt

    @model_validator(mode="after")
    def validate_execution_reference(self) -> Self:
        executing = self.status in {
            VehicleStatus.EXECUTING_SERVICE,
            VehicleStatus.EXECUTING_OPERATIONAL,
        }
        if executing != (self.current_execution_id is not None):
            raise ValueError("execution reference must agree with vehicle status")
        return self


class AssignmentRow(ContractModel):
    vehicle: VehicleKey
    order_index: NonNegativeInt
    task_id: OpaqueId


class AssignmentPlan(ContractModel):
    assignment_plan_id: OpaqueId
    rows: tuple[AssignmentRow, ...]

    @field_validator("rows")
    @classmethod
    def validate_rows(cls, value: tuple[AssignmentRow, ...]) -> tuple[AssignmentRow, ...]:
        task_ids = [row.task_id for row in value]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("a task may have only one predefined owner")
        positions = [
            (row.vehicle.fleet_id, row.vehicle.vehicle_id, row.order_index) for row in value
        ]
        if len(positions) != len(set(positions)):
            raise ValueError("predefined order indices must be unique per vehicle")
        return value


class EventPhase(StrEnum):
    COMPLETION = "completion"
    VEHICLE_EXIT = "vehicle_exit"
    VEHICLE_ENTRY = "vehicle_entry"
    RELEASE_OR_WAKEUP = "release_or_wakeup"


EVENT_PHASE_ORDER: dict[EventPhase, int] = {
    EventPhase.COMPLETION: 1,
    EventPhase.VEHICLE_EXIT: 2,
    EventPhase.VEHICLE_ENTRY: 3,
    EventPhase.RELEASE_OR_WAKEUP: 4,
}


class EventType(StrEnum):
    TASK_RELEASE = "task_release"
    VEHICLE_ENTRY = "vehicle_entry"
    EXECUTION_COMPLETION = "execution_completion"
    VEHICLE_EXIT = "vehicle_exit"
    POLICY_WAKEUP = "policy_wakeup"
    DISPATCH_BATCH = "dispatch_batch"


_EXPECTED_EVENT_PHASE = {
    EventType.TASK_RELEASE: EventPhase.RELEASE_OR_WAKEUP,
    EventType.VEHICLE_ENTRY: EventPhase.VEHICLE_ENTRY,
    EventType.EXECUTION_COMPLETION: EventPhase.COMPLETION,
    EventType.VEHICLE_EXIT: EventPhase.VEHICLE_EXIT,
    EventType.POLICY_WAKEUP: EventPhase.RELEASE_OR_WAKEUP,
    EventType.DISPATCH_BATCH: EventPhase.RELEASE_OR_WAKEUP,
}


class Event(ContractModel):
    time_s: FiniteFloat
    phase: EventPhase
    stable_tie_key: tuple[OpaqueId, ...]
    sequence: NonNegativeInt
    event_type: EventType
    fleet_id: OpaqueId | None = None
    vehicle_id: OpaqueId | None = None
    task_id: OpaqueId | None = None
    execution_generation: NonNegativeInt | None = None

    @model_validator(mode="after")
    def validate_phase_and_references(self) -> Self:
        if self.phase != _EXPECTED_EVENT_PHASE[self.event_type]:
            raise ValueError("event phase does not match event type")
        if not self.stable_tie_key:
            raise ValueError("stable_tie_key must be nonempty")
        required: dict[EventType, tuple[str, ...]] = {
            EventType.TASK_RELEASE: ("fleet_id", "task_id"),
            EventType.VEHICLE_ENTRY: ("fleet_id", "vehicle_id"),
            EventType.EXECUTION_COMPLETION: (
                "fleet_id",
                "vehicle_id",
                "task_id",
                "execution_generation",
            ),
            EventType.VEHICLE_EXIT: ("fleet_id", "vehicle_id", "execution_generation"),
            EventType.POLICY_WAKEUP: ("fleet_id", "vehicle_id", "execution_generation"),
            EventType.DISPATCH_BATCH: ("fleet_id",),
        }
        missing = [name for name in required[self.event_type] if getattr(self, name) is None]
        if missing:
            raise ValueError(
                f"{self.event_type.value} event requires references: {', '.join(missing)}"
            )
        return self

    @property
    def heap_key(self) -> tuple[float, int, tuple[str, ...], int]:
        return (self.time_s, EVENT_PHASE_ORDER[self.phase], self.stable_tie_key, self.sequence)


class RouteEdge(ContractModel):
    edge_id: OpaqueId
    length_m: PositiveFloat
    duration_s: PositiveFloat


class RouteResult(ContractModel):
    reachable: bool
    edges: tuple[RouteEdge, ...]
    total_duration_s: NonNegativeFloat
    total_distance_m: NonNegativeFloat
    network_hash: Sha256
    profile_hash: Sha256
    reason: str | None = None

    @model_validator(mode="after")
    def validate_reachability(self) -> Self:
        if self.reachable and self.reason is not None:
            raise ValueError("reachable route must not have an unreachable reason")
        if not self.reachable:
            if self.reason is None:
                raise ValueError("unreachable route requires a reason")
            if self.edges or self.total_duration_s != 0 or self.total_distance_m != 0:
                raise ValueError("unreachable route cannot contain realized edges or totals")
        if self.reachable:
            duration = math.fsum(edge.duration_s for edge in self.edges)
            distance = math.fsum(edge.length_m for edge in self.edges)
            if not math.isclose(
                duration, self.total_duration_s, rel_tol=1e-12, abs_tol=1e-9
            ) or not math.isclose(distance, self.total_distance_m, rel_tol=1e-12, abs_tol=1e-9):
                raise ValueError("route totals must equal ordered edge totals")
        return self


class MovementInterval(ContractModel):
    kind: Literal["movement"]
    start_s: FiniteFloat
    end_s: FiniteFloat
    movement_kind: Literal[
        "service_pickup", "service_inter_step", "cruise", "depot_return", "reposition"
    ]
    edge_id: OpaqueId
    edge_start_fraction: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
    edge_end_fraction: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]

    @model_validator(mode="after")
    def validate_interval(self) -> Self:
        if self.end_s <= self.start_s:
            raise ValueError("movement interval must have positive duration")
        if self.edge_end_fraction <= self.edge_start_fraction:
            raise ValueError("edge fractions must increase in traversal order")
        return self


class StationaryInterval(ContractModel):
    kind: Literal["service", "wait"]
    start_s: FiniteFloat
    end_s: FiniteFloat
    location_id: OpaqueId
    step_index: PositiveInt | None = None

    @model_validator(mode="after")
    def validate_interval(self) -> Self:
        if self.end_s <= self.start_s:
            raise ValueError("stationary interval must have positive duration")
        return self


ExecutionInterval: TypeAlias = Annotated[
    MovementInterval | StationaryInterval,
    Field(discriminator="kind"),
]


class CapacityMilestone(ContractModel):
    time_s: FiniteFloat
    step_index: PositiveInt | None
    quantity_delta: FiniteFloat | None = None
    reset_to_capacity: bool = False

    @model_validator(mode="after")
    def validate_action(self) -> Self:
        if (self.quantity_delta is None) == (not self.reset_to_capacity):
            raise ValueError("capacity milestone requires exactly one delta or reset")
        return self


class TaskMilestone(ContractModel):
    step_index: PositiveInt
    arrival_s: FiniteFloat
    service_start_s: FiniteFloat
    departure_s: FiniteFloat

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        if not self.arrival_s <= self.service_start_s <= self.departure_s:
            raise ValueError("task milestone times must be ordered")
        return self


class ExecutionPlan(ContractModel):
    execution_id: OpaqueId
    execution_token: NonNegativeInt
    vehicle: VehicleKey
    task_id: OpaqueId
    start_s: FiniteFloat
    end_s: FiniteFloat
    end_location_id: OpaqueId
    intervals: tuple[ExecutionInterval, ...]
    task_milestones: tuple[TaskMilestone, ...]
    capacity_milestones: tuple[CapacityMilestone, ...] = ()

    @model_validator(mode="after")
    def validate_timeline(self) -> Self:
        if self.end_s < self.start_s:
            raise ValueError("execution plan cannot end before it starts")
        cursor = self.start_s
        for interval in self.intervals:
            if not math.isclose(interval.start_s, cursor, rel_tol=1e-12, abs_tol=1e-9):
                raise ValueError("execution intervals must form a contiguous timeline")
            if interval.end_s > self.end_s and not math.isclose(
                interval.end_s, self.end_s, rel_tol=1e-12, abs_tol=1e-9
            ):
                raise ValueError("execution intervals must be bounded by the plan")
            cursor = interval.end_s
        if self.end_s > self.start_s and not self.intervals:
            raise ValueError("positive-duration execution plan requires intervals")
        if self.intervals and not math.isclose(cursor, self.end_s, rel_tol=1e-12, abs_tol=1e-9):
            raise ValueError("execution intervals must cover the complete plan")
        if self.end_s == self.start_s and self.intervals:
            raise ValueError("zero-duration execution plan cannot contain intervals")

        step_indices = tuple(milestone.step_index for milestone in self.task_milestones)
        if step_indices and step_indices != tuple(range(1, len(step_indices) + 1)):
            raise ValueError("task milestones require contiguous positive step indices")
        previous_departure = self.start_s
        for milestone in self.task_milestones:
            if (
                milestone.arrival_s < self.start_s
                or milestone.departure_s > self.end_s
                or milestone.arrival_s < previous_departure
            ):
                raise ValueError("task milestones must be ordered within the execution plan")
            previous_departure = milestone.departure_s
        milestone_times = [milestone.time_s for milestone in self.capacity_milestones]
        if milestone_times != sorted(milestone_times) or any(
            time < self.start_s or time > self.end_s for time in milestone_times
        ):
            raise ValueError("capacity milestones must be ordered within the execution plan")
        valid_steps = set(step_indices)
        if valid_steps and any(
            milestone.step_index is not None and milestone.step_index not in valid_steps
            for milestone in self.capacity_milestones
        ):
            raise ValueError("capacity milestone references an unknown task step")
        return self
