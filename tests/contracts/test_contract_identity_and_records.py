from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from mobile_sensing.contracts import (
    ArtifactManifest,
    CatalogIdentity,
    Event,
    ExecutionPlan,
    JointReplicationIdentity,
    PartitionManifest,
    ReplicationSetIdentity,
    RouteResult,
    RuntimeProvenance,
    ScientificIdentity,
    TableManifest,
    TimeAxis,
    VehicleKey,
    canonical_json_bytes,
    scientific_hash,
    stable_id,
)


def test_explicit_ids_and_generated_ids_are_stable() -> None:
    key = VehicleKey(fleet_id="fleet", vehicle_id="001")
    assert key.vehicle_id == "001"
    assert stable_id("task", {"b": 2, "a": 1}) == stable_id("task", {"a": 1, "b": 2})
    assert stable_id("task", ["a", "b"]) != stable_id("task", ["b", "a"])

    with pytest.raises(ValidationError):
        VehicleKey(fleet_id="fleet", vehicle_id=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="non-finite"):
        canonical_json_bytes({"value": float("inf")})


def test_catalog_and_joint_replication_identity_are_content_checked() -> None:
    metadata_hash = "1" * 64
    keys = (
        VehicleKey(fleet_id="a", vehicle_id="001"),
        VehicleKey(fleet_id="b", vehicle_id="002"),
    )
    catalog_hash = scientific_hash(
        {
            "vehicle_keys": [["a", "001"], ["b", "002"]],
            "physical_metadata_hash": metadata_hash,
        }
    )
    catalog = CatalogIdentity(
        catalog_id="catalog_001",
        vehicle_keys=keys,
        physical_metadata_hash=metadata_hash,
        catalog_hash=catalog_hash,
    )
    assert catalog.catalog_hash == catalog_hash

    replications = tuple(
        JointReplicationIdentity(
            replication_id=f"replication_{index:03d}",
            joint_scenario_id=f"joint_{index:03d}",
            scenario_realization_hash=str(index + 2) * 64,
            catalog_hash=catalog_hash,
        )
        for index in range(2)
    )
    replication_set_hash = scientific_hash([item.model_dump(mode="json") for item in replications])
    identity = ReplicationSetIdentity(
        replications=replications,
        replication_set_hash=replication_set_hash,
    )
    assert len(identity.replications) == 2

    with pytest.raises(ValidationError, match="catalog_hash"):
        CatalogIdentity(
            catalog_id="catalog_001",
            vehicle_keys=keys,
            physical_metadata_hash=metadata_hash,
            catalog_hash="f" * 64,
        )


def test_manifest_fingerprint_excludes_runtime_and_timestamp() -> None:
    resolved_config = {"scenario_id": "scenario_001"}
    identity = ScientificIdentity(
        schema_version="2.0",
        artifact_kind="simulation",
        resolved_config=resolved_config,
        resolved_config_hash=scientific_hash(resolved_config),
        dependency_hashes={},
        algorithm_versions={"kernel": "event_kernel@1"},
        random_stream_version="semantic-seed@1",
        catalog_hash="2" * 64,
        replication_set_hash="3" * 64,
    )
    fingerprint = scientific_hash(identity)

    def manifest(created_at: datetime, worker_count: int) -> ArtifactManifest:
        table = TableManifest(
            name="replication_status",
            relative_path="tables/replication_status",
            schema_hash="4" * 64,
            row_count=0,
            partition_axes=(),
            partitions=(PartitionManifest(partition_values={}, row_count=0, file_sha256=None),),
        )
        return ArtifactManifest(
            schema_version="2.0",
            artifact_id=f"simulation_attempt_{worker_count}",
            artifact_kind="simulation",
            content_fingerprint=fingerprint,
            scientific_identity=identity,
            dependencies=(),
            expected_table_names=("replication_status",),
            tables=(table,),
            created_at_utc=created_at,
            runtime=RuntimeProvenance(
                python_version="3.12.14",
                package_version="0.1.0",
                worker_count=worker_count,
                platform="test",
            ),
        )

    first = manifest(datetime(2026, 1, 1, tzinfo=timezone.utc), 1)
    second = manifest(datetime(2026, 2, 1, tzinfo=timezone.utc), 4)
    assert first.content_fingerprint == second.content_fingerprint
    assert canonical_json_bytes(first) != canonical_json_bytes(second)

    with pytest.raises(ValidationError, match="explicit complete partitions"):
        TableManifest(
            name="exposure",
            relative_path="tables/exposure",
            schema_hash="4" * 64,
            row_count=0,
            partition_axes=(),
            partitions=(),
        )


def test_clock_axis_event_route_and_execution_boundaries() -> None:
    axis = TimeAxis.model_validate_json(
        """{
          "time_axis_id":"hours",
          "observation_start_s":0.0,
          "end_s":20.0,
          "bins":[
            {"time_bin_id":"t0","canonical_index":0,"start_s":0.0,"end_s":10.0},
            {"time_bin_id":"t1","canonical_index":1,"start_s":10.0,"end_s":20.0}
          ]
        }"""
    )
    assert axis.bins[1].start_s == 10.0

    event = Event.model_validate_json(
        """{
          "time_s":5.0,"phase":"vehicle_exit","stable_tie_key":["fleet","vehicle"],
          "sequence":0,"event_type":"vehicle_exit","fleet_id":"fleet","vehicle_id":"vehicle",
          "task_id":null,"execution_generation":1
        }"""
    )
    assert event.heap_key[:2] == (5.0, 2)
    with pytest.raises(ValidationError, match="event phase"):
        Event.model_validate_json(
            event.model_dump_json().replace('"vehicle_exit"', '"vehicle_entry"', 1)
        )

    same_node = RouteResult.model_validate_json(
        """{
          "reachable":true,"edges":[],"total_duration_s":0.0,"total_distance_m":0.0,
          "network_hash":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
          "profile_hash":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
          "reason":null
        }"""
    )
    assert same_node.reachable and not same_node.edges

    zero_duration = ExecutionPlan.model_validate_json(
        """{
          "execution_id":"execution_001","execution_token":0,
          "vehicle":{"fleet_id":"fleet","vehicle_id":"001"},"task_id":"task_001",
          "start_s":4.0,"end_s":4.0,"end_location_id":"node_001",
          "intervals":[],"task_milestones":[],"capacity_milestones":[]
        }"""
    )
    assert zero_duration.start_s == zero_duration.end_s
