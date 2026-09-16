"""Deterministic event kernel and state lifecycle for one joint replication.

M05A deliberately delegates eligibility, dispatch, routing, and plan construction to
an injected decision adapter.  The kernel owns only global event ordering, state
transitions, execution generations, and shift/horizon finalization.
"""

from __future__ import annotations

import heapq
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Protocol

from mobile_sensing.contracts import (
    CancellationToken,
    ClockConfig,
    LocationRef,
    Event,
    EventPhase,
    EventType,
    ExecutionPlan,
    MovementInterval,
    ProgressSink,
    StationaryInterval,
    Task,
    TaskMilestone,
    CapacityMilestone,
    VehicleAvailability,
    VehicleKey,
    VehicleSpec,
    VehicleState,
    VehicleStatus,
)
from mobile_sensing.simulation.executor import MaterializedPosition, materialize_execution


EVENT_KERNEL_VERSION = "event-kernel@2"
DEFAULT_MAX_EVENTS = 1_000_000
DEFAULT_MAX_MICRO_ROUNDS = 100_000

TaskKey = tuple[str, str]
VehicleKeyTuple = tuple[str, str]


class TaskLifecycleStatus(StrEnum):
    UNRELEASED = "unreleased"
    WAITING = "waiting"
    ASSIGNED = "assigned"
    COMPLETED = "completed"
    REJECTED = "rejected"
    INTERRUPTED_SHIFT = "interrupted_shift"
    CENSORED_HORIZON = "censored_horizon"
    UNSERVED_HORIZON = "unserved_horizon"


class ExecutionLifecycleStatus(StrEnum):
    COMPLETED = "completed"
    INTERRUPTED_SHIFT = "interrupted_shift"
    CENSORED_HORIZON = "censored_horizon"


@dataclass(frozen=True, slots=True)
class DecisionSnapshot:
    """Immutable, canonically ordered view presented to an external decision adapter."""

    time_s: float
    idle_vehicles: tuple[VehicleState, ...]
    waiting_tasks: tuple[Task, ...]
    # Earliest later timestamp at which the kernel will reconsider dispatch.
    # Operational policies may materialize state up to this bound without
    # introducing an event for every internal movement segment.
    next_decision_s: float | None = None


@dataclass(frozen=True, slots=True)
class PlannedAssignment:
    """A task and complete plan proposed by the injected M05B/M05C seam."""

    task: Task
    plan: ExecutionPlan


class KernelDecisionAdapter(Protocol):
    """Decision seam; M05A tests use a minimal deterministic implementation only."""

    def service_assignments(self, snapshot: DecisionSnapshot) -> Sequence[PlannedAssignment]: ...

    def operational_assignments(
        self, snapshot: DecisionSnapshot
    ) -> Sequence[PlannedAssignment]: ...


@dataclass(frozen=True, slots=True)
class LifecycleEvent:
    event_index: int
    time_s: float
    phase_order: int
    event_type: str
    fleet_id: str | None = None
    vehicle_id: str | None = None
    task_id: str | None = None
    execution_id: str | None = None
    execution_generation: int | None = None


@dataclass(frozen=True, slots=True)
class TaskOutcome:
    fleet_id: str
    task_id: str
    kind: str
    source_policy: str | None
    release_s: float
    status: TaskLifecycleStatus
    assigned_at_s: float | None
    terminal_time_s: float | None
    vehicle_id: str | None
    execution_id: str | None
    first_service_s: float | None = None
    completion_s: float | None = None
    assignment_wait_s: float | None = None
    first_service_wait_s: float | None = None
    lateness_s: float | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class VehicleOutcome:
    vehicle: VehicleKey
    state: VehicleState
    entered_at_s: float | None
    exited_at_s: float | None


@dataclass(frozen=True, slots=True)
class IdleActivityInterval:
    vehicle: VehicleKey
    start_s: float
    end_s: float
    location_id: str

    def __post_init__(self) -> None:
        if not self.end_s > self.start_s:
            raise ValueError("idle activity interval must have positive duration")


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    execution_id: str
    vehicle: VehicleKey
    fleet_id: str
    task_id: str
    execution_generation: int
    assigned_at_s: float
    planned_end_s: float
    realized_end_s: float
    status: ExecutionLifecycleStatus
    realized_intervals: tuple[MovementInterval | StationaryInterval, ...] = ()
    planned_task_milestones: tuple[TaskMilestone, ...] = ()
    applied_capacity_milestones: tuple[CapacityMilestone, ...] = ()
    realized_position: MaterializedPosition | None = None


@dataclass(frozen=True, slots=True)
class KernelResult:
    replication_id: str
    simulation_start_s: float
    end_s: float
    task_outcomes: tuple[TaskOutcome, ...]
    vehicle_outcomes: tuple[VehicleOutcome, ...]
    execution_outcomes: tuple[ExecutionOutcome, ...]
    idle_activity_intervals: tuple[IdleActivityInterval, ...]
    lifecycle_events: tuple[LifecycleEvent, ...]
    processed_heap_events: int
    maximum_queue_size: int
    stationary_locations: tuple[LocationRef, ...] = ()


class KernelCancelled(RuntimeError):
    """Raised after a cancellation checkpoint; no complete result is returned."""

    def __init__(self, *, time_s: float, processed_heap_events: int) -> None:
        super().__init__(f"simulation cancelled at {time_s} s")
        self.time_s = time_s
        self.processed_heap_events = processed_heap_events


class _NeverCancelled:
    def is_cancelled(self) -> bool:
        return False


class _NullProgress:
    def update(self, *, phase: str, completed: int, total: int | None) -> None:
        return None


@dataclass(slots=True)
class _TaskRuntime:
    task: Task
    status: TaskLifecycleStatus
    assigned_at_s: float | None = None
    terminal_time_s: float | None = None
    vehicle_id: str | None = None
    execution_id: str | None = None


@dataclass(slots=True)
class _VehicleRuntime:
    spec: VehicleSpec
    availability: VehicleAvailability
    state: VehicleState
    entered_at_s: float | None = None
    exited_at_s: float | None = None
    idle_since_s: float | None = None


@dataclass(slots=True)
class _ActiveExecution:
    task_key: TaskKey
    plan: ExecutionPlan
    start_location_id: str


def _vehicle_tuple(key: VehicleKey) -> VehicleKeyTuple:
    return (key.fleet_id, key.vehicle_id)


def _task_key(task: Task) -> TaskKey:
    return (task.fleet_id, task.task_id)


class _Kernel:
    def __init__(
        self,
        *,
        replication_id: str,
        clock: ClockConfig,
        tasks: Sequence[Task],
        online_tasks: Iterable[Task],
        vehicle_specs: Sequence[VehicleSpec],
        availability: Sequence[VehicleAvailability],
        decisions: KernelDecisionAdapter,
        cancellation: CancellationToken,
        progress: ProgressSink,
        progress_frequency_events: int,
        max_events: int,
        max_micro_rounds: int,
    ) -> None:
        if replication_id == "":
            raise ValueError("replication_id must be nonempty")
        if isinstance(progress_frequency_events, bool) or progress_frequency_events <= 0:
            raise ValueError("progress_frequency_events must be a positive integer")
        if isinstance(max_events, bool) or max_events <= 0:
            raise ValueError("max_events must be a positive integer")
        if isinstance(max_micro_rounds, bool) or max_micro_rounds <= 0:
            raise ValueError("max_micro_rounds must be a positive integer")
        self.replication_id = replication_id
        self.start_s = float(clock.simulation_start_s)
        self.end_s = float(clock.end_s)
        self.decisions = decisions
        self.cancellation = cancellation
        self.progress = progress
        self.progress_frequency_events = progress_frequency_events
        self.max_events = max_events
        self.max_micro_rounds = max_micro_rounds
        self.queue: list[tuple[tuple[float, int, tuple[str, ...], int], Event]] = []
        self.next_sequence = 0
        self.total_scheduled_events = 0
        self.processed_heap_events = 0
        self.maximum_queue_size = 0
        self.last_progress_count = 0
        self.now_s = self.start_s
        self.lifecycle_events: list[LifecycleEvent] = []
        self.execution_ids: set[str] = set()
        self.executions: list[ExecutionOutcome] = []
        self.idle_activity_intervals: list[IdleActivityInterval] = []
        self.active_executions: dict[VehicleKeyTuple, _ActiveExecution] = {}
        self.tasks = self._initialize_tasks(tasks)
        self.waiting_keys = {
            key
            for key, runtime in self.tasks.items()
            if runtime.status == TaskLifecycleStatus.WAITING
        }
        self.online_iterator = iter(online_tasks)
        self.online_previous_key: tuple[float, str, str] | None = None
        self.online_pending: Task | None = None
        self._pull_online()
        self.vehicles = self._initialize_vehicles(vehicle_specs, availability)

    def _pull_online(self) -> None:
        self.online_pending = next(self.online_iterator, None)
        if self.online_pending is None:
            return
        task = self.online_pending
        key = (task.release_s, task.fleet_id, task.task_id)
        if self.online_previous_key is not None and key < self.online_previous_key:
            raise ValueError("Online tasks must be ordered by release, fleet, task ID")
        if not self.start_s <= task.release_s < self.end_s:
            raise ValueError("Online task releases must lie inside the execution interval")
        self.online_previous_key = key

    def _inject_online(self, time_s: float) -> None:
        # Inject the complete same-time group before any decision is evaluated.
        while self.online_pending is not None and self.online_pending.release_s <= time_s:
            task = self.online_pending
            key = _task_key(task)
            if key in self.tasks:
                raise ValueError("Online task duplicates an existing task key")
            self.tasks[key] = _TaskRuntime(task, TaskLifecycleStatus.UNRELEASED)
            self._push(
                self._event(
                    time_s=task.release_s,
                    event_type=EventType.TASK_RELEASE,
                    stable_tie_key=(task.fleet_id, task.task_id, "task_release"),
                    fleet_id=task.fleet_id,
                    task_id=task.task_id,
                )
            )
            self._pull_online()

    def _initialize_tasks(self, tasks: Sequence[Task]) -> dict[TaskKey, _TaskRuntime]:
        ordered = sorted(tasks, key=lambda item: (item.fleet_id, item.task_id))
        keys = [_task_key(task) for task in ordered]
        if len(keys) != len(set(keys)):
            raise ValueError("tasks must have unique (fleet_id, task_id) keys")
        return {
            key: _TaskRuntime(
                task=task,
                status=(
                    TaskLifecycleStatus.WAITING
                    if task.release_s < self.start_s
                    else TaskLifecycleStatus.UNRELEASED
                ),
            )
            for key, task in zip(keys, ordered, strict=True)
        }

    def _initialize_vehicles(
        self,
        specs: Sequence[VehicleSpec],
        availability: Sequence[VehicleAvailability],
    ) -> dict[VehicleKeyTuple, _VehicleRuntime]:
        ordered_specs = sorted(specs, key=lambda item: _vehicle_tuple(item.key))
        spec_keys = [_vehicle_tuple(item.key) for item in ordered_specs]
        if len(spec_keys) != len(set(spec_keys)):
            raise ValueError("vehicle specifications must have unique keys")
        ordered_availability = sorted(availability, key=lambda item: _vehicle_tuple(item.vehicle))
        availability_keys = [_vehicle_tuple(item.vehicle) for item in ordered_availability]
        if len(availability_keys) != len(set(availability_keys)):
            raise ValueError("vehicle availability must have unique keys")
        if spec_keys != availability_keys:
            raise ValueError("availability must cover the vehicle catalog exactly")
        rows: dict[VehicleKeyTuple, _VehicleRuntime] = {}
        for spec, realization in zip(ordered_specs, ordered_availability, strict=True):
            if realization.replication_id != self.replication_id:
                raise ValueError("vehicle availability replication_id mismatch")
            if realization.active:
                assert realization.availability_start_s is not None
                assert realization.availability_end_s is not None
                assert realization.initial_location_id is not None
                if (
                    realization.availability_start_s < spec.availability_start_s
                    or realization.availability_end_s > spec.availability_end_s
                ):
                    raise ValueError("realized availability must lie within the vehicle envelope")
                available_at_s = max(self.start_s, realization.availability_start_s)
                location_id = realization.initial_location_id
                initial_status = (
                    VehicleStatus.OFF_SHIFT
                    if realization.availability_end_s <= self.start_s
                    else VehicleStatus.INACTIVE
                )
            else:
                available_at_s = spec.availability_start_s
                location_id = spec.initial_location_id
                initial_status = VehicleStatus.INACTIVE
            rows[_vehicle_tuple(spec.key)] = _VehicleRuntime(
                spec=spec,
                availability=realization,
                state=VehicleState(
                    key=spec.key,
                    status=initial_status,
                    current_execution_id=None,
                    committed_location_id=location_id,
                    available_at_s=available_at_s,
                    remaining_capacity=spec.capacity,
                    execution_generation=0,
                ),
            )
        return rows

    def _event(
        self,
        *,
        time_s: float,
        event_type: EventType,
        stable_tie_key: tuple[str, ...],
        fleet_id: str | None = None,
        vehicle_id: str | None = None,
        task_id: str | None = None,
        execution_generation: int | None = None,
    ) -> Event:
        phase = {
            EventType.EXECUTION_COMPLETION: EventPhase.COMPLETION,
            EventType.VEHICLE_EXIT: EventPhase.VEHICLE_EXIT,
            EventType.VEHICLE_ENTRY: EventPhase.VEHICLE_ENTRY,
            EventType.TASK_RELEASE: EventPhase.RELEASE_OR_WAKEUP,
            EventType.POLICY_WAKEUP: EventPhase.RELEASE_OR_WAKEUP,
            EventType.DISPATCH_BATCH: EventPhase.RELEASE_OR_WAKEUP,
        }[event_type]
        event = Event(
            time_s=time_s,
            phase=phase,
            stable_tie_key=stable_tie_key,
            sequence=self.next_sequence,
            event_type=event_type,
            fleet_id=fleet_id,
            vehicle_id=vehicle_id,
            task_id=task_id,
            execution_generation=execution_generation,
        )
        self.next_sequence += 1
        return event

    def _push(self, event: Event) -> None:
        self.total_scheduled_events += 1
        if self.total_scheduled_events > self.max_events:
            raise RuntimeError(f"event limit exceeded ({self.max_events})")
        heapq.heappush(self.queue, (event.heap_key, event))
        self.maximum_queue_size = max(self.maximum_queue_size, len(self.queue))

    def _schedule_initial_events(self) -> None:
        candidates: list[dict[str, object]] = []
        for fleet_id, origin, interval in getattr(
            self.decisions, "batch_wakeup_specs", lambda: ()
        )():
            first = max(0, math.ceil((self.start_s - origin) / interval))
            last = math.ceil((self.end_s - origin) / interval)
            if max(0, last - first) > self.max_events - len(candidates):
                raise RuntimeError(
                    "Batch interval produces more events than the configured event limit"
                )
            for rank in range(first, last):
                time_s = origin + rank * interval
                if self.start_s <= time_s < self.end_s:
                    candidates.append(
                        {
                            "time_s": time_s,
                            "event_type": EventType.DISPATCH_BATCH,
                            "stable_tie_key": (fleet_id, "dispatch_batch", str(rank)),
                            "fleet_id": fleet_id,
                        }
                    )
        for runtime in self.tasks.values():
            task = runtime.task
            if self.start_s <= task.release_s < self.end_s:
                candidates.append(
                    {
                        "time_s": task.release_s,
                        "event_type": EventType.TASK_RELEASE,
                        "stable_tie_key": (task.fleet_id, task.task_id, "task_release"),
                        "fleet_id": task.fleet_id,
                        "task_id": task.task_id,
                    }
                )
        for key, runtime in self.vehicles.items():
            realization = runtime.availability
            if not realization.active:
                continue
            assert realization.availability_start_s is not None
            assert realization.availability_end_s is not None
            if realization.availability_end_s <= self.start_s:
                continue
            entry_s = max(self.start_s, realization.availability_start_s)
            if entry_s < self.end_s:
                candidates.append(
                    {
                        "time_s": entry_s,
                        "event_type": EventType.VEHICLE_ENTRY,
                        "stable_tie_key": (*key, "vehicle_entry"),
                        "fleet_id": key[0],
                        "vehicle_id": key[1],
                    }
                )
            if self.start_s < realization.availability_end_s < self.end_s:
                candidates.append(
                    {
                        "time_s": realization.availability_end_s,
                        "event_type": EventType.VEHICLE_EXIT,
                        "stable_tie_key": (*key, "vehicle_exit"),
                        "fleet_id": key[0],
                        "vehicle_id": key[1],
                        "execution_generation": 0,
                    }
                )
        phase_order = {
            EventType.EXECUTION_COMPLETION: 1,
            EventType.VEHICLE_EXIT: 2,
            EventType.VEHICLE_ENTRY: 3,
            EventType.TASK_RELEASE: 4,
            EventType.POLICY_WAKEUP: 4,
            EventType.DISPATCH_BATCH: 4,
        }
        candidates.sort(
            key=lambda item: (
                float(item["time_s"]),
                phase_order[item["event_type"]],  # type: ignore[index]
                item["stable_tie_key"],
            )
        )
        for item in candidates:
            self._push(self._event(**item))  # type: ignore[arg-type]

    def _record(
        self,
        *,
        time_s: float,
        phase_order: int,
        event_type: str,
        fleet_id: str | None = None,
        vehicle_id: str | None = None,
        task_id: str | None = None,
        execution_id: str | None = None,
        execution_generation: int | None = None,
    ) -> None:
        self.lifecycle_events.append(
            LifecycleEvent(
                event_index=len(self.lifecycle_events),
                time_s=time_s,
                phase_order=phase_order,
                event_type=event_type,
                fleet_id=fleet_id,
                vehicle_id=vehicle_id,
                task_id=task_id,
                execution_id=execution_id,
                execution_generation=execution_generation,
            )
        )

    def _check_cancelled(self) -> None:
        if not self.cancellation.is_cancelled():
            return
        for key, active in sorted(self.active_executions.items()):
            runtime = self.vehicles[key]
            runtime.state = runtime.state.model_copy(
                update={
                    "current_execution_id": None,
                    "status": VehicleStatus.IDLE,
                    "available_at_s": self.now_s,
                    "execution_generation": runtime.state.execution_generation + 1,
                }
            )
            task = self.tasks[active.task_key]
            task.execution_id = active.plan.execution_id
        self.active_executions.clear()
        raise KernelCancelled(
            time_s=self.now_s,
            processed_heap_events=self.processed_heap_events,
        )

    def _maybe_progress(self) -> None:
        if self.processed_heap_events - self.last_progress_count >= self.progress_frequency_events:
            self.progress.update(
                phase="simulation.events",
                completed=self.processed_heap_events,
                total=None,
            )
            self.last_progress_count = self.processed_heap_events

    def _pop_timestamp(self, time_s: float) -> list[Event]:
        events = []
        while self.queue and self.queue[0][1].time_s == time_s:
            _, event = heapq.heappop(self.queue)
            events.append(event)
            self.processed_heap_events += 1
        events.sort(key=lambda item: item.heap_key)
        return events

    def _release_task(self, event: Event) -> None:
        assert event.fleet_id is not None and event.task_id is not None
        runtime = self.tasks[(event.fleet_id, event.task_id)]
        if runtime.status != TaskLifecycleStatus.UNRELEASED:
            raise RuntimeError("duplicate task release event")
        runtime.status = TaskLifecycleStatus.WAITING
        self.waiting_keys.add((event.fleet_id, event.task_id))
        self._record(
            time_s=event.time_s,
            phase_order=4,
            event_type=EventType.TASK_RELEASE.value,
            fleet_id=event.fleet_id,
            task_id=event.task_id,
        )

    def _enter_vehicle(self, event: Event) -> None:
        assert event.fleet_id is not None and event.vehicle_id is not None
        key = (event.fleet_id, event.vehicle_id)
        runtime = self.vehicles[key]
        if runtime.state.status != VehicleStatus.INACTIVE:
            raise RuntimeError("vehicle entry requires inactive state")
        realization = runtime.availability
        assert realization.availability_end_s is not None
        if not event.time_s < min(realization.availability_end_s, self.end_s):
            raise RuntimeError("vehicle cannot enter at its shift end or horizon")
        runtime.state = runtime.state.model_copy(
            update={
                "status": VehicleStatus.IDLE,
                "available_at_s": event.time_s,
            }
        )
        runtime.entered_at_s = event.time_s
        runtime.idle_since_s = event.time_s
        self._record(
            time_s=event.time_s,
            phase_order=3,
            event_type=EventType.VEHICLE_ENTRY.value,
            fleet_id=event.fleet_id,
            vehicle_id=event.vehicle_id,
            execution_generation=runtime.state.execution_generation,
        )

    def _apply_capacity(
        self, runtime: _VehicleRuntime, plan: ExecutionPlan, cutoff_s: float
    ) -> None:
        remaining = runtime.state.remaining_capacity
        if remaining is None:
            if plan.capacity_milestones:
                raise RuntimeError("capacity-free vehicle received capacity milestones")
            return
        assert runtime.spec.capacity is not None
        for milestone in plan.capacity_milestones:
            if milestone.time_s > cutoff_s:
                break
            if milestone.reset_to_capacity:
                remaining = runtime.spec.capacity
            else:
                assert milestone.quantity_delta is not None
                remaining += milestone.quantity_delta
            if remaining < 0 or remaining > runtime.spec.capacity:
                raise RuntimeError("execution capacity milestone violates vehicle bounds")
        runtime.state = runtime.state.model_copy(update={"remaining_capacity": remaining})

    def _complete_execution(self, event: Event) -> bool:
        assert event.fleet_id is not None and event.vehicle_id is not None
        assert event.task_id is not None and event.execution_generation is not None
        key = (event.fleet_id, event.vehicle_id)
        runtime = self.vehicles[key]
        active = self.active_executions.get(key)
        if (
            active is None
            or runtime.state.execution_generation != event.execution_generation
            or active.plan.task_id != event.task_id
        ):
            self._record(
                time_s=event.time_s,
                phase_order=1,
                event_type="stale_completion_ignored",
                fleet_id=event.fleet_id,
                vehicle_id=event.vehicle_id,
                task_id=event.task_id,
                execution_generation=event.execution_generation,
            )
            return False
        plan = active.plan
        task = self.tasks[active.task_key]
        if task.status != TaskLifecycleStatus.ASSIGNED:
            raise RuntimeError("active execution task is not assigned")
        self._apply_capacity(runtime, plan, event.time_s)
        materialized = materialize_execution(
            plan,
            task.task,
            start_location_id=active.start_location_id,
            cutoff_s=event.time_s,
        )
        task.status = TaskLifecycleStatus.COMPLETED
        task.terminal_time_s = event.time_s
        runtime.state = runtime.state.model_copy(
            update={
                "status": VehicleStatus.IDLE,
                "current_execution_id": None,
                "committed_location_id": plan.end_location_id,
                "available_at_s": event.time_s,
            }
        )
        runtime.idle_since_s = event.time_s
        del self.active_executions[key]
        self.executions.append(
            ExecutionOutcome(
                execution_id=plan.execution_id,
                vehicle=plan.vehicle,
                fleet_id=active.task_key[0],
                task_id=active.task_key[1],
                execution_generation=plan.execution_token,
                assigned_at_s=plan.start_s,
                planned_end_s=plan.end_s,
                realized_end_s=event.time_s,
                status=ExecutionLifecycleStatus.COMPLETED,
                realized_intervals=materialized.intervals,
                planned_task_milestones=plan.task_milestones,
                applied_capacity_milestones=tuple(
                    milestone
                    for milestone in plan.capacity_milestones
                    if milestone.time_s <= event.time_s
                ),
                realized_position=materialized.position,
            )
        )
        # Cruise is an operating state, not demand. Retain its exact movement
        # intervals for exposure, but do not materialize task outcomes or
        # operational-event rows for an internal implementation detail.
        if task.task.kind == "cruise":
            del self.tasks[active.task_key]
        else:
            self._record(
                time_s=event.time_s,
                phase_order=1,
                event_type=EventType.EXECUTION_COMPLETION.value,
                fleet_id=event.fleet_id,
                vehicle_id=event.vehicle_id,
                task_id=event.task_id,
                execution_id=plan.execution_id,
                execution_generation=event.execution_generation,
            )
        return True

    def _close_idle(self, runtime: _VehicleRuntime, end_s: float) -> None:
        start_s = runtime.idle_since_s
        if start_s is None:
            return
        if end_s < start_s:
            raise RuntimeError("idle activity cannot end before it starts")
        if end_s > start_s:
            self.idle_activity_intervals.append(
                IdleActivityInterval(
                    vehicle=runtime.state.key,
                    start_s=start_s,
                    end_s=end_s,
                    location_id=runtime.state.committed_location_id,
                )
            )
        runtime.idle_since_s = None

    def _truncate_execution(
        self,
        key: VehicleKeyTuple,
        *,
        cutoff_s: float,
        task_status: TaskLifecycleStatus,
        execution_status: ExecutionLifecycleStatus,
    ) -> None:
        runtime = self.vehicles[key]
        active = self.active_executions.pop(key)
        plan = active.plan
        task = self.tasks[active.task_key]
        self._apply_capacity(runtime, plan, cutoff_s)
        materialized = materialize_execution(
            plan,
            task.task,
            start_location_id=active.start_location_id,
            cutoff_s=cutoff_s,
        )
        task.status = task_status
        task.terminal_time_s = cutoff_s
        runtime.state = runtime.state.model_copy(
            update={
                "status": VehicleStatus.IDLE,
                "current_execution_id": None,
                "available_at_s": cutoff_s,
                "execution_generation": runtime.state.execution_generation + 1,
                "committed_location_id": (
                    materialized.position.location_id
                    if materialized.position.location_id is not None
                    else active.start_location_id
                ),
            }
        )
        self.executions.append(
            ExecutionOutcome(
                execution_id=plan.execution_id,
                vehicle=plan.vehicle,
                fleet_id=active.task_key[0],
                task_id=active.task_key[1],
                execution_generation=plan.execution_token,
                assigned_at_s=plan.start_s,
                planned_end_s=plan.end_s,
                realized_end_s=cutoff_s,
                status=execution_status,
                realized_intervals=materialized.intervals,
                planned_task_milestones=plan.task_milestones,
                applied_capacity_milestones=tuple(
                    milestone
                    for milestone in plan.capacity_milestones
                    if milestone.time_s <= cutoff_s
                ),
                realized_position=materialized.position,
            )
        )
        if task.task.kind == "cruise":
            del self.tasks[active.task_key]
        else:
            self._record(
                time_s=cutoff_s,
                phase_order=2 if task_status == TaskLifecycleStatus.INTERRUPTED_SHIFT else 7,
                event_type=task_status.value,
                fleet_id=key[0],
                vehicle_id=key[1],
                task_id=active.task_key[1],
                execution_id=plan.execution_id,
                execution_generation=plan.execution_token,
            )

    def _exit_vehicle(self, event: Event) -> None:
        assert event.fleet_id is not None and event.vehicle_id is not None
        key = (event.fleet_id, event.vehicle_id)
        runtime = self.vehicles[key]
        if key in self.active_executions:
            self._truncate_execution(
                key,
                cutoff_s=event.time_s,
                task_status=TaskLifecycleStatus.INTERRUPTED_SHIFT,
                execution_status=ExecutionLifecycleStatus.INTERRUPTED_SHIFT,
            )
        elif runtime.state.status == VehicleStatus.IDLE:
            self._close_idle(runtime, event.time_s)
            runtime.state = runtime.state.model_copy(
                update={
                    "available_at_s": event.time_s,
                    "execution_generation": runtime.state.execution_generation + 1,
                }
            )
        elif runtime.state.status == VehicleStatus.OFF_SHIFT:
            raise RuntimeError("duplicate vehicle exit event")
        runtime.state = runtime.state.model_copy(
            update={
                "status": VehicleStatus.OFF_SHIFT,
                "current_execution_id": None,
            }
        )
        runtime.exited_at_s = event.time_s
        self._record(
            time_s=event.time_s,
            phase_order=2,
            event_type=EventType.VEHICLE_EXIT.value,
            fleet_id=event.fleet_id,
            vehicle_id=event.vehicle_id,
            execution_generation=runtime.state.execution_generation,
        )

    def _snapshot(self) -> DecisionSnapshot:
        idle = tuple(
            runtime.state
            for key, runtime in sorted(self.vehicles.items())
            if runtime.state.status == VehicleStatus.IDLE
            and runtime.availability.active
            and runtime.availability.availability_end_s is not None
            and self.now_s < runtime.availability.availability_end_s
            and self.now_s < self.end_s
        )
        waiting = tuple(self.tasks[key].task for key in sorted(self.waiting_keys))
        next_candidates = [self.end_s]
        if self.queue:
            next_candidates.extend(
                event.time_s for _, event in self.queue if event.time_s > self.now_s
            )
        if self.online_pending is not None and self.online_pending.release_s > self.now_s:
            next_candidates.append(self.online_pending.release_s)
        return DecisionSnapshot(
            time_s=self.now_s,
            idle_vehicles=idle,
            waiting_tasks=waiting,
            next_decision_s=min(next_candidates),
        )

    def _apply_assignments(
        self,
        proposed: Sequence[PlannedAssignment],
        *,
        operational: bool,
    ) -> int:
        assignments = tuple(
            sorted(
                proposed,
                key=lambda item: (
                    item.plan.vehicle.fleet_id,
                    item.plan.vehicle.vehicle_id,
                    item.task.fleet_id,
                    item.task.task_id,
                    item.plan.execution_id,
                ),
            )
        )
        vehicle_keys = [_vehicle_tuple(item.plan.vehicle) for item in assignments]
        task_keys = [_task_key(item.task) for item in assignments]
        if len(vehicle_keys) != len(set(vehicle_keys)):
            raise ValueError("one decision round cannot assign a vehicle twice")
        if len(task_keys) != len(set(task_keys)):
            raise ValueError("one decision round cannot assign a task twice")
        immediate = 0
        for assignment in assignments:
            task = assignment.task
            plan = assignment.plan
            key = _vehicle_tuple(plan.vehicle)
            task_key = _task_key(task)
            try:
                vehicle = self.vehicles[key]
            except KeyError as exc:
                raise ValueError(f"assignment references unknown vehicle {key}") from exc
            if vehicle.state.status != VehicleStatus.IDLE:
                raise ValueError("assignment requires an idle vehicle")
            if task.fleet_id != key[0]:
                raise ValueError("task and vehicle fleet IDs must match")
            assert vehicle.availability.availability_end_s is not None
            if not self.now_s < min(vehicle.availability.availability_end_s, self.end_s):
                raise ValueError("assignment is forbidden at shift end or horizon")
            if operational:
                if task.kind == "service":
                    raise ValueError("operational phase requires an operational task kind")
                if task_key in self.tasks:
                    raise ValueError("operational task key must be newly generated")
                if task.release_s > self.now_s:
                    raise ValueError("operational task cannot be assigned before release")
                if plan.end_s <= self.now_s:
                    raise ValueError("operational task execution must have positive duration")
                self.tasks[task_key] = _TaskRuntime(task=task, status=TaskLifecycleStatus.WAITING)
            else:
                try:
                    known_task = self.tasks[task_key]
                except KeyError as exc:
                    raise ValueError(
                        f"assignment references unknown service task {task_key}"
                    ) from exc
                if known_task.task != task:
                    raise ValueError("assignment must use the exact waiting task")
                if known_task.status != TaskLifecycleStatus.WAITING:
                    raise ValueError("service assignment requires a waiting task")
            if plan.task_id != task.task_id or plan.vehicle != vehicle.state.key:
                raise ValueError("execution plan references must match its assignment")
            if not math.isclose(plan.start_s, self.now_s, rel_tol=0.0, abs_tol=0.0):
                raise ValueError("execution plan must start at the decision timestamp")
            expected_token = vehicle.state.execution_generation + 1
            if plan.execution_token != expected_token:
                raise ValueError("execution plan token must be the next vehicle generation")
            if plan.execution_id in self.execution_ids:
                raise ValueError("execution_id must be globally unique within a replication")
            runtime_task = self.tasks[task_key]
            runtime_task.status = TaskLifecycleStatus.ASSIGNED
            self.waiting_keys.discard(task_key)
            runtime_task.assigned_at_s = self.now_s
            runtime_task.vehicle_id = key[1]
            runtime_task.execution_id = plan.execution_id
            self._close_idle(vehicle, self.now_s)
            vehicle.state = vehicle.state.model_copy(
                update={
                    "status": (
                        VehicleStatus.EXECUTING_SERVICE
                        if task.kind == "service"
                        else VehicleStatus.EXECUTING_OPERATIONAL
                    ),
                    "current_execution_id": plan.execution_id,
                    "available_at_s": plan.end_s,
                    "execution_generation": plan.execution_token,
                }
            )
            self.execution_ids.add(plan.execution_id)
            self.active_executions[key] = _ActiveExecution(
                task_key=task_key,
                plan=plan,
                start_location_id=vehicle.state.committed_location_id,
            )
            if task.kind != "cruise":
                self._record(
                    time_s=self.now_s,
                    phase_order=6 if operational else 5,
                    event_type="operational_assignment" if operational else "service_assignment",
                    fleet_id=key[0],
                    vehicle_id=key[1],
                    task_id=task.task_id,
                    execution_id=plan.execution_id,
                    execution_generation=plan.execution_token,
                )
            completion = self._event(
                time_s=plan.end_s,
                event_type=EventType.EXECUTION_COMPLETION,
                stable_tie_key=(*key, task.task_id, plan.execution_id),
                fleet_id=key[0],
                vehicle_id=key[1],
                task_id=task.task_id,
                execution_generation=plan.execution_token,
            )
            self._push(completion)
            if plan.end_s == self.now_s:
                immediate += 1
        return immediate

    def _drain_zero_completions(self) -> int:
        completions = []
        while (
            self.queue
            and self.queue[0][1].time_s == self.now_s
            and self.queue[0][1].phase == EventPhase.COMPLETION
        ):
            _, event = heapq.heappop(self.queue)
            completions.append(event)
            self.processed_heap_events += 1
        for event in sorted(completions, key=lambda item: item.heap_key):
            self._complete_execution(event)
        return len(completions)

    def _decision_closure(self) -> None:
        rounds = 0
        while True:
            self._check_cancelled()
            rounds += 1
            if rounds > self.max_micro_rounds:
                raise RuntimeError(
                    f"same-timestamp decision closure exceeded {self.max_micro_rounds} rounds"
                )
            snapshot = self._snapshot()
            immediate = self._apply_assignments(
                self.decisions.service_assignments(snapshot), operational=False
            )
            if immediate:
                drained = self._drain_zero_completions()
                if drained != immediate:
                    raise RuntimeError("zero-duration completion queue is inconsistent")
                continue
            operational_snapshot = self._snapshot()
            self._apply_assignments(
                self.decisions.operational_assignments(operational_snapshot), operational=True
            )
            return

    def _process_timestamp(self, time_s: float) -> None:
        self.now_s = time_s
        self._check_cancelled()
        events = self._pop_timestamp(time_s)
        for event in events:
            if event.phase == EventPhase.COMPLETION:
                self._complete_execution(event)
        for event in events:
            if event.phase == EventPhase.VEHICLE_EXIT:
                self._exit_vehicle(event)
        for event in events:
            if event.phase == EventPhase.VEHICLE_ENTRY:
                self._enter_vehicle(event)
        for event in events:
            if event.event_type == EventType.TASK_RELEASE:
                self._release_task(event)
            elif event.event_type in {EventType.POLICY_WAKEUP, EventType.DISPATCH_BATCH}:
                self._record(
                    time_s=event.time_s,
                    phase_order=4,
                    event_type=event.event_type.value,
                    fleet_id=event.fleet_id,
                    vehicle_id=event.vehicle_id,
                    execution_generation=event.execution_generation,
                )
        self._decision_closure()
        self._maybe_progress()

    def _finalize_horizon(self) -> None:
        self.now_s = self.end_s
        self._check_cancelled()
        exact_completions = []
        while (
            self.queue
            and self.queue[0][1].time_s == self.end_s
            and self.queue[0][1].phase == EventPhase.COMPLETION
        ):
            _, event = heapq.heappop(self.queue)
            exact_completions.append(event)
            self.processed_heap_events += 1
        for event in sorted(exact_completions, key=lambda item: item.heap_key):
            self._complete_execution(event)
        for key in sorted(tuple(self.active_executions)):
            self._truncate_execution(
                key,
                cutoff_s=self.end_s,
                task_status=TaskLifecycleStatus.CENSORED_HORIZON,
                execution_status=ExecutionLifecycleStatus.CENSORED_HORIZON,
            )
        for task_key, runtime in sorted(self.tasks.items()):
            if runtime.status == TaskLifecycleStatus.WAITING:
                runtime.status = TaskLifecycleStatus.UNSERVED_HORIZON
                runtime.terminal_time_s = self.end_s
                self._record(
                    time_s=self.end_s,
                    phase_order=7,
                    event_type=TaskLifecycleStatus.UNSERVED_HORIZON.value,
                    fleet_id=task_key[0],
                    task_id=task_key[1],
                )
        for _, runtime in sorted(self.vehicles.items()):
            if runtime.state.status == VehicleStatus.IDLE:
                self._close_idle(runtime, self.end_s)
        for key, runtime in sorted(self.vehicles.items()):
            realization = runtime.availability
            if (
                realization.active
                and realization.availability_end_s is not None
                and realization.availability_end_s <= self.end_s
                and runtime.state.status != VehicleStatus.OFF_SHIFT
            ):
                runtime.state = runtime.state.model_copy(
                    update={
                        "status": VehicleStatus.OFF_SHIFT,
                        "current_execution_id": None,
                        "available_at_s": self.end_s,
                        "execution_generation": runtime.state.execution_generation + 1,
                    }
                )
                runtime.exited_at_s = self.end_s
                self._record(
                    time_s=self.end_s,
                    phase_order=7,
                    event_type=EventType.VEHICLE_EXIT.value,
                    fleet_id=key[0],
                    vehicle_id=key[1],
                    execution_generation=runtime.state.execution_generation,
                )

    def run(self) -> KernelResult:
        self.progress.update(phase="simulation.initializing", completed=0, total=None)
        self._check_cancelled()
        self._schedule_initial_events()
        while self.queue or self.online_pending is not None:
            next_time = min(
                self.queue[0][1].time_s if self.queue else math.inf,
                self.online_pending.release_s if self.online_pending is not None else math.inf,
            )
            if next_time >= self.end_s:
                break
            if next_time < self.start_s:
                raise RuntimeError("event queue contains an event before simulation_start_s")
            self._inject_online(next_time)
            self._process_timestamp(next_time)
        self.progress.update(
            phase="simulation.finalizing",
            completed=self.processed_heap_events,
            total=None,
        )
        self._finalize_horizon()
        self.progress.update(
            phase="simulation.completed",
            completed=self.processed_heap_events,
            total=self.processed_heap_events,
        )
        execution_by_id = {row.execution_id: row for row in self.executions}

        def task_outcome(key: TaskKey, runtime: _TaskRuntime) -> TaskOutcome:
            execution = (
                execution_by_id.get(runtime.execution_id)
                if runtime.execution_id is not None
                else None
            )
            first_service_s = None
            first_step_index = None
            if execution is not None:
                services = [
                    interval
                    for interval in execution.realized_intervals
                    if interval.kind == "service"
                ]
                if services:
                    first_service_s = services[0].start_s
                    first_step_index = services[0].step_index
                elif (
                    runtime.status == TaskLifecycleStatus.COMPLETED
                    and execution.planned_task_milestones
                ):
                    milestone = execution.planned_task_milestones[0]
                    first_service_s = milestone.service_start_s
                    first_step_index = milestone.step_index
            scheduled_s = None
            if first_step_index is not None:
                scheduled_s = next(
                    (
                        step.scheduled_time_s
                        for step in runtime.task.steps
                        if step.step_index == first_step_index
                    ),
                    None,
                )
            assignment_wait_s = (
                runtime.assigned_at_s - runtime.task.release_s
                if runtime.assigned_at_s is not None
                else None
            )
            first_service_wait_s = (
                first_service_s - runtime.task.release_s if first_service_s is not None else None
            )
            return TaskOutcome(
                fleet_id=key[0],
                task_id=key[1],
                kind=runtime.task.kind,
                source_policy=runtime.task.source_policy,
                release_s=runtime.task.release_s,
                status=runtime.status,
                assigned_at_s=runtime.assigned_at_s,
                terminal_time_s=runtime.terminal_time_s,
                vehicle_id=runtime.vehicle_id,
                execution_id=runtime.execution_id,
                first_service_s=first_service_s,
                completion_s=(
                    runtime.terminal_time_s
                    if runtime.status == TaskLifecycleStatus.COMPLETED
                    else None
                ),
                assignment_wait_s=assignment_wait_s,
                first_service_wait_s=first_service_wait_s,
                lateness_s=(
                    max(0.0, first_service_s - scheduled_s)
                    if first_service_s is not None and scheduled_s is not None
                    else None
                ),
                reason=(
                    None
                    if runtime.status == TaskLifecycleStatus.COMPLETED
                    else runtime.status.value
                ),
            )

        return KernelResult(
            replication_id=self.replication_id,
            simulation_start_s=self.start_s,
            end_s=self.end_s,
            task_outcomes=tuple(
                task_outcome(key, runtime) for key, runtime in sorted(self.tasks.items())
            ),
            vehicle_outcomes=tuple(
                VehicleOutcome(
                    vehicle=runtime.state.key,
                    state=runtime.state,
                    entered_at_s=runtime.entered_at_s,
                    exited_at_s=runtime.exited_at_s,
                )
                for _, runtime in sorted(self.vehicles.items())
            ),
            execution_outcomes=tuple(
                sorted(
                    self.executions,
                    key=lambda item: (
                        item.realized_end_s,
                        item.vehicle.fleet_id,
                        item.vehicle.vehicle_id,
                        item.execution_id,
                    ),
                )
            ),
            idle_activity_intervals=tuple(
                sorted(
                    self.idle_activity_intervals,
                    key=lambda item: (
                        item.start_s,
                        item.vehicle.fleet_id,
                        item.vehicle.vehicle_id,
                    ),
                )
            ),
            lifecycle_events=tuple(self.lifecycle_events),
            processed_heap_events=self.processed_heap_events,
            maximum_queue_size=self.maximum_queue_size,
        )


def run_event_kernel(
    *,
    replication_id: str,
    clock: ClockConfig,
    tasks: Sequence[Task],
    online_tasks: Iterable[Task] = (),
    vehicle_specs: Sequence[VehicleSpec],
    availability: Sequence[VehicleAvailability],
    decisions: KernelDecisionAdapter,
    cancellation: CancellationToken | None = None,
    progress: ProgressSink | None = None,
    progress_frequency_events: int = 1_000,
    max_events: int = DEFAULT_MAX_EVENTS,
    max_micro_rounds: int = DEFAULT_MAX_MICRO_ROUNDS,
) -> KernelResult:
    """Run one complete deterministic replication or raise ``KernelCancelled``."""

    result = _Kernel(
        replication_id=replication_id,
        clock=clock,
        tasks=tasks,
        online_tasks=online_tasks,
        vehicle_specs=vehicle_specs,
        availability=availability,
        decisions=decisions,
        cancellation=cancellation or _NeverCancelled(),
        progress=progress or _NullProgress(),
        progress_frequency_events=progress_frequency_events,
        max_events=max_events,
        max_micro_rounds=max_micro_rounds,
    ).run()

    used = {
        i.location_id
        for outcome in result.execution_outcomes
        for i in outcome.realized_intervals
        if i.kind != "movement"
    }
    used.update(i.location_id for i in result.idle_activity_intervals)
    resolved = getattr(decisions, "locations", {})
    return replace(
        result,
        stationary_locations=tuple(
            resolved[key] for key in sorted(used - {None}) if key in resolved
        ),
    )
