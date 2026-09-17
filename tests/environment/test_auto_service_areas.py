import json

import pytest

from mobile_sensing.application.migration import migrate_project
from mobile_sensing.application.project_models import (
    DemandEditor,
    FleetEditor,
    ProjectConfig,
    SimulationEditor,
    SupplyEditor,
)
from mobile_sensing.application.project_resolution import resolve_project
from mobile_sensing.application.service_areas import automatic_service_areas
from tests.support.environment_fixtures import RecordedProgress
from tests.environment.test_environment_editor import Cancellation
from tests.application.test_authoring import environment


def test_demand_balanced_areas_are_deterministic_and_cover_catalog():
    rows = [
        ("a", 0.0, 0.0, 0.40),
        ("b", 1.0, 0.0, 0.10),
        ("c", 10.0, 0.0, 0.25),
        ("d", 11.0, 0.0, 0.25),
        ("zero", 20.0, 0.0, 0.0),
    ]
    vehicles = [f"v{i}" for i in range(5)]
    first = automatic_service_areas(
        fleet_id="postal", area_count=2, location_rows=rows, vehicle_ids=vehicles
    )
    second = automatic_service_areas(
        fleet_id="postal",
        area_count=2,
        location_rows=reversed(rows),
        vehicle_ids=reversed(vehicles),
    )
    assert first == second
    assert set(first.location_area_ids) == {row[0] for row in rows}
    assert set(first.vehicle_area_ids) == set(vehicles)
    assert all(len(value) == 1 for value in first.vehicle_area_ids.values())
    assert sum(row["physical_vehicle_count"] for row in first.report["areas"]) == 5
    assert sum(
        row["expected_origin_demand_share"] for row in first.report["areas"]
    ) == pytest.approx(1)


def test_auto_area_resolution_freezes_memberships_before_replications(tmp_path):
    root, editor, prepared = environment(tmp_path)
    fleet = FleetEditor(
        fleet_id="courier",
        name="Courier",
        demand=DemandEditor(task_volume=8, spatial_feature="uniform"),
        supply=SupplyEditor(
            fleet_size=4,
            service_area_mode="auto",
            auto_service_area_count=2,
        ),
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
    specs = resolved.validated.vehicle_specs
    assert len(specs) == 4
    assert len({area for spec in specs for area in spec.assigned_area_ids}) == 2
    assert all(len(spec.assigned_area_ids) == 1 for spec in specs)
    for replication in resolved.validated.replications:
        assert all(
            resolved.validated.location_area_ids[task.steps[0].location_id]
            for task in replication.tasks
        )
    report = resolved.reports["courier"]["service_areas"]
    assert report["area_count"] == 2
    runtime = resolved.validated.bundle.scenario.fleets[0].supply
    assert runtime.area_definitions_ref and runtime.area_assignments_ref


def test_service_area_contract_rejects_ambiguous_or_unsupported_inputs():
    with pytest.raises(ValueError, match="at least one vehicle"):
        SupplyEditor(fleet_size=2, service_area_mode="auto", auto_service_area_count=3)
    with pytest.raises(ValueError, match="require geometry"):
        SupplyEditor(service_area_mode="uploaded")
    with pytest.raises(ValueError, match="generated demand"):
        FleetEditor(
            fleet_id="f",
            name="F",
            demand=DemandEditor(source="import", input_id="input"),
            supply=SupplyEditor(service_area_mode="auto", auto_service_area_count=2),
        )


def test_schema_33_migrates_to_explicit_disabled_service_areas():
    payload = ProjectConfig(
        schema_version="3.3", fleets=(FleetEditor(fleet_id="f", name="F"),)
    ).model_dump(mode="json")
    for fleet in payload["fleets"]:
        fleet["supply"].pop("service_area_mode", None)
        fleet["supply"].pop("auto_service_area_count", None)
    migrated = migrate_project(json.loads(json.dumps(payload)), "revision-old")
    assert migrated.config.schema_version == "3.4"
    assert migrated.config.fleets[0].supply.service_area_mode == "none"
    assert migrated.notices
