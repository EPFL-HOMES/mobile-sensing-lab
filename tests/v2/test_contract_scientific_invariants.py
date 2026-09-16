from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import get_args, get_type_hints

import pytest
from pydantic import ValidationError

from mobile_sensing.contracts import (
    AllocationSampler,
    ArtifactManifest,
    EnvironmentArtifactRef,
    Event,
    EnvironmentBuildConfig,
    ExecutionPlan,
    ExecutionOptions,
    ExposureReader,
    GridAxis,
    PartitionAxis,
    PartitionManifest,
    ReplicationContext,
    ReplicationSetIdentity,
    RouteEdge,
    RouteResult,
    SamplingRoundRecord,
    ScenarioConfig,
    ScientificIdentity,
    SparseExposureChunk,
    SupplySource,
    TableManifest,
    Task,
    TaskStep,
    TimeAxis,
    VehicleAvailability,
    VehicleKey,
    VehicleSpec,
    canonical_json_bytes,
    scientific_hash,
)


FIXTURES = Path(__file__).parent / "fixtures" / "contracts" / "examples"


def _scenario() -> dict[str, object]:
    return json.loads((FIXTURES / "scenario_config.json").read_text(encoding="utf-8"))


def test_scientific_projection_excludes_display_and_execution_fields() -> None:
    original = _scenario()
    renamed = copy.deepcopy(original)
    renamed["fleets"][0]["label"] = "Renamed display label"  # type: ignore[index]

    first = ScenarioConfig.model_validate_json(json.dumps(original))
    second = ScenarioConfig.model_validate_json(json.dumps(renamed))
    assert canonical_json_bytes(first) != canonical_json_bytes(second)
    assert scientific_hash(first) == scientific_hash(second)

    assert scientific_hash(
        ExecutionOptions(workers=1, memory_limit_bytes=1_000_000)
    ) == scientific_hash(ExecutionOptions(workers=4, memory_limit_bytes=2_000_000))
    with pytest.raises(TypeError, match="keys must be strings"):
        canonical_json_bytes({1: "numeric", "1": "text"})

    multiple = _scenario()
    first_fleet = copy.deepcopy(multiple["fleets"][0])  # type: ignore[index]
    second_fleet = copy.deepcopy(first_fleet)
    first_fleet["fleet_id"] = "fleet_b"
    second_fleet["fleet_id"] = "fleet_a"
    multiple["fleets"] = [first_fleet, second_fleet]
    reversed_input = copy.deepcopy(multiple)
    reversed_input["fleets"].reverse()  # type: ignore[union-attr]
    assert canonical_json_bytes(
        ScenarioConfig.model_validate_json(json.dumps(multiple))
    ) == canonical_json_bytes(ScenarioConfig.model_validate_json(json.dumps(reversed_input)))

    with pytest.raises(ValidationError, match="canonically sorted"):
        GridAxis(grid_axis_id="grid", cell_ids=("cell_b", "cell_a"), working_crs="EPSG:2056")
    with pytest.raises(ValidationError, match="canonically sorted"):
        VehicleSpec(
            key=VehicleKey(fleet_id="fleet", vehicle_id="vehicle"),
            availability_start_s=0.0,
            availability_end_s=1.0,
            initial_location_id="node",
            assigned_area_ids=("area_b", "area_a"),
            identity_provenance="generated",
        )


def test_routing_source_area_and_supply_combinations_are_explicit() -> None:
    environment = json.loads(
        (FIXTURES / "environment_build_config.json").read_text(encoding="utf-8")
    )
    environment["travel_time_profiles"][0] = {
        "profile_id": "ambiguous",
        "algorithm": "static_shortest_travel_time",
        "edge_speed_field": "speed",
        "fallback_speed_mps": 8.0,
    }
    with pytest.raises(ValidationError):
        EnvironmentBuildConfig.model_validate_json(json.dumps(environment))

    scenario = _scenario()
    supply = scenario["fleets"][0]["supply"]  # type: ignore[index]
    supply["area_definitions_ref"] = "areas"
    with pytest.raises(ValidationError, match="provided together"):
        ScenarioConfig.model_validate_json(json.dumps(scenario))

    gtfs = copy.deepcopy(_scenario())
    gtfs["fleets"][0]["supply"] = {  # type: ignore[index]
        "source": "gtfs_duties",
        "reconstruction_id": "reconstruction_001",
        "capacity": {"mode": "occupancy", "unit": "passengers"},
        "area_definitions_ref": None,
        "area_assignments_ref": None,
        "idle_policy": {"policy": "stationary"},
        "depot_policy": None,
    }
    assert ScenarioConfig.model_validate_json(json.dumps(gtfs)).fleets[0].supply.capacity.mode == (
        "occupancy"
    )


def test_manifest_requires_exact_canonical_partition_coverage() -> None:
    axes = (PartitionAxis(name="replication_id", values=("r0", "r1")),)
    incomplete = (
        PartitionManifest(partition_values={"replication_id": "r0"}, row_count=0, file_sha256=None),
    )
    with pytest.raises(ValidationError, match="complete partition-axis product"):
        TableManifest(
            name="exposure",
            relative_path="tables/exposure",
            schema_hash="1" * 64,
            row_count=0,
            partition_axes=axes,
            partitions=incomplete,
        )

    complete = incomplete + (
        PartitionManifest(partition_values={"replication_id": "r1"}, row_count=0, file_sha256=None),
    )
    table = TableManifest(
        name="exposure",
        relative_path="tables/exposure",
        schema_hash="1" * 64,
        row_count=0,
        partition_axes=axes,
        partitions=complete,
    )
    manifest = json.loads((FIXTURES / "artifact_manifest.json").read_text(encoding="utf-8"))
    manifest["expected_table_names"] = ["exposure"]
    manifest["tables"] = [json.loads(table.model_dump_json())]
    parsed = ArtifactManifest.model_validate_json(json.dumps(manifest))
    assert parsed.tables[0].partitions == complete

    manifest["expected_table_names"] = ["exposure", "missing"]
    with pytest.raises(ValidationError, match="expected table inventory"):
        ArtifactManifest.model_validate_json(json.dumps(manifest))

    identity = manifest["scientific_identity"]
    identity["resolved_config_hash"] = "f" * 64
    with pytest.raises(ValidationError, match="resolved_config_hash"):
        ScientificIdentity.model_validate_json(json.dumps(identity))


def test_axis_event_route_and_plan_counterexamples_are_rejected() -> None:
    with pytest.raises(ValidationError, match="time bin IDs must be unique"):
        TimeAxis.model_validate_json(
            """{
              "time_axis_id":"axis","observation_start_s":0.0,"end_s":2.0,
              "bins":[
                {"time_bin_id":"dup","canonical_index":0,"start_s":0.0,"end_s":1.0},
                {"time_bin_id":"dup","canonical_index":1,"start_s":1.0,"end_s":2.0}
              ]
            }"""
        )

    with pytest.raises(ValidationError, match="vehicle_exit event requires references"):
        Event.model_validate_json(
            """{
              "time_s":1.0,"phase":"vehicle_exit","stable_tie_key":["event"],
              "sequence":0,"event_type":"vehicle_exit"
            }"""
        )

    route = RouteResult(
        reachable=True,
        edges=(
            RouteEdge(edge_id="a", length_m=0.1, duration_s=0.1),
            RouteEdge(edge_id="b", length_m=0.2, duration_s=0.2),
        ),
        total_duration_s=0.3,
        total_distance_m=0.3,
        network_hash="a" * 64,
        profile_hash="b" * 64,
    )
    assert route.total_duration_s == 0.3

    empty_positive = {
        "execution_id": "execution",
        "execution_token": 0,
        "vehicle": {"fleet_id": "fleet", "vehicle_id": "vehicle"},
        "task_id": "task",
        "start_s": 0.0,
        "end_s": 1.0,
        "end_location_id": "node",
        "intervals": [],
        "task_milestones": [],
        "capacity_milestones": [],
    }
    with pytest.raises(ValidationError, match="requires intervals"):
        ExecutionPlan.model_validate_json(json.dumps(empty_positive))

    gapped = copy.deepcopy(empty_positive)
    gapped["intervals"] = [
        {
            "kind": "wait",
            "start_s": 0.1,
            "end_s": 1.0,
            "location_id": "node",
            "step_index": None,
        }
    ]
    with pytest.raises(ValidationError, match="contiguous timeline"):
        ExecutionPlan.model_validate_json(json.dumps(gapped))


def test_positive_task_indices_and_availability_contract() -> None:
    task = Task(
        task_id="task",
        fleet_id="fleet",
        release_s=0.0,
        steps=(TaskStep(step_index=1, location_id="node"),),
    )
    assert task.steps[0].step_index == 1
    with pytest.raises(ValidationError, match="greater than 0"):
        TaskStep(step_index=0, location_id="node")

    inactive = VehicleAvailability(
        replication_id="r0",
        vehicle=VehicleKey(fleet_id="fleet", vehicle_id="vehicle"),
        active=False,
    )
    assert not inactive.active


def test_sampling_lineage_and_protocols_are_typed() -> None:
    replication_set_hash = "1" * 64
    seed_manifest_hash = "2" * 64
    sampling_seed = 991
    design_hash = scientific_hash(
        {
            "sampling_design": "joint_replication_uniform_vehicle",
            "sampling_seed": sampling_seed,
            "seed_manifest_hash": seed_manifest_hash,
            "replication_set_hash": replication_set_hash,
        }
    )
    round_record = SamplingRoundRecord(
        round_id=0,
        selected_joint_replication_id="r0",
        sampling_design="joint_replication_uniform_vehicle",
        sampling_seed=sampling_seed,
        seed_manifest_hash=seed_manifest_hash,
        replication_set_hash=replication_set_hash,
        sampling_design_hash=design_hash,
    )
    assert round_record.replication_set_hash == replication_set_hash
    tampered = json.loads(round_record.model_dump_json())
    tampered["sampling_design_hash"] = "f" * 64
    with pytest.raises(ValidationError, match="sampling lineage"):
        SamplingRoundRecord.model_validate_json(json.dumps(tampered))

    context_hints = get_type_hints(ReplicationContext)
    assert context_hints["environment"] is EnvironmentArtifactRef
    supply_return = get_type_hints(SupplySource.realize_availability)["return"]
    assert supply_return == tuple[VehicleAvailability, ...]
    exposure_return = get_type_hints(ExposureReader.read_sparse_chunks)["return"]
    assert get_args(get_args(exposure_return)[1]) == (SparseExposureChunk,)
    sampler_hints = get_type_hints(AllocationSampler.draw)
    assert sampler_hints["complete_replications"] is ReplicationSetIdentity
