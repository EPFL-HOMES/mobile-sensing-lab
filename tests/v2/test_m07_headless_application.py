from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from mobile_sensing.application import (
    FleetRuntimeResource,
    HeadlessApplication,
    ScenarioResourceBundle,
)
from mobile_sensing.cli import main
from mobile_sensing.contracts import ExecutionOptions, ExposureConfig, ScenarioConfig
from mobile_sensing.datasets import DemandImportMapping, VehicleImportMapping
from mobile_sensing.environment import PreparedEnvironmentReader
from mobile_sensing.exposure import ExposureArtifactReader
from mobile_sensing.simulation import LocationWeight, OdWeight, SimulationArtifactReader
from tests.v2.environment_fixtures import build_config, provider_request, write_analytic_sources


def _prepared(tmp_path: Path):
    artifact_root = tmp_path / "artifacts"
    application = HeadlessApplication(artifact_root)
    reference = application.prepare_environment(
        write_analytic_sources(tmp_path / "sources"),
        provider_request(),
        build_config(),
    )
    return application, PreparedEnvironmentReader(artifact_root).read(reference)


def _upload_inputs(application: HeadlessApplication, environment, tmp_path: Path):
    nodes = environment.nodes.sort_values("node_id", kind="stable")
    node_ids = next(
        (str(row.u_node_id), str(row.v_node_id))
        for row in environment.road_edges.sort_values("edge_id", kind="stable").itertuples()
        if row.u_node_id != row.v_node_id
        and environment.routing.route(
            "constant_2mps", str(row.u_node_id), str(row.v_node_id)
        ).reachable
        and environment.routing.route(
            "constant_2mps", str(row.v_node_id), str(row.u_node_id)
        ).reachable
    )
    node_by_id = {str(row.node_id): row for row in nodes.itertuples()}
    left, right = (node_by_id[node_id] for node_id in node_ids)

    demand_path = tmp_path / "scheduled_ordered.csv"
    pd.DataFrame(
        {
            "task": ["scheduled_1", "scheduled_1"],
            "release": [0.0, 0.0],
            "step": [1, 2],
            "scheduled": [4.0, 14.0],
            "x": [left.geometry.x, right.geometry.x],
            "y": [left.geometry.y, right.geometry.y],
            "service": [1.0, 1.0],
        }
    ).to_csv(demand_path, index=False)
    demand = application.normalize_demand(
        application.register_upload(demand_path, provenance="M07 analytic scheduled demand"),
        DemandImportMapping.model_validate(
            {
                "schema_version": "2.0",
                "adapter": "demand.upload_ordered@1",
                "structure": "ordered",
                "data_semantics": "observed_tasks",
                "fleet_id": "uploaded_scheduled",
                "columns": {
                    "task_id": "task",
                    "release_time": "release",
                    "step_index": "step",
                    "scheduled_time": "scheduled",
                    "x": "x",
                    "y": "y",
                    "service_duration": "service",
                },
                "location_representation": "coordinates",
                "source_crs": "EPSG:2056",
                "time": {"kind": "elapsed", "unit": "seconds"},
                "duration_unit": "seconds",
                "quantity_mode": "none",
                "error_policy": "strict",
            }
        ),
        environment,
    )

    supply_path = tmp_path / "vehicles.csv"
    pd.DataFrame(
        {
            "vehicle": ["uploaded_vehicle"],
            "start": [0.0],
            "end": [30.0],
            "x": [left.geometry.x],
            "y": [left.geometry.y],
        }
    ).to_csv(supply_path, index=False)
    supply = application.normalize_supply(
        application.register_upload(supply_path, provenance="M07 analytic physical supply"),
        VehicleImportMapping.model_validate(
            {
                "schema_version": "2.0",
                "adapter": "supply.upload_vehicle_catalog@1",
                "fleet_id": "uploaded_scheduled",
                "columns": {
                    "vehicle_id": "vehicle",
                    "availability_start": "start",
                    "availability_end": "end",
                    "initial_x": "x",
                    "initial_y": "y",
                },
                "location_representation": "coordinates",
                "source_crs": "EPSG:2056",
                "time": {"kind": "elapsed", "unit": "seconds"},
                "capacity_mode": "none",
            }
        ),
        environment,
    )
    locations = {item.location_id: item for item in (*demand.locations, *supply.locations)}
    return demand, supply, tuple(locations.values())


def _bundle(environment, demand, supply, locations) -> ScenarioResourceBundle:
    origin = supply.vehicles[0].initial_location_id
    destination = demand.tasks[0].steps[-1].location_id
    location_weights_id = "analytic_location_weights"
    od_weights_id = "analytic_od_weights"
    scenario = ScenarioConfig.model_validate(
        {
            "schema_version": "2.0",
            "environment_id": environment.reference.artifact_id,
            "clock": {
                "origin_utc": datetime(2026, 1, 1, tzinfo=timezone.utc),
                "display_timezone": "UTC",
                "simulation_start_s": 0.0,
                "observation_start_s": 0.0,
                "end_s": 30.0,
            },
            "replications": 2,
            "master_seed": 73,
            "joint_scenario_model": {"kind": "independent_conditional_environment"},
            "fleets": (
                {
                    "fleet_id": "generated_location",
                    "label": "Generated location",
                    "demand": {
                        "source": "generator",
                        "structure": "location",
                        "adapter": "demand.poisson_piecewise_constant@1",
                        "generation_timing": "offline",
                        "parameters": {
                            "intervals": (
                                {
                                    "start_s": 0.0,
                                    "end_s": 30.0,
                                    "rate_tasks_per_s": 1.0,
                                },
                            ),
                            "location_weights_ref": location_weights_id,
                            "service_duration_s": 1.0,
                        },
                    },
                    "supply": {
                        "source": "generated",
                        "catalog_size": 1,
                        "catalog_id_namespace": "generated_location",
                        "availability": {
                            "kind": "simultaneous",
                            "start_s": 0.0,
                            "end_s": 30.0,
                        },
                        "initial_locations_ref": "location_initials",
                        "capacity": {"mode": "none"},
                        "idle_policy": {"policy": "stationary"},
                    },
                    "dispatch": {"policy": "nearest_matching"},
                    "routing": {"profile_id": "constant_2mps"},
                },
                {
                    "fleet_id": "generated_od",
                    "label": "Generated OD",
                    "demand": {
                        "source": "generator",
                        "structure": "od",
                        "adapter": "demand.poisson_piecewise_constant@1",
                        "generation_timing": "offline",
                        "parameters": {
                            "intervals": (
                                {
                                    "start_s": 0.0,
                                    "end_s": 30.0,
                                    "rate_tasks_per_s": 1.0,
                                },
                            ),
                            "od_weights_ref": od_weights_id,
                            "service_duration_s": 1.0,
                        },
                    },
                    "supply": {
                        "source": "generated",
                        "catalog_size": 1,
                        "catalog_id_namespace": "generated_od",
                        "availability": {
                            "kind": "simultaneous",
                            "start_s": 0.0,
                            "end_s": 30.0,
                        },
                        "initial_locations_ref": "od_initials",
                        "capacity": {"mode": "none"},
                        "idle_policy": {"policy": "stationary"},
                    },
                    "dispatch": {"policy": "nearest_matching"},
                    "routing": {"profile_id": "constant_2mps"},
                },
                {
                    "fleet_id": "uploaded_scheduled",
                    "label": "Uploaded scheduled ordered task",
                    "demand": {
                        "source": "upload",
                        "structure": "ordered",
                        "adapter": "demand.upload_ordered@1",
                        "parameters": {
                            "dataset_id": demand.reference.artifact_id,
                            "mapping_id": demand.mapping_id,
                            "error_policy": "strict",
                        },
                    },
                    "supply": {
                        "source": "upload",
                        "catalog_ref": supply.reference.artifact_id,
                        "availability_ref": supply.reference.artifact_id,
                        "capacity": {"mode": "none"},
                        "idle_policy": {"policy": "stationary"},
                    },
                    "dispatch": {"policy": "nearest_matching"},
                    "routing": {"profile_id": "constant_2mps"},
                },
            ),
        }
    )
    return ScenarioResourceBundle(
        scenario=scenario,
        fleets=(
            FleetRuntimeResource(
                fleet_id="generated_location",
                weight_source_id=location_weights_id,
                initial_locations_source_id="location_initials",
                locations=locations,
                generated_initial_location_ids=(origin,),
                location_weights=(LocationWeight(location_id=destination, weight=1.0),),
            ),
            FleetRuntimeResource(
                fleet_id="generated_od",
                weight_source_id=od_weights_id,
                initial_locations_source_id="od_initials",
                locations=locations,
                generated_initial_location_ids=(origin,),
                od_weights=(
                    OdWeight(
                        origin_location_id=origin,
                        destination_location_id=destination,
                        weight=1.0,
                    ),
                ),
            ),
            FleetRuntimeResource(
                fleet_id="uploaded_scheduled",
                demand_artifact=demand.reference,
                supply_artifact=supply.reference,
                locations=locations,
                tasks=demand.tasks,
                vehicle_specs=supply.vehicles,
            ),
        ),
    )


def test_headless_uploaded_generated_scheduled_location_od_and_seed_replay(
    tmp_path: Path,
) -> None:
    application, environment = _prepared(tmp_path)
    demand, supply, locations = _upload_inputs(application, environment, tmp_path)
    bundle = _bundle(environment, demand, supply, locations)
    validated = application.validate_scenario(environment, bundle)
    assert len(validated.vehicle_specs) == 3
    assert len(validated.replications) == 2
    assert "synthetic_demand" in validated.assumptions
    assert all(
        {task.fleet_id for task in replication.tasks}
        == {"generated_location", "generated_od", "uploaded_scheduled"}
        for replication in validated.replications
    )
    with pytest.raises(ValueError, match="expected task count"):
        HeadlessApplication(
            application.artifact_root, max_tasks_per_replication=1
        ).validate_scenario(environment, bundle)

    options = ExecutionOptions(
        workers=1, memory_limit_bytes=16 * 1024 * 1024, progress_frequency_events=2
    )
    first = application.run_simulation(validated, options)
    second = application.run_simulation(application.validate_scenario(environment, bundle), options)
    assert first.reference == second.reference
    reader = SimulationArtifactReader(application.artifact_root, first.reference)
    assert len(reader.replication_ids) == 2
    assert len(reader.vehicle_keys) == 3

    exposure = application.allocate_exposure(
        environment,
        first.reference,
        ExposureConfig(
            schema_version="2.0",
            simulation_id=first.reference.artifact_id,
            sensing_geometry_id=environment.metadata.sensing_hash,
            bin_edges_s=(0.0, 10.0, 20.0, 30.0),
            active_movement_kinds=("service_inter_step", "service_pickup"),
        ),
    )
    axes = ExposureArtifactReader(application.artifact_root).axes(exposure)
    assert len(axes["replication_ids"]) * len(axes["vehicle_keys"]) == 6
    assert len(axes["time_bin_ids"]) == 3

    parallel = application.run_simulation(
        application.validate_scenario(environment, bundle),
        ExecutionOptions(workers=2, memory_limit_bytes=16 * 1024 * 1024),
    )
    assert parallel.reference == first.reference
    assert parallel.results == first.results

    relabeled = bundle.model_copy(
        update={
            "scenario": bundle.scenario.model_copy(
                update={
                    "fleets": tuple(
                        fleet.model_copy(update={"label": f"New {fleet.label}"})
                        for fleet in bundle.scenario.fleets
                    )
                }
            )
        }
    )
    assert application.validate_scenario(environment, relabeled).scenario_hash == (
        validated.scenario_hash
    )

    shuffled_payload = bundle.model_dump(mode="json")
    for resource in shuffled_payload["fleets"]:
        for field in ("locations", "tasks", "vehicle_specs", "location_weights", "od_weights"):
            resource[field] = list(reversed(resource[field]))
        if resource["assignment_plan"] is not None:
            resource["assignment_plan"]["rows"] = list(
                reversed(resource["assignment_plan"]["rows"])
            )
    shuffled = ScenarioResourceBundle.model_validate_json(json.dumps(shuffled_payload))
    shuffled_validated = application.validate_scenario(environment, shuffled)
    assert shuffled_validated.scenario_hash == validated.scenario_hash
    assert [item.replication_id for item in shuffled_validated.replications] == [
        item.replication_id for item in validated.replications
    ]

    altered_payload = bundle.model_dump(mode="json")
    uploaded = next(
        resource
        for resource in altered_payload["fleets"]
        if resource["fleet_id"] == "uploaded_scheduled"
    )
    uploaded["tasks"][0]["steps"][0]["service_duration_s"] += 7.0
    with pytest.raises(ValueError, match="tasks differ from the bound demand artifact"):
        application.validate_scenario(
            environment, ScenarioResourceBundle.model_validate_json(json.dumps(altered_payload))
        )

    altered_vehicle_payload = bundle.model_dump(mode="json")
    uploaded = next(
        resource
        for resource in altered_vehicle_payload["fleets"]
        if resource["fleet_id"] == "uploaded_scheduled"
    )
    uploaded["vehicle_specs"][0]["availability_end_s"] -= 1.0
    with pytest.raises(ValueError, match="vehicles differ from the bound supply artifact"):
        application.validate_scenario(
            environment,
            ScenarioResourceBundle.model_validate_json(json.dumps(altered_vehicle_payload)),
        )

    forged_location_payload = bundle.model_dump(mode="json")
    uploaded = next(
        resource
        for resource in forged_location_payload["fleets"]
        if resource["fleet_id"] == "uploaded_scheduled"
    )
    forged_location = dict(uploaded["locations"][0])
    forged_location["location_id"] = "unverified_inline_location"
    uploaded["locations"].append(forged_location)
    with pytest.raises(ValueError, match="inline runtime locations are not owned"):
        application.validate_scenario(
            environment,
            ScenarioResourceBundle.model_validate_json(json.dumps(forged_location_payload)),
        )


def test_headless_cli_validates_and_runs_without_http_or_browser(tmp_path: Path, capsys) -> None:
    application, environment = _prepared(tmp_path)
    demand, supply, locations = _upload_inputs(application, environment, tmp_path)
    bundle = _bundle(environment, demand, supply, locations)
    environment_path = tmp_path / "environment.json"
    resources_path = tmp_path / "resources.json"
    options_path = tmp_path / "options.json"
    environment_path.write_text(environment.reference.model_dump_json(), encoding="utf-8")
    sparse_resources = bundle.model_dump(mode="json")
    uploaded = next(
        resource
        for resource in sparse_resources["fleets"]
        if resource["fleet_id"] == "uploaded_scheduled"
    )
    uploaded["locations"] = []
    uploaded["tasks"] = []
    uploaded["vehicle_specs"] = []
    resources_path.write_text(
        ScenarioResourceBundle.model_validate_json(json.dumps(sparse_resources)).model_dump_json(),
        encoding="utf-8",
    )
    options_path.write_text(
        ExecutionOptions(workers=1, memory_limit_bytes=16 * 1024 * 1024).model_dump_json(),
        encoding="utf-8",
    )
    common = [
        "--artifact-root",
        str(application.artifact_root),
        "--environment",
        str(environment_path),
        "--resources",
        str(resources_path),
    ]
    assert main(["validate-scenario", *common]) == 0
    validation_output = json.loads(capsys.readouterr().out)
    assert validation_output["replication_count"] == 2
    assert main(["run-simulation", *common, "--options", str(options_path)]) == 0
    simulation_output = json.loads(capsys.readouterr().out)
    assert simulation_output["replication_count"] == 2
