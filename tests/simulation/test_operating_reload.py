"""Independent time accounting and multi-trip capacity acceptance cases."""

import math

import pandas as pd
import pytest

from mobile_sensing.application.project_models import ProjectConfig
from mobile_sensing.contracts import VehicleKey, SparseExposureRow
from mobile_sensing.exposure.models import AllocationResult, VehicleExposureDiagnostic
from mobile_sensing.exposure.operating import add_stationary_exposure
from mobile_sensing.exposure.storage import build_time_axis
from mobile_sensing.exposure.allocation import ExposureAllocationError
from tests.simulation.test_dispatch_planning import (
    fixture_vrp,
    solve,
    core,
    execute,
    _fleet,
    _location,
)


def reload_fixture(capacity=1):
    fleet, specs, tasks, routing, locations = fixture_vrp(capacity)
    fleet = fleet.model_copy(
        update={
            "dispatch": fleet.dispatch.model_copy(update={"allow_replenishment": True}),
            "supply": fleet.supply.model_copy(update={"depot_min_stay_minutes": 0.1}),
        }
    )
    return fleet, specs, tasks, routing, locations


def test_multiple_trips_reset_only_after_minimum_depot_service():
    fleet, specs, tasks, routing, locations = reload_fixture()
    plan = solve(fleet, specs, tasks, routing, locations)
    # Capacity one: each of three customers needs its own depot round trip.
    oracle = sum(
        routing.durations["L0", f"L{i}"] + routing.durations[f"L{i}", "L0"] for i in range(1, 4)
    )
    assert plan.report["objective_travel_seconds"] == oracle
    assert plan.report["reload_count"] == 1
    result = execute(
        core(
            _fleet("f", capacity_mode="consumable"),
            {"policy": "one_shot", "assignment_plan_ref": plan.assignment.assignment_plan_id},
        ),
        specs,
        plan.tasks,
        routing,
        locations,
        plan.assignment,
    )
    assert all(row.status.value == "completed" for row in result.task_outcomes)
    reload_ids = {task.task_id for task in plan.tasks if task.capacity_reset_at_end}
    reloads = [out for out in result.execution_outcomes if out.task_id in reload_ids]
    assert len(reloads) == 1
    out = reloads[0]
    service = [i for i in out.realized_intervals if i.kind == "service"]
    assert sum(i.end_s - i.start_s for i in service) == 6
    assert len(out.applied_capacity_milestones) == 1
    assert out.applied_capacity_milestones[0].time_s == out.realized_end_s
    assert out.applied_capacity_milestones[0].reset_to_capacity


def test_reload_requires_time_and_bounded_visits():
    from mobile_sensing.simulation.one_shot import PlanningFailure

    fleet, specs, tasks, routing, locations = reload_fixture()
    fleet = fleet.model_copy(
        update={"supply": fleet.supply.model_copy(update={"depot_min_stay_minutes": 10})}
    )
    with pytest.raises(PlanningFailure, match="infeasible|no feasible"):
        solve(fleet, specs, tasks, routing, locations)
    too_large = (
        tasks[0].model_copy(
            update={
                "required_capacity": 2.0,
                "steps": (tasks[0].steps[0].model_copy(update={"quantity_delta": -2.0}),),
            }
        ),
    )
    with pytest.raises(PlanningFailure, match="full capacity"):
        solve(fleet, specs, too_large, routing, locations)


def test_same_location_tasks_remain_separate_and_consecutive():
    fleet, specs, tasks, routing, locations = reload_fixture(capacity=10)
    specs = specs[:1]
    tasks = tuple(tasks[0].model_copy(update={"task_id": f"t{i}"}) for i in range(3)) + (tasks[2],)
    plan = solve(fleet, specs, tasks, routing, locations)
    ids = [r.task_id for r in sorted(plan.assignment.rows, key=lambda r: r.order_index)]
    ranks = sorted(ids.index(f"t{i}") for i in range(3))
    assert ranks == list(range(min(ranks), min(ranks) + 3))
    assert len([t for t in plan.tasks if t.kind == "service"]) == 4


def test_operating_exposure_depot_boundary_outside_and_off_duty():
    axis, _ = build_time_axis((0, 10, 20, 30))
    vehicle = VehicleKey(fleet_id="f", vehicle_id="v")
    movement = AllocationResult(
        (
            SparseExposureRow(
                replication_id="r",
                vehicle=vehicle,
                cell_id="g",
                time_bin_id=axis.bins[0].time_bin_id,
                duration_s=2,
            ),
        ),
        (VehicleExposureDiagnostic("r", vehicle, 2, 2, 0, 0),),
    )
    locations = {key: _location(f"L{i}") for i, key in enumerate(["depot", "customer", "outside"])}
    # Depot and customer deliberately occupy the same cell but different nodes.
    records = [
        (0, 2, "movement", None),
        (2, 6, "service", "depot"),
        (6, 12, "service", "customer"),
        (12, 14, "wait", "outside"),
        (14, 30, "idle", "customer"),
    ]
    frame = pd.DataFrame(
        [
            dict(
                replication_id="r",
                fleet_id="f",
                vehicle_id="v",
                start_s=a,
                end_s=b,
                activity_kind=k,
                location_id=loc,
            )
            for a, b, k, loc in records
        ]
    )
    result = add_stationary_exposure(
        movement,
        frame,
        time_axis=axis,
        locations=locations,
        location_cells={"depot": "g", "customer": "g", "outside": None},
        depot_nodes={("f", "v"): locations["depot"].node_id},
        operating_fleets=("f",),
        idle_break_seconds={"f": 15},
    )
    d = result.diagnostics[0]
    assert d.active_moving_time_s == 2 and d.stationary_time_s == 8
    assert d.in_grid_duration_s == 8 and d.outside_grid_duration_s == 2
    assert d.excluded_depot_time_s == 4 and d.excluded_offduty_time_s == 16
    assert d.conservation_residual_s == 0
    assert {r.time_bin_id: r.duration_s for r in result.rows} == {
        axis.bins[0].time_bin_id: 6,
        axis.bins[1].time_bin_id: 2,
    }
    assert math.fsum(r.duration_s for r in result.rows) == 8
    overlapping = frame.copy()
    overlapping.loc[1, "start_s"] = 1
    with pytest.raises(ExposureAllocationError, match="Overlapping"):
        add_stationary_exposure(
            movement,
            overlapping,
            time_axis=axis,
            locations=locations,
            location_cells={},
            depot_nodes={},
            operating_fleets=("f",),
        )


def test_historical_project_semantics_remain_explicit():
    payload = {
        "schema_version": "3.1",
        "fleets": [{"fleet_id": "f", "name": "Old", "dispatch": {"mode": "one_shot"}}],
    }
    old = ProjectConfig.model_validate_json(__import__("json").dumps(payload))
    assert old.fleets[0].sensing_mode == "movement_duration"
    assert not old.fleets[0].dispatch.allow_replenishment
    assert old.fleets[0].supply.timetable_idle_break_minutes is None


def test_operating_pipeline_retains_cruise_locations_and_source(tmp_path):
    from tests.application.test_runs_analysis import config_fixture
    from tests.environment.test_environment_editor import Cancellation
    from tests.support.environment_fixtures import RecordedProgress
    from mobile_sensing.application.run_pipeline import run_project
    from mobile_sensing.application.run_models import RunOptions
    from mobile_sensing.simulation import SimulationArtifactReader
    from mobile_sensing.exposure import ExposureArtifactReader

    root, config = config_fixture(tmp_path)
    f = config.fleets[0]
    config = config.model_copy(
        update={
            "fleets": (
                f.model_copy(
                    update={"supply": f.supply.model_copy(update={"post_service": "random_cruise"})}
                ),
            )
        }
    )
    run = run_project(
        root,
        config,
        name="Operating cruise",
        source_revision_id=None,
        options=RunOptions(),
        cancellation=Cancellation(),
        progress=RecordedProgress(),
    )
    simulation = SimulationArtifactReader(root, run.simulation)
    locations = simulation.read_stationary_locations()
    stationary = simulation.read_activities().query("activity_kind != 'movement'")
    assert set(stationary.location_id) <= set(locations)
    artifact = ExposureArtifactReader(root)._open(run.exposure)[0]
    source = next(d for d in artifact.manifest.dependencies if d.role == "activity_locations")
    assert source.artifact_id == run.resolution.artifact_id
    status = ExposureArtifactReader(root)._open(run.exposure)[1]
    for row in status:
        assert row["sensor_active_time_s"] > 0
        assert row["sensor_active_stationary_time_s"] > 0
        assert row["sensor_active_time_s"] == pytest.approx(
            row["sensor_active_moving_time_s"] + row["sensor_active_stationary_time_s"]
        )
        assert row["sensor_active_time_s"] == pytest.approx(
            row["in_grid_duration_s"] + row["outside_grid_duration_s"]
        )
