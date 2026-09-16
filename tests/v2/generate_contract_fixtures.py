"""Regenerate checked-in M01 contract examples and JSON Schemas."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel

from mobile_sensing.contracts import (
    ArtifactManifest,
    CapabilityRegistry,
    EnvironmentBuildConfig,
    EnvironmentProviderRequest,
    Event,
    ExecutionPlan,
    ExposureConfig,
    PortfolioConfig,
    PartitionManifest,
    ProjectRevision,
    RawGeographicBundle,
    SamplingRoundRecord,
    ScenarioConfig,
    ScientificIdentity,
    TableManifest,
    TimeAxis,
    VehicleAvailability,
    canonical_json_bytes,
    scientific_hash,
)


FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "contracts"


SCENARIO = {
    "schema_version": "2.0",
    "environment_id": "env_lausanne_demo",
    "clock": {
        "origin_utc": "2026-01-14T00:00:00Z",
        "display_timezone": "Europe/Zurich",
        "simulation_start_s": 0.0,
        "observation_start_s": 3600.0,
        "end_s": 10800.0,
    },
    "fleets": [
        {
            "fleet_id": "fleet_001",
            "label": "Illustrative service fleet",
            "demand": {
                "source": "generator",
                "structure": "location",
                "adapter": "demand.poisson_piecewise_constant@1",
                "generation_timing": "offline",
                "parameters": {
                    "intervals": [{"start_s": 0.0, "end_s": 10800.0, "rate_tasks_per_s": 0.001}],
                    "location_weights_ref": "weights_population_2024",
                    "od_weights_ref": None,
                    "service_duration_s": 60.0,
                    "quantity": None,
                },
            },
            "supply": {
                "source": "generated",
                "catalog_size": 4,
                "catalog_id_namespace": "lausanne_service",
                "availability": {
                    "kind": "simultaneous",
                    "start_s": 0.0,
                    "end_s": 10800.0,
                },
                "initial_locations_ref": "initial_locations_001",
                "capacity": {"mode": "none"},
                "area_definitions_ref": None,
                "area_assignments_ref": None,
                "idle_policy": {"policy": "stationary"},
                "depot_policy": None,
            },
            "dispatch": {
                "policy": "nearest_matching",
                "implementation": "dispatch.nearest_matching@1",
                "max_pickup_time_s": 900.0,
            },
            "routing": {
                "profile_id": "road_static_30kph",
                "algorithm": "static_shortest_travel_time",
            },
        }
    ],
    "replications": 2,
    "master_seed": 7241,
    "joint_scenario_model": {"kind": "independent_conditional_environment"},
}

EXPOSURE = {
    "schema_version": "2.0",
    "simulation_id": "simulation_001",
    "sensing_geometry_id": "grid_lausanne_100m",
    "bin_edges_s": [3600.0, 7200.0, 10800.0],
    "active_movement_kinds": [
        "service_pickup",
        "service_inter_step",
        "cruise",
        "depot_return",
        "reposition",
    ],
    "measurement": "movement_duration",
}

PORTFOLIO = {
    "schema_version": "2.0",
    "exposure_id": "exposure_001",
    "utility": {
        "kind": "exponential_saturation",
        "saturation_s": 900.0,
        "weights_ref": "weights_uniform_001",
    },
    "count_enumeration": {"fleets": {"fleet_001": {"count_levels": [0, 2, 4]}}},
    "budgets": {"min_minor": 0, "max_minor": 40000, "step_minor": 10000, "include_max": True},
    "costs": {
        "unit": "CHF",
        "minor_unit_scale": 100,
        "by_fleet_minor": {"fleet_001": 10000},
    },
    "sampling_rounds": 100,
    "sampling_seed": 991,
    "sampling_design": "joint_replication_uniform_vehicle",
    "comparison_resolution": {"mean_utility": 1e-12, "std_utility": 1e-12},
}

ENVIRONMENT = {
    "schema_version": "2.0",
    "provider": "environment.local_files@1",
    "boundary": {
        "kind": "local_municipalities",
        "dataset_id": "boundary_lausanne",
        "municipality_names": ["Lausanne (suburban)", "Lausanne (urban)"],
    },
    "routing_extent_ref": "routing_extent_lausanne",
    "network_source": {
        "kind": "supplied",
        "dataset_id": "roads_encoded",
        "layer": "roads_encoded",
        "topology_policy": "conservative_repair@1",
        "endpoint_tolerance_m": 0.05,
    },
    "network_mode": "drive",
    "working_crs": "EPSG:2056",
    "travel_time_profiles": [
        {
            "profile_id": "road_static_30kph",
            "algorithm": "static_shortest_travel_time",
            "source": {"kind": "constant_speed", "speed_mps": 8.333333333333334},
        }
    ],
    "snapping": {"max_distance_m": 250.0, "tie_break": "canonical_node_id"},
    "grid": {
        "kind": "regular",
        "cell_size_m": 100.0,
        "origin_easting_m": 2530000.0,
        "origin_northing_m": 1150000.0,
    },
    "population_features": {
        "dataset_id": "population_swiss_2024",
        "year": 2024,
        "missing_policy": "zero",
    },
}

PROVIDER_REQUEST = {
    "schema_version": "2.0",
    "provider": "environment.local_files@1",
    "boundary": ENVIRONMENT["boundary"],
    "routing_extent_ref": "routing_extent_lausanne",
    "target_crs": "EPSG:2056",
    "network_mode": "drive",
    "requested_features": ["population"],
    "cache_policy": "reuse",
}

RAW_GEOGRAPHIC_BUNDLE = {
    "schema_version": "2.0",
    "bundle_id": "raw_bundle_001",
    "provider": "environment.local_files@1",
    "provider_version": "local_files@1",
    "query_hash": "2" * 64,
    "boundary": {
        "role": "boundary",
        "dataset_id": "boundary_lausanne",
        "content_hash": "3" * 64,
        "source_crs": "EPSG:4326",
    },
    "network": {
        "role": "network",
        "dataset_id": "roads_encoded",
        "content_hash": "4" * 64,
        "source_crs": "EPSG:4326",
    },
    "features": [
        {
            "role": "population",
            "dataset_id": "population_swiss_2024",
            "content_hash": "5" * 64,
            "source_crs": "EPSG:2056",
        }
    ],
    "retrieved_at_utc": "2026-09-07T00:00:00Z",
    "attribution": ["Local project data"],
    "source_coverage_limits": ["Coverage requires validation in M02."],
}

TIME_AXIS = {
    "time_axis_id": "time_axis_001",
    "observation_start_s": 3600.0,
    "end_s": 10800.0,
    "bins": [
        {
            "time_bin_id": "bin_000",
            "canonical_index": 0,
            "start_s": 3600.0,
            "end_s": 7200.0,
        },
        {
            "time_bin_id": "bin_001",
            "canonical_index": 1,
            "start_s": 7200.0,
            "end_s": 10800.0,
        },
    ],
}

VEHICLE_AVAILABILITY = {
    "replication_id": "replication_000",
    "vehicle": {"fleet_id": "fleet_001", "vehicle_id": "vehicle_001"},
    "active": True,
    "availability_start_s": 0.0,
    "availability_end_s": 10800.0,
    "initial_location_id": "node_001",
}

EVENT = {
    "time_s": 10800.0,
    "phase": "vehicle_exit",
    "stable_tie_key": ["fleet_001", "vehicle_001"],
    "sequence": 0,
    "event_type": "vehicle_exit",
    "fleet_id": "fleet_001",
    "vehicle_id": "vehicle_001",
    "task_id": None,
    "execution_generation": 1,
}

EXECUTION_PLAN = {
    "execution_id": "execution_001",
    "execution_token": 1,
    "vehicle": {"fleet_id": "fleet_001", "vehicle_id": "vehicle_001"},
    "task_id": "task_001",
    "start_s": 10.0,
    "end_s": 20.0,
    "end_location_id": "node_001",
    "intervals": [
        {
            "kind": "service",
            "start_s": 10.0,
            "end_s": 20.0,
            "location_id": "node_001",
            "step_index": 1,
        }
    ],
    "task_milestones": [
        {"step_index": 1, "arrival_s": 10.0, "service_start_s": 10.0, "departure_s": 20.0}
    ],
    "capacity_milestones": [],
}

SAMPLING_REPLICATION_SET_HASH = "7" * 64
SAMPLING_SEED_MANIFEST_HASH = "8" * 64
SAMPLING_DESIGN_HASH = scientific_hash(
    {
        "sampling_design": "joint_replication_uniform_vehicle",
        "sampling_seed": 991,
        "seed_manifest_hash": SAMPLING_SEED_MANIFEST_HASH,
        "replication_set_hash": SAMPLING_REPLICATION_SET_HASH,
    }
)
SAMPLING_ROUND = {
    "round_id": 0,
    "selected_joint_replication_id": "replication_000",
    "sampling_design": "joint_replication_uniform_vehicle",
    "sampling_seed": 991,
    "seed_manifest_hash": SAMPLING_SEED_MANIFEST_HASH,
    "replication_set_hash": SAMPLING_REPLICATION_SET_HASH,
    "sampling_design_hash": SAMPLING_DESIGN_HASH,
}

PROJECT = {
    "schema_version": "2.0",
    "project_id": "project_001",
    "revision_id": "revision_001",
    "name": "M01 contract example",
    "description": "Schema fixture only; not an implemented fleet preset.",
    "scenario": SCENARIO,
    "default_exposure": EXPOSURE,
    "default_portfolio": PORTFOLIO,
    "dataset_references": [
        "boundary_lausanne",
        "roads_encoded",
        "population_swiss_2024",
    ],
}

CAPABILITIES = {
    "registry_version": "capabilities@1",
    "max_entries": 256,
    "capabilities": [
        {
            "key": "environment.local_files@1",
            "available": False,
            "implementation_version": None,
            "parameter_schema": EnvironmentProviderRequest.model_json_schema(mode="validation"),
            "unavailable_reason": "Contract frozen in M01; implementation begins in M02.",
        },
        {
            "key": "environment.osm@1",
            "available": False,
            "implementation_version": None,
            "parameter_schema": {},
            "unavailable_reason": "Live OSM acquisition is deferred.",
        },
    ],
}


EXAMPLES: dict[str, tuple[type[BaseModel], dict[str, object]]] = {
    "capability_registry": (CapabilityRegistry, CAPABILITIES),
    "environment_build_config": (EnvironmentBuildConfig, ENVIRONMENT),
    "environment_provider_request": (EnvironmentProviderRequest, PROVIDER_REQUEST),
    "event": (Event, EVENT),
    "execution_plan": (ExecutionPlan, EXECUTION_PLAN),
    "exposure_config": (ExposureConfig, EXPOSURE),
    "portfolio_config": (PortfolioConfig, PORTFOLIO),
    "project_revision": (ProjectRevision, PROJECT),
    "raw_geographic_bundle": (RawGeographicBundle, RAW_GEOGRAPHIC_BUNDLE),
    "sampling_round": (SamplingRoundRecord, SAMPLING_ROUND),
    "scenario_config": (ScenarioConfig, SCENARIO),
    "time_axis": (TimeAxis, TIME_AXIS),
    "vehicle_availability": (VehicleAvailability, VEHICLE_AVAILABILITY),
}

SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    **{name: model for name, (model, _) in EXAMPLES.items()},
    "artifact_manifest": ArtifactManifest,
}


def _validated_json(model: type[BaseModel], value: dict[str, object]) -> bytes:
    instance = model.model_validate_json(json.dumps(value))
    return canonical_json_bytes(instance)


def build_fixture_files() -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for name, (model, example) in EXAMPLES.items():
        files[f"examples/{name}.json"] = _validated_json(model, example) + b"\n"
    for name, model in SCHEMA_MODELS.items():
        files[f"schemas/{name}.schema.json"] = (
            canonical_json_bytes(model.model_json_schema(mode="validation")) + b"\n"
        )

    resolved_config = {"fixture_kind": "dataset_contract_example"}
    identity = ScientificIdentity(
        schema_version="2.0",
        artifact_kind="dataset",
        resolved_config=resolved_config,
        resolved_config_hash=scientific_hash(resolved_config),
        dependency_hashes={},
        algorithm_versions={"adapter": "fixture@1"},
    )
    manifest = ArtifactManifest(
        schema_version="2.0",
        artifact_id="artifact_fixture_001",
        artifact_kind="dataset",
        content_fingerprint=scientific_hash(identity),
        scientific_identity=identity,
        dependencies=(),
        expected_table_names=("validation_report",),
        tables=(
            TableManifest(
                name="validation_report",
                relative_path="tables/validation_report",
                schema_hash="6" * 64,
                row_count=0,
                partition_axes=(),
                partitions=(PartitionManifest(partition_values={}, row_count=0, file_sha256=None),),
            ),
        ),
        created_at_utc=datetime(2026, 9, 7, tzinfo=timezone.utc),
        runtime={
            "python_version": "3.12.14",
            "package_version": "0.1.0",
            "worker_count": 1,
            "platform": "fixture",
        },
    )
    files["examples/artifact_manifest.json"] = canonical_json_bytes(manifest) + b"\n"
    return files


def write_fixture_files(root: Path = FIXTURE_ROOT) -> None:
    files = build_fixture_files()
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    checksums = [
        f"{hashlib.sha256(content).hexdigest()}  {relative}"
        for relative, content in sorted(files.items())
    ]
    (root / "SHA256SUMS").write_text("\n".join(checksums) + "\n", encoding="utf-8")


if __name__ == "__main__":
    write_fixture_files()
