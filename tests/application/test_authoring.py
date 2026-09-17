from datetime import date
import pandas as pd
import pytest

from mobile_sensing.application.civil_time import civil_clock, local_instant
from mobile_sensing.application.project_models import (
    ProjectConfig,
    FleetEditor,
    DemandEditor,
    SupplyEditor,
    DispatchEditor,
    SimulationEditor,
)
from mobile_sensing.application.project_resolution import resolve_project
from mobile_sensing.application.services import HeadlessApplication
from mobile_sensing.application.environment_editor import build_environment
from mobile_sensing.application.studio_models import EnvironmentEditor
from mobile_sensing.application.task_authoring import import_tasks, sparse_od_input
from mobile_sensing.application.spatial_support import prepare_support
from mobile_sensing.application.duty_authoring import infer_duties
from mobile_sensing.contracts import ExecutionOptions, Task, TaskStep
from mobile_sensing.environment import PreparedEnvironmentReader
from mobile_sensing.simulation.rng import SemanticRngStreams
from tests.support.environment_fixtures import write_analytic_sources, RecordedProgress
from tests.environment.test_environment_editor import register, Cancellation
from tests.simulation.test_event_kernel import (
    _clock,
    _task,
    _vehicle,
    MinimalDeterministicDecisions,
)
from mobile_sensing.simulation import run_event_kernel


def environment(tmp_path):
    sources = write_analytic_sources(tmp_path / "sources")
    root = tmp_path / "artifacts"
    config = EnvironmentEditor(
        boundary_input=register(root, sources.resources["analytic_boundary"].path, "boundary"),
        network_input=register(root, sources.resources["analytic_roads"].path, "network"),
        working_crs="EPSG:2056",
        grid_size_m=10,
        speed_kph=7.2,
    )
    result = build_environment(
        root,
        config,
        application=HeadlessApplication(root),
        cancellation=Cancellation(),
        progress=RecordedProgress(),
    )
    return root, config, result


def test_fixed_expected_catalog_and_online_kernel_equivalence(tmp_path):
    root, editor, prepared = environment(tmp_path)
    app = HeadlessApplication(root)
    config = ProjectConfig(
        environment=editor,
        prepared_environment=prepared,
        simulation=SimulationEditor(replications=2),
        fleets=(
            FleetEditor(
                fleet_id="postal",
                name="Postal",
                demand=DemandEditor(task_volume=8),
                supply=SupplyEditor(fleet_size=2),
            ),
        ),
    )
    offline = resolve_project(
        root, config, cancellation=Cancellation(), progress=RecordedProgress()
    )
    changed = config.model_copy(
        update={
            "fleets": (
                config.fleets[0].model_copy(
                    update={
                        "demand": config.fleets[0].demand.model_copy(
                            update={"generation_timing": "online"}
                        )
                    }
                ),
            )
        }
    )
    online = resolve_project(
        root, changed, cancellation=Cancellation(), progress=RecordedProgress()
    )
    for left, right in zip(offline.validated.replications, online.validated.replications):
        assert (
            len(left.tasks) == 8
            and left.tasks == right.tasks
            and left.availability == right.availability
        )
        assert left.input_hash == right.input_hash
    assert offline.validated.catalog == online.validated.catalog
    options = ExecutionOptions(memory_limit_bytes=512 * 1024**2)
    left = app.run_simulation(offline.validated, options)
    right = app.run_simulation(online.validated, options)
    for a, b in zip(left.results, right.results):
        assert a.task_outcomes == b.task_outcomes
        assert a.execution_outcomes == b.execution_outcomes
        assert a.lifecycle_events == b.lifecycle_events
    expected = config.model_copy(
        update={
            "simulation": config.simulation.model_copy(update={"replications": 8}),
            "fleets": (
                config.fleets[0].model_copy(
                    update={
                        "demand": config.fleets[0].demand.model_copy(
                            update={"volume_mode": "expected"}
                        )
                    }
                ),
            ),
        }
    )
    sampled = resolve_project(
        root, expected, cancellation=Cancellation(), progress=RecordedProgress()
    )
    assert len({len(item.tasks) for item in sampled.validated.replications}) > 1


def test_online_injects_same_time_group_before_matching_and_is_lazy():
    tasks = [_task("a", release_s=2.0), _task("b", release_s=2.0), _task("c", release_s=9.0)]
    spec, availability = _vehicle("v", start_s=0.0, end_s=20.0)
    pulls = []

    def stream():
        for task in tasks:
            pulls.append(task.task_id)
            yield task

    offline = run_event_kernel(
        replication_id="replication_001",
        clock=_clock(),
        tasks=tasks,
        vehicle_specs=[spec],
        availability=[availability],
        decisions=MinimalDeterministicDecisions({"a": 1, "b": 1, "c": 1}),
    )
    online = run_event_kernel(
        replication_id="replication_001",
        clock=_clock(),
        tasks=(),
        online_tasks=stream(),
        vehicle_specs=[spec],
        availability=[availability],
        decisions=MinimalDeterministicDecisions({"a": 1, "b": 1, "c": 1}),
    )
    assert pulls == ["a", "b", "c"]
    assert online.task_outcomes == offline.task_outcomes
    assert online.lifecycle_events == offline.lifecycle_events


def test_civil_day_dst_and_ambiguous_clock():
    assert (
        civil_clock(SimulationEditor(service_date=date(2026, 3, 29)), "Europe/Zurich").end_s
        == 23 * 3600
    )
    assert (
        civil_clock(SimulationEditor(service_date=date(2026, 10, 25)), "Europe/Zurich").end_s
        == 25 * 3600
    )
    with pytest.raises(ValueError, match="Nonexistent"):
        local_instant(date(2026, 3, 29), "02:30", "Europe/Zurich")
    with pytest.raises(ValueError, match="Ambiguous"):
        local_instant(date(2026, 10, 25), "02:30", "Europe/Zurich")


def test_count_quantity_and_multistop_error_rows(tmp_path):
    root, editor, prepared = environment(tmp_path)
    env = PreparedEnvironmentReader(root).read(prepared.artifact)
    support = prepare_support(root, env, prepared.features)
    cell = next(iter(support.cell_locations))
    path = tmp_path / "counts.csv"
    pd.DataFrame({"cell_id": [cell], "value": [3]}).to_csv(path, index=False)
    input_id = register(root, path, "demand")
    fleet = FleetEditor(
        fleet_id="f",
        name="F",
        demand=DemandEditor(
            source="import", input_id=input_id, content="counts", count_semantics="task_count"
        ),
    )
    clock = civil_clock(SimulationEditor(), "UTC")
    rng = SemanticRngStreams(1, namespace="test")
    tasks, _ = import_tasks(root, fleet, support, clock, rng, "r")
    assert len(tasks) == 3
    quantities = fleet.model_copy(
        update={
            "demand": fleet.demand.model_copy(update={"count_semantics": "service_quantity"}),
            "supply": SupplyEditor(capacity_mode="consumable", capacity=4),
        }
    )
    tasks, _ = import_tasks(root, quantities, support, clock, rng, "r")
    assert len(tasks) == 1 and tasks[0].required_capacity == 3
    pd.DataFrame(
        {
            "task_id": ["x", "x", "x"],
            "cell_id": [cell, "unknown", cell],
            "step_index": [1, 2, 3],
            "scheduled_time": ["08:00", "08:01", "08:02"],
        }
    ).to_csv(path, index=False)
    bad_id = register(root, path, "demand")
    ordered = fleet.model_copy(
        update={"demand": DemandEditor(source="import", input_id=bad_id, task_type="ordered")}
    )
    with pytest.raises(ValueError, match="source row 3.*No partial"):
        import_tasks(root, ordered, support, clock, rng, "r")


def test_duty_fixed_count_failure_is_not_infeasibility_certificate(tmp_path):
    root, _, prepared = environment(tmp_path)
    support = prepare_support(
        root, PreparedEnvironmentReader(root).read(prepared.artifact), prepared.features
    )
    location = next(iter(support.locations))
    fleet = FleetEditor(
        fleet_id="b",
        name="Bus",
        demand=DemandEditor(source="import", task_type="ordered"),
        supply=SupplyEditor(source="timetable", fleet_size=None),
        dispatch=DispatchEditor(mode="scheduled"),
    )
    tasks = tuple(
        Task(
            task_id=f"t{i}",
            fleet_id="b",
            release_s=0.0,
            steps=(
                TaskStep(
                    step_index=1,
                    location_id=location,
                    scheduled_time_s=0.0,
                    service_duration_s=10.0,
                ),
            ),
        )
        for i in range(2)
    )
    with pytest.raises(ValueError, match="not a proof of mathematical infeasibility"):
        infer_duties(fleet, tasks, support, fixed_size=1)
    _, specs, plan, _ = infer_duties(fleet, tasks, support)
    assert len(specs) == 2 and len(plan.rows) == 2


def test_civil_gtfs_adjacent_dates_and_carry_in_use_common_executor(tmp_path):
    import zipfile
    from types import SimpleNamespace
    import geopandas as gpd
    from shapely.geometry import box
    from mobile_sensing.application.duty_authoring import civil_gtfs
    from mobile_sensing.application.spatial_support import SpatialSupport
    from mobile_sensing.application.supply_authoring import realize_supply
    from mobile_sensing.contracts import FleetConfig
    from mobile_sensing.simulation.dispatch import SimulationDecisionAdapter
    from tests.datasets.test_gtfs import _feed, _resolver

    feed = _feed(tmp_path / "feed")
    trips = pd.read_csv(feed / "trips.csv", dtype=str, keep_default_na=False)
    trips = trips.loc[trips.trip_id != "gone"]
    trips = pd.concat(
        [
            trips,
            pd.DataFrame(
                [{"route_id": "001", "service_id": "weekday", "trip_id": "night", "block_id": ""}]
            ),
        ]
    )
    trips.to_csv(feed / "trips.csv", index=False)
    stops = pd.read_csv(feed / "stop_times.csv", dtype=str, keep_default_na=False)
    stops = pd.concat(
        [
            stops,
            pd.DataFrame(
                [
                    {
                        "trip_id": "night",
                        "arrival_time": "23:59:55",
                        "departure_time": "23:59:55",
                        "stop_id": "A",
                        "stop_sequence": "1",
                    },
                    {
                        "trip_id": "night",
                        "arrival_time": "24:00:05",
                        "departure_time": "24:00:10",
                        "stop_id": "B",
                        "stop_sequence": "2",
                    },
                ]
            ),
        ]
    )
    stops.to_csv(feed / "stop_times.csv", index=False)
    archive = tmp_path / "feed.zip"
    with zipfile.ZipFile(archive, "w") as target:
        for path in feed.iterdir():
            target.write(path, path.name)
    root = tmp_path / "artifacts"
    input_id = register(root, archive, "gtfs")
    resolver = _resolver()
    support = SpatialSupport(
        SimpleNamespace(
            reference=resolver.environment,
            snapping=resolver.snapper,
            routing=resolver.routing,
            boundary=gpd.GeoDataFrame(geometry=[box(-1, -1, 30, 1)], crs=2056),
        ),
        {},
        {},
        {},
        [],
    )
    fleet = FleetEditor(
        fleet_id="bus",
        name="Bus",
        routing_profile="bus",
        demand=DemandEditor(
            source="import",
            task_type="ordered",
            template="gtfs",
            input_id=input_id,
            route_ids=("001",),
        ),
        supply=SupplyEditor(
            source="timetable", fleet_size=None, operating_start=None, operating_end=None
        ),
        dispatch=DispatchEditor(mode="scheduled"),
    )
    clock = civil_clock(SimulationEditor(), "Europe/Zurich")
    tasks, report, _ = civil_gtfs(root, fleet, support, clock, Cancellation())
    nights = [
        task
        for task in tasks
        if any(ref.endswith(":trip:night") for ref in task.source_record_refs)
    ]
    assert len(nights) == 2 and len({task.task_id for task in nights}) == 2
    assert sorted(task.release_s for task in nights) == [-5, 86395]
    tasks, specs, plan, _ = infer_duties(fleet, tasks, support)
    clock = clock.model_copy(
        update={"simulation_start_s": min(spec.availability_start_s for spec in specs)}
    )
    rng = SemanticRngStreams(1, namespace="test")
    availability = realize_supply(fleet, specs, clock, rng, "replication_001")
    core = FleetConfig.model_validate_json(
        __import__("json").dumps(
            {
                "fleet_id": "bus",
                "label": "Bus",
                "demand": {
                    "source": "gtfs",
                    "structure": "ordered",
                    "adapter": "demand.gtfs_reconstruction@1",
                    "parameters": {"reconstruction_id": "test"},
                },
                "supply": {
                    "source": "gtfs_duties",
                    "reconstruction_id": "test",
                    "capacity": {"mode": "none"},
                    "idle_policy": {"policy": "stationary"},
                },
                "dispatch": {
                    "policy": "predefined",
                    "assignment_plan_ref": plan.assignment_plan_id,
                },
                "routing": {"profile_id": "bus"},
            }
        )
    )
    decisions = SimulationDecisionAdapter(
        fleets=(core,),
        tasks=tasks,
        vehicle_specs=specs,
        locations=support.locations,
        routing=resolver.routing,
        assignment_plans={plan.assignment_plan_id: plan},
        replication_id="replication_001",
        simulation_end_s=clock.end_s,
        availability=availability,
        rng=rng,
    )
    result = run_event_kernel(
        replication_id="replication_001",
        clock=clock,
        tasks=tasks,
        vehicle_specs=specs,
        availability=availability,
        decisions=decisions,
    )
    carry = next(task for task in nights if task.release_s < 0)
    outcome = next(row for row in result.task_outcomes if row.task_id == carry.task_id)
    assert outcome.status.value == "completed" and outcome.completion_s == 10
    movements = [
        interval
        for execution in result.execution_outcomes
        if execution.task_id == carry.task_id
        for interval in execution.realized_intervals
        if interval.kind == "movement"
    ]
    assert any(interval.start_s < 0 < interval.end_s for interval in movements)


def test_independent_od_conditions_on_distinct_reachable_nodes(tmp_path):
    root, editor, prepared = environment(tmp_path)
    fleet = FleetEditor(
        fleet_id="ride",
        name="Ride-hailing",
        demand=DemandEditor(task_type="od", task_volume=30, pickup_seconds=30, service_seconds=30),
        supply=SupplyEditor(fleet_size=3, capacity_mode="occupancy", capacity=1),
    )
    result = resolve_project(
        root,
        ProjectConfig(
            environment=editor,
            prepared_environment=prepared,
            fleets=(fleet,),
            simulation=SimulationEditor(replications=1),
        ),
        cancellation=Cancellation(),
        progress=RecordedProgress(),
    )
    for task in result.validated.replications[0].tasks:
        origin, destination = [
            result.validated.locations[step.location_id].node_id for step in task.steps
        ]
        assert origin != destination
        assert result.validated.environment.routing.route("default", origin, destination).reachable
        assert [step.service_duration_s for step in task.steps] == [30, 30]
        assert [step.quantity_delta for step in task.steps] == [-1, 1]


def test_version_three_fleet_resolution_crosses_http_and_worker(tmp_path):
    from fastapi.testclient import TestClient
    from mobile_sensing.api import create_app
    from mobile_sensing.jobs import JobStore, LocalCoordinator

    root, editor, prepared = environment(tmp_path)
    config = ProjectConfig(
        environment=editor,
        prepared_environment=prepared,
        fleets=(FleetEditor(fleet_id="f", name="F", demand=DemandEditor(task_volume=3)),),
        simulation=SimulationEditor(replications=1),
    )
    client = TestClient(create_app(root))
    response = client.post(
        "/api/v1/workbench/resolve", json={"config": config.model_dump(mode="json")}
    )
    assert response.status_code == 202, response.text
    with LocalCoordinator(root, max_workers=1) as coordinator:
        coordinator.run_once()
    job = JobStore(root).get_job(response.json()["job_id"])
    assert job.status == "completed", job.error_message
    result = client.get(f"/api/v1/workbench/resolutions/{job.resource_id}")
    assert result.status_code == 200, result.text
    assert result.json()["validation_level"] == "configuration"
    assert result.json()["artifact"] is None
    assert result.json()["task_counts"] == {}
    assert result.json()["vehicle_counts"] == {"f": 10}


@pytest.mark.parametrize("role", ["feature", "weight", "population"])
def test_sparse_od_accepts_unified_feature_and_legacy_aliases(tmp_path, role):
    from mobile_sensing.application.spatial_support import SpatialSupport

    root = tmp_path / "artifacts"
    source = tmp_path / "od.csv"
    pd.DataFrame([{"origin_cell_id": "a", "destination_cell_id": "b", "weight": 3.5}]).to_csv(
        source, index=False
    )
    identifier = register(root, source, role)
    support = SpatialSupport(None, {}, {"a": "location_a", "b": "location_b"}, {}, [])
    assert sparse_od_input(root, identifier, support) == (("location_a", "location_b", 3.5),)
