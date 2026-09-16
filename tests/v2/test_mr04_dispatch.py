import itertools
import json

import pytest

from mobile_sensing.application.project_models import (
    FleetEditor,
    DemandEditor,
    SupplyEditor,
    DispatchEditor,
    ProjectConfig,
    SimulationEditor,
)
from mobile_sensing.application.project_resolution import resolve_project
from mobile_sensing.application.services import HeadlessApplication
from mobile_sensing.contracts import FleetConfig, ExecutionOptions
from mobile_sensing.simulation import SimulationDecisionAdapter, run_event_kernel
from mobile_sensing.simulation.dispatch import EligibilityEngine
from mobile_sensing.simulation.one_shot import plan_one_shot, PlanningFailure, PlanningTimeout
from mobile_sensing.simulation.rng import SemanticRngStreams
from tests.v2.test_m05b_execution_dispatch import (
    MatrixRouting,
    _location,
    _vehicle,
    _availability,
    _task,
    _clock,
    _fleet,
)
from tests.v2.test_mr03_authoring import environment
from tests.v2.test_mr02_environment_editor import Cancellation
from tests.v2.environment_fixtures import RecordedProgress


def core(fleet, dispatch):
    config = fleet.model_dump(mode="json")
    config["dispatch"] = dispatch
    return FleetConfig.model_validate_json(json.dumps(config))


def execute(fleet, specs, tasks, routing, locations, plan=None, end=100.0):
    availability = tuple(_availability(spec) for spec in specs)
    decisions = SimulationDecisionAdapter(
        fleets=(fleet,),
        tasks=tasks,
        vehicle_specs=specs,
        locations=locations,
        routing=routing,
        assignment_plans={} if plan is None else {plan.assignment_plan_id: plan},
        replication_id="rep",
        simulation_end_s=end,
        availability=availability,
        rng=SemanticRngStreams(1, namespace="test"),
    )
    return run_event_kernel(
        replication_id="rep",
        clock=_clock(end),
        tasks=tasks,
        vehicle_specs=specs,
        availability=availability,
        decisions=decisions,
    )


def test_batch_boundary_completion_release_and_no_early_matching():
    routing = MatrixRouting({})
    locations = {"L0": _location("L0")}
    specs = (_vehicle("f", "v"),)
    tasks = (
        _task("f", "a", ("L0",), services=(10.0,)),
        _task("f", "b", ("L0",), release_s=1.0, services=(1.0,)),
        _task("f", "c", ("L0",), release_s=10.0, services=(1.0,)),
    )
    batch = core(
        _fleet("f"),
        {"policy": "batch_nearest_matching", "batch_interval_s": 10.0, "max_pickup_time_s": 15.0},
    )
    result = execute(batch, specs, tasks, routing, locations, end=31.0)
    rows = {row.task_id: row for row in result.task_outcomes}
    assert [rows[key].assigned_at_s for key in ("a", "b", "c")] == [0, 10, 20]
    assert [rows[key].completion_s for key in ("a", "b", "c")] == [10, 11, 21]
    sequential = execute(_fleet("f", max_pickup=15.0), specs, tasks, routing, locations, end=31.0)
    assert {row.task_id: row.assigned_at_s for row in sequential.task_outcomes} == {
        "a": 0,
        "b": 10,
        "c": 11,
    }


def test_batch_pickup_time_and_half_open_vehicle_exit():
    routes = MatrixRouting({("L0", "L1"): 6.0})
    locations = {name: _location(name) for name in ("L0", "L1")}
    tasks = (_task("f", "a", ("L1",), release_s=1.0),)
    batch = core(
        _fleet("f"),
        {"policy": "batch_nearest_matching", "batch_interval_s": 10.0, "max_pickup_time_s": 5.0},
    )
    result = execute(batch, (_vehicle("f", "v"),), tasks, routes, locations, end=31.0)
    assert result.task_outcomes[0].assigned_at_s is None
    relaxed = core(
        batch,
        {"policy": "batch_nearest_matching", "batch_interval_s": 10.0, "max_pickup_time_s": 6.0},
    )
    result = execute(relaxed, (_vehicle("f", "v", end_s=10.0),), tasks, routes, locations, end=31.0)
    assert result.task_outcomes[0].assigned_at_s is None


def fixture_vrp(capacity=2.0):
    positions = {"L0": (0, 0), "L1": (1, 0), "L2": (3, 0), "L3": (0, 2)}
    durations = {
        (a, b): float(abs(x - u) + abs(y - v))
        for a, (x, y) in positions.items()
        for b, (u, v) in positions.items()
        if a != b
    }
    routing = MatrixRouting(durations)
    fleet = FleetEditor(
        fleet_id="f",
        name="Postal",
        routing_profile="profile",
        demand=DemandEditor(release_mode="at_start"),
        supply=SupplyEditor(
            fleet_size=2, capacity_mode="consumable", capacity=capacity, initial_location="depot"
        ),
        dispatch=DispatchEditor(mode="one_shot", solution_limit=100, allow_replenishment=False),
    )
    specs = tuple(
        _vehicle("f", f"v{i}", capacity_mode="consumable", capacity=capacity, depot="L0")
        for i in range(2)
    )
    tasks = tuple(
        _task("f", f"t{i}", (f"L{i}",), services=(1.0,), required=1.0, deltas=(-1.0,))
        for i in range(1, 4)
    )
    return fleet, specs, tasks, routing, {name: _location(name) for name in positions}


def solve(fleet, specs, tasks, routing, locations):
    return plan_one_shot(
        fleet,
        tasks,
        specs,
        tuple(_availability(spec) for spec in specs),
        locations,
        routing,
        "rep",
        cancellation=Cancellation(),
    )


def test_one_shot_enumerated_optimum_common_executor_and_determinism():
    fleet, specs, tasks, routing, locations = fixture_vrp()
    plan = solve(fleet, specs, tasks, routing, locations)
    objectives = []
    for sequence in itertools.permutations(["L1", "L2", "L3"]):
        for split in (1, 2):
            objectives.append(
                sum(
                    routing.durations.get((a, b), 0)
                    for group in (sequence[:split], sequence[split:])
                    for a, b in zip(("L0", *group), (*group, "L0"))
                )
            )
    assert plan.report["objective_travel_seconds"] == min(objectives)
    other = solve(fleet, specs, tasks[::-1], routing, locations)
    assert plan.assignment == other.assignment
    for value in (plan.report, other.report):
        value.pop("search_seconds")
    assert plan.report == other.report
    config = core(
        _fleet("f", capacity_mode="consumable"),
        {"policy": "one_shot", "assignment_plan_ref": plan.assignment.assignment_plan_id},
    )
    result = execute(config, specs, plan.tasks, routing, locations, plan.assignment)
    assert all(row.status.value == "completed" for row in result.task_outcomes)
    assert len([task for task in plan.tasks if task.kind == "depot_return"]) == 2
    assert all(
        task.capacity_reset_at_end is False for task in plan.tasks if task.kind == "depot_return"
    )


def test_one_shot_release_work_capacity_and_query_limits():
    fleet, specs, tasks, routing, locations = fixture_vrp()
    tasks = (tasks[0].model_copy(update={"release_s": 90.0}),)
    plan = solve(fleet, specs, tasks, routing, locations)
    config = core(
        _fleet("f", capacity_mode="consumable"),
        {"policy": "one_shot", "assignment_plan_ref": plan.assignment.assignment_plan_id},
    )
    result = execute(config, specs, plan.tasks, routing, locations, plan.assignment)
    service = next(row for row in result.task_outcomes if row.task_id == tasks[0].task_id)
    assert service.assigned_at_s == 90.0 and service.completion_s == 92.0
    assert all(row.status.value == "completed" for row in result.task_outcomes)
    too_late = (tasks[0].model_copy(update={"release_s": 99.0}),)
    with pytest.raises(PlanningFailure, match="infeasible|no feasible"):
        solve(fleet, specs, too_late, routing, locations)
    fleet, specs, tasks, routing, locations = fixture_vrp(capacity=1.0)
    with pytest.raises(PlanningFailure, match="capacity"):
        solve(fleet, specs, tasks, routing, locations)
    constrained = fleet.model_copy(
        update={"dispatch": fleet.dispatch.model_copy(update={"max_cost_pairs": 2})}
    )
    with pytest.raises(PlanningFailure, match="cost entries"):
        solve(constrained, specs, tasks, routing, locations)


def test_four_modes_reach_authoring_runtime(tmp_path):
    root, editor, prepared = environment(tmp_path)
    # The fixture contains one-way disconnected nodes. Tasks at a declared depot
    # isolate the authoring/runtime contract from reachability already tested above.
    from mobile_sensing.application.spatial_support import prepare_support
    from mobile_sensing.environment import PreparedEnvironmentReader
    import pandas as pd
    from tests.v2.test_mr02_environment_editor import register

    support = prepare_support(
        root, PreparedEnvironmentReader(root).read(prepared.artifact), prepared.features
    )
    cell = next(iter(support.cell_locations))
    path = tmp_path / "tasks.csv"
    pd.DataFrame(
        {"task_id": ["a", "b"], "cell_id": [cell, cell], "release_time": ["08:00", "08:00"]}
    ).to_csv(path, index=False)
    input_id = register(root, path, "demand")
    fleet = FleetEditor(
        fleet_id="f",
        name="Postal",
        demand=DemandEditor(source="import", input_id=input_id),
        supply=SupplyEditor(
            fleet_size=2,
            initial_location="depot",
            depot_cell_id=cell,
            capacity_mode="consumable",
            capacity=2,
        ),
        dispatch=DispatchEditor(mode="one_shot"),
    )
    config = ProjectConfig(
        environment=editor,
        prepared_environment=prepared,
        fleets=(fleet,),
        simulation=SimulationEditor(replications=2),
    )
    resolved = resolve_project(
        root, config, cancellation=Cancellation(), progress=RecordedProgress()
    )
    run = HeadlessApplication(root).run_simulation(
        resolved.validated, ExecutionOptions(memory_limit_bytes=512 * 1024**2)
    )
    assert all(
        row.status.value == "completed" for result in run.results for row in result.task_outcomes
    )
    again = resolve_project(root, config, cancellation=Cancellation(), progress=RecordedProgress())
    assert resolved.validated.reference == again.validated.reference
    for mode in ("batch", "sequential"):
        changed = config.model_copy(
            update={"fleets": (fleet.model_copy(update={"dispatch": DispatchEditor(mode=mode)}),)}
        )
        resolved = resolve_project(
            root, changed, cancellation=Cancellation(), progress=RecordedProgress()
        )
        run = HeadlessApplication(root).run_simulation(
            resolved.validated, ExecutionOptions(memory_limit_bytes=512 * 1024**2)
        )
        assert all(
            row.status.value == "completed"
            for result in run.results
            for row in result.task_outcomes
        )


def test_search_timeout_never_accepts_a_wall_time_dependent_incumbent(monkeypatch):
    import mobile_sensing.simulation.one_shot as module

    fleet, specs, tasks, routing, locations = fixture_vrp()
    times = iter((0.0, 121.0))
    monkeypatch.setattr(module.time, "perf_counter", lambda: next(times))
    with pytest.raises(PlanningTimeout, match="no timing-dependent incumbent"):
        solve(fleet, specs, tasks, routing, locations)


def test_one_shot_respects_explicit_area_assignments():
    fleet, specs, tasks, routing, locations = fixture_vrp()
    fleet = fleet.model_copy(
        update={
            "supply": fleet.supply.model_copy(
                update={"service_area_input": "areas", "area_assignment_input": "assignments"}
            )
        }
    )
    with pytest.raises(PlanningFailure, match="No vehicle is assigned"):
        solve(fleet, specs, tasks, routing, locations)
    specs = tuple(spec.model_copy(update={"assigned_area_ids": ("a",)}) for spec in specs)
    plan = plan_one_shot(
        fleet,
        tasks,
        specs,
        tuple(_availability(spec) for spec in specs),
        locations,
        routing,
        "rep",
        location_area_ids={name: ("a",) for name in locations},
    )
    assert len([task for task in plan.tasks if task.kind == "service"]) == 3


def test_service_areas_do_not_block_shared_depot_reload_tasks():
    fleet, specs, tasks, routing, locations = fixture_vrp()
    fleet = fleet.model_copy(
        update={"supply": fleet.supply.model_copy(update={"area_assignments_ref": "assignments"})}
    )
    spec = specs[0].model_copy(update={"assigned_area_ids": ("area-a",)})
    reload_task = tasks[0].model_copy(
        update={
            "kind": "depot_return",
            "source_policy": "dispatch.one_shot.reload@1",
            "capacity_reset_at_end": True,
        }
    )
    engine = EligibilityEngine(
        fleet_configs={fleet.fleet_id: fleet},
        vehicle_specs={(spec.key.fleet_id, spec.key.vehicle_id): spec},
        locations=locations,
        location_area_ids={},
        routing=routing,
    )
    assert engine._area_allowed(spec, reload_task)
