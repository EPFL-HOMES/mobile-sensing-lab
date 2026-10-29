import pytest

from mobile_sensing.contracts import (
    ExecutionPlan,
    MovementInterval,
    StationaryInterval,
    Task,
    TaskMilestone,
    TaskStep,
    VehicleKey,
)
from mobile_sensing.simulation.executor import materialize_execution


def execution(*, fractional=False, overshoot=0.0):
    task = Task(
        task_id="task",
        fleet_id="fleet",
        release_s=0.0,
        steps=(TaskStep(step_index=1, location_id="destination"),),
    )
    plan = ExecutionPlan(
        execution_id="execution",
        execution_token=1,
        vehicle=VehicleKey(fleet_id="fleet", vehicle_id="vehicle"),
        task_id=task.task_id,
        start_s=0.0,
        end_s=6.0,
        end_location_id="destination",
        intervals=(
            MovementInterval(
                kind="movement",
                start_s=0.0,
                end_s=4.0,
                movement_kind="service_pickup",
                edge_id="edge",
                edge_start_fraction=0.25 if fractional else 0.0,
                edge_end_fraction=0.75 if fractional else 1.0,
            ),
            StationaryInterval(
                kind="service",
                start_s=4.0,
                end_s=6.0 + overshoot,
                location_id="destination",
                step_index=1,
            ),
        ),
        task_milestones=(
            TaskMilestone(
                step_index=1,
                arrival_s=4.0,
                service_start_s=4.0,
                departure_s=6.0,
            ),
        ),
    )
    return task, plan


@pytest.mark.parametrize("fractional", [False, True])
@pytest.mark.parametrize("cutoff", [0.0, 2.0, 4.0, 5.0, 6.0])
def test_completion_and_cutoffs_keep_exact_edge_and_location(fractional, cutoff):
    task, plan = execution(fractional=fractional)
    result = materialize_execution(plan, task, start_location_id="origin", cutoff_s=cutoff)
    assert sum(row.end_s - row.start_s for row in result.intervals) == cutoff
    if cutoff == 0.0:
        assert result.position.location_id == "origin"
    elif cutoff < 4.0:
        assert result.position.edge_id == "edge"
        assert result.position.edge_fraction == 0.5
    else:
        assert result.position.location_id == "destination"
    if cutoff == 6.0:
        assert result.intervals == plan.intervals
        if not fractional:
            assert result.intervals is plan.intervals


def test_complete_plan_still_clips_tolerance_sized_interval_overshoot():
    task, plan = execution(overshoot=1e-10)
    result = materialize_execution(plan, task, start_location_id="origin", cutoff_s=6.0)
    assert result.intervals[-1].end_s == 6.0
    assert plan.intervals[-1].end_s > 6.0
