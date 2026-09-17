from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mobile_sensing.contracts import (
    ClockConfig,
    ExecutionPlan,
    StationaryInterval,
    Task,
    TaskMilestone,
    TaskStep,
    VehicleAvailability,
    VehicleKey,
    VehicleSpec,
    VehicleStatus,
)
from mobile_sensing.simulation import (
    DecisionSnapshot,
    ExecutionLifecycleStatus,
    KernelCancelled,
    PlannedAssignment,
    TaskLifecycleStatus,
    run_event_kernel,
)


FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "simulation"
    / "simultaneous_event_order.json"
)


def _clock(*, end_s: float = 20.0) -> ClockConfig:
    return ClockConfig(
        origin_utc=datetime(2026, 1, 1, tzinfo=timezone.utc),
        display_timezone="UTC",
        simulation_start_s=0.0,
        observation_start_s=0.0,
        end_s=end_s,
    )


def _task(task_id: str, *, release_s: float, fleet_id: str = "fleet") -> Task:
    return Task(
        task_id=task_id,
        fleet_id=fleet_id,
        release_s=release_s,
        steps=(TaskStep(step_index=1, location_id=f"location_{task_id}"),),
    )


def _vehicle(
    vehicle_id: str,
    *,
    start_s: float,
    end_s: float,
    fleet_id: str = "fleet",
    active: bool = True,
) -> tuple[VehicleSpec, VehicleAvailability]:
    key = VehicleKey(fleet_id=fleet_id, vehicle_id=vehicle_id)
    spec = VehicleSpec(
        key=key,
        availability_start_s=start_s,
        availability_end_s=end_s,
        initial_location_id=f"initial_{vehicle_id}",
        identity_provenance="uploaded",
    )
    availability = VehicleAvailability(
        replication_id="replication_001",
        vehicle=key,
        active=active,
        availability_start_s=start_s if active else None,
        availability_end_s=end_s if active else None,
        initial_location_id=spec.initial_location_id if active else None,
    )
    return spec, availability


class MinimalDeterministicDecisions:
    """Test-only executor/decision seam; it implements no routing or eligibility policy."""

    def __init__(self, durations: dict[str, float]) -> None:
        self.durations = durations
        self.service_calls = 0
        self.observed_committed_locations: list[tuple[str, str]] = []

    def service_assignments(self, snapshot: DecisionSnapshot) -> Sequence[PlannedAssignment]:
        self.service_calls += 1
        unused = list(snapshot.waiting_tasks)
        proposed = []
        for vehicle in snapshot.idle_vehicles:
            self.observed_committed_locations.append(
                (vehicle.key.vehicle_id, vehicle.committed_location_id)
            )
            match = next((task for task in unused if task.fleet_id == vehicle.key.fleet_id), None)
            if match is None:
                continue
            unused.remove(match)
            duration = self.durations[match.task_id]
            end_s = snapshot.time_s + duration
            intervals = (
                (
                    StationaryInterval(
                        kind="service",
                        start_s=snapshot.time_s,
                        end_s=end_s,
                        location_id=match.steps[-1].location_id,
                        step_index=1,
                    ),
                )
                if duration > 0
                else ()
            )
            milestones = (
                TaskMilestone(
                    step_index=1,
                    arrival_s=snapshot.time_s,
                    service_start_s=snapshot.time_s,
                    departure_s=end_s,
                ),
            )
            plan = ExecutionPlan(
                execution_id=(
                    f"execution_{vehicle.key.fleet_id}_{vehicle.key.vehicle_id}_{match.task_id}"
                ),
                execution_token=vehicle.execution_generation + 1,
                vehicle=vehicle.key,
                task_id=match.task_id,
                start_s=snapshot.time_s,
                end_s=end_s,
                end_location_id=match.steps[-1].location_id,
                intervals=intervals,
                task_milestones=milestones,
            )
            proposed.append(PlannedAssignment(task=match, plan=plan))
        return tuple(proposed)

    def operational_assignments(self, snapshot: DecisionSnapshot) -> Sequence[PlannedAssignment]:
        return ()


class RecordedProgress:
    def __init__(self) -> None:
        self.updates: list[tuple[str, int, int | None]] = []

    def update(self, *, phase: str, completed: int, total: int | None) -> None:
        self.updates.append((phase, completed, total))


class CancelAtCheck:
    def __init__(self, check: int) -> None:
        self.check = check
        self.calls = 0

    def is_cancelled(self) -> bool:
        self.calls += 1
        return self.calls >= self.check


def _event_projection(result: object) -> list[dict[str, object]]:
    return [
        {
            "time_s": event.time_s,
            "phase_order": event.phase_order,
            "event_type": event.event_type,
            "vehicle_id": event.vehicle_id,
            "task_id": event.task_id,
        }
        for event in result.lifecycle_events  # type: ignore[attr-defined]
    ]


def test_simultaneous_phases_are_shuffle_stable_and_match_recorded_order() -> None:
    old_spec, old_availability = _vehicle("old", start_s=0.0, end_s=5.0)
    new_spec, new_availability = _vehicle("new", start_s=5.0, end_s=20.0)
    tasks = (
        _task("old_task", release_s=0.0),
        _task("new_task", release_s=5.0),
    )

    first = run_event_kernel(
        replication_id="replication_001",
        clock=_clock(),
        tasks=tasks,
        vehicle_specs=(old_spec, new_spec),
        availability=(old_availability, new_availability),
        decisions=MinimalDeterministicDecisions({"old_task": 5.0, "new_task": 0.0}),
        progress_frequency_events=1,
    )
    shuffled = run_event_kernel(
        replication_id="replication_001",
        clock=_clock(),
        tasks=tuple(reversed(tasks)),
        vehicle_specs=(new_spec, old_spec),
        availability=(new_availability, old_availability),
        decisions=MinimalDeterministicDecisions({"old_task": 5.0, "new_task": 0.0}),
        progress_frequency_events=1,
    )

    assert first == shuffled
    assert _event_projection(first) == json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert [item.status for item in first.task_outcomes] == [
        TaskLifecycleStatus.COMPLETED,
        TaskLifecycleStatus.COMPLETED,
    ]
    assignment_at_shift_end = [
        event
        for event in first.lifecycle_events
        if event.time_s == 5.0 and event.event_type == "service_assignment"
    ]
    assert [event.vehicle_id for event in assignment_at_shift_end] == ["new"]
    old = next(item for item in first.vehicle_outcomes if item.vehicle.vehicle_id == "old")
    assert old.state.status == VehicleStatus.OFF_SHIFT


def test_zero_duration_service_closure_terminates_at_one_timestamp() -> None:
    spec, availability = _vehicle("vehicle", start_s=0.0, end_s=10.0)
    decisions = MinimalDeterministicDecisions({f"task_{index}": 0.0 for index in range(3)})
    result = run_event_kernel(
        replication_id="replication_001",
        clock=_clock(end_s=10.0),
        tasks=tuple(_task(f"task_{index}", release_s=0.0) for index in range(3)),
        vehicle_specs=(spec,),
        availability=(availability,),
        decisions=decisions,
        max_micro_rounds=4,
    )

    assert decisions.service_calls == 4
    assert all(item.status == TaskLifecycleStatus.COMPLETED for item in result.task_outcomes)
    assert all(item.realized_end_s == 0.0 for item in result.execution_outcomes)
    assert [
        event.event_type
        for event in result.lifecycle_events
        if event.time_s == 0.0 and event.event_type.endswith("completion")
    ] == ["execution_completion"] * 3


def test_hard_shift_invalidates_stale_completion_without_task_mutation() -> None:
    spec, availability = _vehicle("vehicle", start_s=0.0, end_s=5.0)
    result = run_event_kernel(
        replication_id="replication_001",
        clock=_clock(end_s=20.0),
        tasks=(_task("long_task", release_s=0.0),),
        vehicle_specs=(spec,),
        availability=(availability,),
        decisions=MinimalDeterministicDecisions({"long_task": 10.0}),
    )

    assert result.task_outcomes[0].status == TaskLifecycleStatus.INTERRUPTED_SHIFT
    assert result.task_outcomes[0].terminal_time_s == 5.0
    assert result.execution_outcomes[0].status == ExecutionLifecycleStatus.INTERRUPTED_SHIFT
    assert result.execution_outcomes[0].realized_end_s == 5.0
    assert result.vehicle_outcomes[0].state.status == VehicleStatus.OFF_SHIFT
    assert result.vehicle_outcomes[0].state.current_execution_id is None
    assert result.vehicle_outcomes[0].state.execution_generation == 2
    stale = [
        event for event in result.lifecycle_events if event.event_type == "stale_completion_ignored"
    ]
    assert len(stale) == 1 and stale[0].time_s == 10.0


def test_horizon_completes_exact_boundary_and_censors_only_active_work() -> None:
    exact_spec, exact_availability = _vehicle("a_exact", start_s=0.0, end_s=20.0)
    long_spec, long_availability = _vehicle("b_long", start_s=0.0, end_s=20.0)
    result = run_event_kernel(
        replication_id="replication_001",
        clock=_clock(end_s=10.0),
        tasks=(
            _task("a_exact", release_s=0.0),
            _task("b_long", release_s=0.0),
            _task("c_waiting", release_s=9.0),
            _task("d_at_horizon", release_s=10.0),
        ),
        vehicle_specs=(exact_spec, long_spec),
        availability=(exact_availability, long_availability),
        decisions=MinimalDeterministicDecisions({"a_exact": 10.0, "b_long": 15.0}),
    )

    statuses = {item.task_id: item.status for item in result.task_outcomes}
    assert statuses == {
        "a_exact": TaskLifecycleStatus.COMPLETED,
        "b_long": TaskLifecycleStatus.CENSORED_HORIZON,
        "c_waiting": TaskLifecycleStatus.UNSERVED_HORIZON,
        "d_at_horizon": TaskLifecycleStatus.UNRELEASED,
    }
    assert all(item.realized_end_s <= 10.0 for item in result.execution_outcomes)
    assert not any(event.time_s > 10.0 for event in result.lifecycle_events)
    exact = next(item for item in result.execution_outcomes if item.task_id == "a_exact")
    assert exact.status == ExecutionLifecycleStatus.COMPLETED
    assert exact.realized_end_s == 10.0


def test_inactive_empty_demand_and_progress_hooks_are_valid() -> None:
    spec, availability = _vehicle("inactive", start_s=0.0, end_s=20.0, active=False)
    progress = RecordedProgress()
    result = run_event_kernel(
        replication_id="replication_001",
        clock=_clock(),
        tasks=(),
        vehicle_specs=(spec,),
        availability=(availability,),
        decisions=MinimalDeterministicDecisions({}),
        progress=progress,
    )

    assert result.lifecycle_events == ()
    assert result.execution_outcomes == ()
    assert result.processed_heap_events == 0
    assert result.vehicle_outcomes[0].state.status == VehicleStatus.INACTIVE
    assert [update[0] for update in progress.updates] == [
        "simulation.initializing",
        "simulation.finalizing",
        "simulation.completed",
    ]


def test_carry_in_task_is_waiting_without_a_past_release_event() -> None:
    spec, availability = _vehicle("vehicle", start_s=0.0, end_s=10.0)
    clock = _clock(end_s=10.0).model_copy(
        update={"simulation_start_s": 5.0, "observation_start_s": 5.0}
    )
    result = run_event_kernel(
        replication_id="replication_001",
        clock=clock,
        tasks=(_task("carry_in", release_s=1.0),),
        vehicle_specs=(spec,),
        availability=(availability,),
        decisions=MinimalDeterministicDecisions({"carry_in": 0.0}),
    )

    assert result.task_outcomes[0].status == TaskLifecycleStatus.COMPLETED
    assert result.task_outcomes[0].assigned_at_s == 5.0
    assert all(event.time_s >= 5.0 for event in result.lifecycle_events)
    assert not any(event.event_type == "task_release" for event in result.lifecycle_events)


def test_cancellation_is_checked_between_event_batches() -> None:
    spec, availability = _vehicle("vehicle", start_s=0.0, end_s=20.0)
    progress = RecordedProgress()
    with pytest.raises(KernelCancelled) as caught:
        run_event_kernel(
            replication_id="replication_001",
            clock=_clock(),
            tasks=(_task("later", release_s=5.0),),
            vehicle_specs=(spec,),
            availability=(availability,),
            decisions=MinimalDeterministicDecisions({"later": 1.0}),
            cancellation=CancelAtCheck(4),
            progress=progress,
            progress_frequency_events=1,
        )

    assert caught.value.time_s == 5.0
    assert caught.value.processed_heap_events == 1
    assert progress.updates[:2] == [
        ("simulation.initializing", 0, None),
        ("simulation.events", 1, None),
    ]


def test_kernel_rejects_catalog_and_resource_limit_defects() -> None:
    spec, availability = _vehicle("vehicle", start_s=0.0, end_s=20.0)
    other_spec, _ = _vehicle("other", start_s=0.0, end_s=20.0)
    with pytest.raises(ValueError, match="cover the vehicle catalog exactly"):
        run_event_kernel(
            replication_id="replication_001",
            clock=_clock(),
            tasks=(),
            vehicle_specs=(spec, other_spec),
            availability=(availability,),
            decisions=MinimalDeterministicDecisions({}),
        )
    with pytest.raises(RuntimeError, match="event limit exceeded"):
        run_event_kernel(
            replication_id="replication_001",
            clock=_clock(),
            tasks=(_task("task", release_s=0.0),),
            vehicle_specs=(spec,),
            availability=(availability,),
            decisions=MinimalDeterministicDecisions({"task": 1.0}),
            max_events=1,
        )
