"""Immutable coarse-partition publication of complete kernel replications."""

from __future__ import annotations

import itertools
import hashlib
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path

import pyarrow as pa

from mobile_sensing.artifacts import (
    PartitionedTableData,
    publish_partitioned_artifact,
    read_partitioned_table,
    verify_partitioned_artifact,
)
from mobile_sensing.contracts import (
    ArtifactDependency,
    ArtifactRef,
    CANONICAL_JSON_VERSION,
    CatalogIdentity,
    JointReplicationIdentity,
    PartitionAxis,
    ReplicationSetIdentity,
    ScientificIdentity,
    SeedManifest,
    VehicleKey,
    canonical_json_text,
    canonical_json_bytes,
    scientific_hash,
)
from mobile_sensing.simulation.executor import EXECUTOR_VERSION
from mobile_sensing.simulation.kernel import EVENT_KERNEL_VERSION, KernelResult
from mobile_sensing.simulation.operational import DEPOT_RETURN_VERSION, RANDOM_CRUISE_VERSION
from mobile_sensing.simulation.rng import RNG_DERIVATION_VERSION


SIMULATION_STORAGE_VERSION = "simulation-parquet@2"

MOVEMENT_SCHEMA = pa.schema(
    [
        ("replication_id", pa.string(), False),
        ("fleet_id", pa.string(), False),
        ("vehicle_id", pa.string(), False),
        ("movement_index", pa.int64(), False),
        ("execution_id", pa.string(), False),
        ("task_id", pa.string(), False),
        ("start_s", pa.float64(), False),
        ("end_s", pa.float64(), False),
        ("movement_kind", pa.string(), False),
        ("edge_id", pa.string(), False),
        ("edge_start_fraction", pa.float64(), False),
        ("edge_end_fraction", pa.float64(), False),
    ]
)

ACTIVITY_SCHEMA = pa.schema(
    [
        ("replication_id", pa.string(), False),
        ("fleet_id", pa.string(), False),
        ("vehicle_id", pa.string(), False),
        ("activity_index", pa.int64(), False),
        ("start_s", pa.float64(), False),
        ("end_s", pa.float64(), False),
        ("activity_kind", pa.string(), False),
        ("movement_kind", pa.string(), True),
        ("execution_id", pa.string(), True),
        ("task_id", pa.string(), True),
        ("location_id", pa.string(), True),
        ("edge_id", pa.string(), True),
        ("edge_start_fraction", pa.float64(), True),
        ("edge_end_fraction", pa.float64(), True),
    ]
)

TASK_OUTCOME_SCHEMA = pa.schema(
    [
        ("replication_id", pa.string(), False),
        ("fleet_id", pa.string(), False),
        ("task_id", pa.string(), False),
        ("kind", pa.string(), False),
        ("source_policy", pa.string(), True),
        ("release_s", pa.float64(), False),
        ("status", pa.string(), False),
        ("assigned_at_s", pa.float64(), True),
        ("terminal_time_s", pa.float64(), True),
        ("vehicle_id", pa.string(), True),
        ("execution_id", pa.string(), True),
        ("first_service_s", pa.float64(), True),
        ("completion_s", pa.float64(), True),
        ("assignment_wait_s", pa.float64(), True),
        ("first_service_wait_s", pa.float64(), True),
        ("lateness_s", pa.float64(), True),
        ("reason", pa.string(), True),
    ]
)

OPERATIONAL_EVENT_SCHEMA = pa.schema(
    [
        ("replication_id", pa.string(), False),
        ("event_index", pa.int64(), False),
        ("time_s", pa.float64(), False),
        ("phase_order", pa.int64(), False),
        ("record_kind", pa.string(), False),
        ("event_type", pa.string(), False),
        ("fleet_id", pa.string(), True),
        ("vehicle_id", pa.string(), True),
        ("task_id", pa.string(), True),
        ("execution_id", pa.string(), True),
        ("execution_generation", pa.int64(), True),
        ("quantity_delta", pa.float64(), True),
        ("capacity_reset", pa.bool_(), False),
    ]
)

REPLICATION_STATUS_SCHEMA = pa.schema(
    [
        ("replication_id", pa.string(), False),
        ("joint_scenario_id", pa.string(), False),
        ("scenario_realization_hash", pa.string(), False),
        ("catalog_hash", pa.string(), False),
        ("complete", pa.bool_(), False),
        ("seed_manifest_json", pa.string(), False),
        ("seed_manifest_hash", pa.string(), False),
        ("movement_row_count", pa.int64(), False),
        ("activity_row_count", pa.int64(), False),
        ("task_outcome_row_count", pa.int64(), False),
        ("operational_event_row_count", pa.int64(), False),
        ("processed_heap_events", pa.int64(), False),
        ("maximum_queue_size", pa.int64(), False),
    ]
)

VEHICLE_CATALOG_SCHEMA = pa.schema(
    [
        ("fleet_id", pa.string(), False),
        ("vehicle_id", pa.string(), False),
        ("catalog_index", pa.int64(), False),
        ("catalog_id", pa.string(), False),
        ("catalog_hash", pa.string(), False),
        ("physical_metadata_hash", pa.string(), False),
    ]
)


def _validate_result(result: KernelResult, catalog_keys: tuple[tuple[str, str], ...]) -> None:
    if not result.end_s > result.simulation_start_s:
        raise ValueError("kernel result requires a positive simulation interval")
    catalog_set = frozenset(catalog_keys)
    execution_ids = [row.execution_id for row in result.execution_outcomes]
    if len(execution_ids) != len(set(execution_ids)):
        raise ValueError("kernel execution outcome IDs must be unique")
    task_keys = [(row.fleet_id, row.task_id) for row in result.task_outcomes]
    task_set = set(task_keys)
    if len(task_keys) != len(task_set):
        raise ValueError("kernel task outcome keys must be unique")
    referenced_keys = {
        (row.vehicle.fleet_id, row.vehicle.vehicle_id)
        for row in (*result.execution_outcomes, *result.idle_activity_intervals)
    }
    if any(row.fleet_id != row.vehicle.fleet_id for row in result.execution_outcomes):
        raise ValueError("kernel execution fleet must match its physical vehicle key")
    referenced_keys.update(
        (row.fleet_id, row.vehicle_id)
        for row in result.lifecycle_events
        if row.fleet_id is not None and row.vehicle_id is not None
    )
    if not referenced_keys <= catalog_set:
        raise ValueError("kernel result references a vehicle outside the stable catalog")
    fleet_set = {key[0] for key in catalog_keys}
    if any(row.fleet_id not in fleet_set for row in result.task_outcomes):
        raise ValueError("kernel task outcome references a fleet outside the stable catalog")
    if any(
        row.vehicle_id is not None and (row.fleet_id, row.vehicle_id) not in catalog_set
        for row in result.task_outcomes
    ):
        raise ValueError("kernel task outcome references a vehicle outside the stable catalog")
    for row in result.execution_outcomes:
        if (row.fleet_id, row.task_id) in task_set:
            continue
        if (
            not any(
                interval.kind == "movement" and interval.movement_kind == "cruise"
                for interval in row.realized_intervals
            )
            or any(
                interval.kind == "movement" and interval.movement_kind != "cruise"
                for interval in row.realized_intervals
            )
            or row.applied_capacity_milestones
        ):
            raise ValueError("kernel execution outcome references an absent task outcome")


def _streaming_output_hash(rows: Mapping[str, Sequence[Mapping]], seed: SeedManifest) -> str:
    """Produce the canonical scientific hash without one giant JSON allocation."""

    digest = hashlib.sha256()
    digest.update(CANONICAL_JSON_VERSION.encode("ascii") + b"\x00")
    digest.update(b'{"seed_manifest":')
    digest.update(canonical_json_bytes(seed))
    digest.update(b',"tables":{')
    for table_name in sorted(rows):
        if table_name != min(rows):
            digest.update(b",")
        digest.update(canonical_json_bytes(table_name))
        digest.update(b":[")
        values = rows[table_name]
        for offset in range(0, len(values), 10_000):
            if offset:
                digest.update(b",")
            chunk = canonical_json_bytes(values[offset : offset + 10_000])
            digest.update(chunk[1:-1])
        digest.update(b"]")
    digest.update(b"}}")
    return digest.hexdigest()


def _replication_set(rows: Sequence[JointReplicationIdentity]) -> ReplicationSetIdentity:
    ordered = tuple(sorted(rows, key=lambda item: item.replication_id))
    return ReplicationSetIdentity(
        replications=ordered,
        replication_set_hash=scientific_hash([item.model_dump(mode="json") for item in ordered]),
    )


def _result_rows(result: KernelResult) -> dict[str, list[dict]]:
    replication_id = result.replication_id
    movements_by_vehicle: dict[tuple[str, str], list[dict]] = defaultdict(list)
    activities_by_vehicle: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for execution in result.execution_outcomes:
        key = (execution.vehicle.fleet_id, execution.vehicle.vehicle_id)
        for interval_order, interval in enumerate(execution.realized_intervals):
            common = {
                "replication_id": replication_id,
                "fleet_id": key[0],
                "vehicle_id": key[1],
                "start_s": interval.start_s,
                "end_s": interval.end_s,
                "execution_id": execution.execution_id,
                "task_id": execution.task_id,
                "_order": interval_order,
            }
            if interval.kind == "movement":
                movement = {
                    **common,
                    "movement_kind": interval.movement_kind,
                    "edge_id": interval.edge_id,
                    "edge_start_fraction": interval.edge_start_fraction,
                    "edge_end_fraction": interval.edge_end_fraction,
                }
                movements_by_vehicle[key].append(movement)
                activities_by_vehicle[key].append(
                    {
                        **common,
                        "activity_kind": "movement",
                        "movement_kind": interval.movement_kind,
                        "location_id": None,
                        "edge_id": interval.edge_id,
                        "edge_start_fraction": interval.edge_start_fraction,
                        "edge_end_fraction": interval.edge_end_fraction,
                    }
                )
            else:
                activities_by_vehicle[key].append(
                    {
                        **common,
                        "activity_kind": interval.kind,
                        "movement_kind": None,
                        "location_id": interval.location_id,
                        "edge_id": None,
                        "edge_start_fraction": None,
                        "edge_end_fraction": None,
                    }
                )
    for interval in result.idle_activity_intervals:
        key = (interval.vehicle.fleet_id, interval.vehicle.vehicle_id)
        activities_by_vehicle[key].append(
            {
                "replication_id": replication_id,
                "fleet_id": key[0],
                "vehicle_id": key[1],
                "start_s": interval.start_s,
                "end_s": interval.end_s,
                "activity_kind": "idle",
                "movement_kind": None,
                "execution_id": None,
                "task_id": None,
                "location_id": interval.location_id,
                "edge_id": None,
                "edge_start_fraction": None,
                "edge_end_fraction": None,
                "_order": -1,
            }
        )
    movements = []
    activities = []
    for key in sorted(
        {
            (outcome.vehicle.fleet_id, outcome.vehicle.vehicle_id)
            for outcome in result.vehicle_outcomes
        }
    ):
        movement_rows = sorted(
            movements_by_vehicle[key],
            key=lambda row: (row["start_s"], row["end_s"], row["execution_id"], row["_order"]),
        )
        for index, row in enumerate(movement_rows):
            row = {name: value for name, value in row.items() if name != "_order"}
            row["movement_index"] = index
            movements.append(row)
        activity_rows = sorted(
            activities_by_vehicle[key],
            key=lambda row: (
                row["start_s"],
                row["end_s"],
                row["activity_kind"],
                row["execution_id"] or "",
                row["_order"],
            ),
        )
        for index, row in enumerate(activity_rows):
            row = {name: value for name, value in row.items() if name != "_order"}
            row["activity_index"] = index
            activities.append(row)
    task_outcomes = [
        {
            "replication_id": replication_id,
            "fleet_id": row.fleet_id,
            "task_id": row.task_id,
            "kind": row.kind,
            "source_policy": row.source_policy,
            "release_s": row.release_s,
            "status": row.status.value,
            "assigned_at_s": row.assigned_at_s,
            "terminal_time_s": row.terminal_time_s,
            "vehicle_id": row.vehicle_id,
            "execution_id": row.execution_id,
            "first_service_s": row.first_service_s,
            "completion_s": row.completion_s,
            "assignment_wait_s": row.assignment_wait_s,
            "first_service_wait_s": row.first_service_wait_s,
            "lateness_s": row.lateness_s,
            "reason": row.reason,
        }
        for row in result.task_outcomes
    ]
    raw_events = [
        {
            "time_s": row.time_s,
            "phase_order": row.phase_order,
            "record_kind": "lifecycle",
            "event_type": row.event_type,
            "fleet_id": row.fleet_id,
            "vehicle_id": row.vehicle_id,
            "task_id": row.task_id,
            "execution_id": row.execution_id,
            "execution_generation": row.execution_generation,
            "quantity_delta": None,
            "capacity_reset": False,
            "_source_index": row.event_index,
        }
        for row in result.lifecycle_events
    ]
    for execution in result.execution_outcomes:
        for milestone_index, milestone in enumerate(execution.applied_capacity_milestones):
            raw_events.append(
                {
                    "time_s": milestone.time_s,
                    "phase_order": 1,
                    "record_kind": "capacity_milestone",
                    "event_type": (
                        "capacity_reset" if milestone.reset_to_capacity else "capacity_delta"
                    ),
                    "fleet_id": execution.fleet_id,
                    "vehicle_id": execution.vehicle.vehicle_id,
                    "task_id": execution.task_id,
                    "execution_id": execution.execution_id,
                    "execution_generation": execution.execution_generation,
                    "quantity_delta": milestone.quantity_delta,
                    "capacity_reset": milestone.reset_to_capacity,
                    "_source_index": milestone_index,
                }
            )
    raw_events.sort(
        key=lambda row: (
            row["time_s"],
            row["phase_order"],
            row["record_kind"],
            row["fleet_id"] or "",
            row["vehicle_id"] or "",
            row["task_id"] or "",
            row["execution_id"] or "",
            row["_source_index"],
        )
    )
    events = []
    for index, row in enumerate(raw_events):
        normalized = {name: value for name, value in row.items() if name != "_source_index"}
        normalized["replication_id"] = replication_id
        normalized["event_index"] = index
        events.append(normalized)
    return {
        "movements": movements,
        "activity_intervals": activities,
        "task_outcomes": task_outcomes,
        "operational_events": events,
    }


def publish_simulation_results(
    *,
    artifact_root: Path,
    catalog: CatalogIdentity,
    replications: Sequence[JointReplicationIdentity],
    results: Sequence[KernelResult],
    seed_manifests: Mapping[str, SeedManifest],
    resolved_config: dict,
    dependencies: tuple[ArtifactDependency, ...],
    worker_count: int = 1,
) -> ArtifactRef:
    """Atomically publish complete kernel outputs with one coarse partition per replication."""

    replication_set = _replication_set(replications)
    identities = {item.replication_id: item for item in replication_set.replications}
    result_by_id = {item.replication_id: item for item in results}
    if len(result_by_id) != len(results) or set(result_by_id) != set(identities):
        raise ValueError("kernel results must cover the replication set exactly")
    if set(seed_manifests) != set(identities):
        raise ValueError("seed manifests must cover the replication set exactly")
    if any(item.catalog_hash != catalog.catalog_hash for item in identities.values()):
        raise ValueError("joint replications must reference the supplied catalog")
    catalog_keys = tuple((key.fleet_id, key.vehicle_id) for key in catalog.vehicle_keys)
    partitions: dict[str, dict[tuple[str], list[dict]]] = {
        name: {(replication_id,): [] for replication_id in sorted(identities)}
        for name in ("movements", "activity_intervals", "task_outcomes", "operational_events")
    }
    status_partitions = {(replication_id,): [] for replication_id in sorted(identities)}
    output_hashes = {}
    for replication_id in sorted(identities):
        result = result_by_id[replication_id]
        outcome_keys = tuple(
            sorted(
                (row.vehicle.fleet_id, row.vehicle.vehicle_id) for row in result.vehicle_outcomes
            )
        )
        if outcome_keys != catalog_keys:
            raise ValueError("every complete replication must retain the full vehicle catalog")
        _validate_result(result, catalog_keys)
        rows = _result_rows(result)
        for _, group in itertools.groupby(
            rows["activity_intervals"],
            key=lambda row: (row["fleet_id"], row["vehicle_id"]),
        ):
            intervals = list(group)
            if any(
                left["end_s"] > right["start_s"] for left, right in zip(intervals, intervals[1:])
            ):
                raise ValueError("kernel result contains overlapping vehicle activities")
            if any(
                row["start_s"] < result.simulation_start_s or row["end_s"] > result.end_s
                for row in intervals
            ):
                raise ValueError("kernel activity lies outside the simulation interval")
        output_hashes[replication_id] = _streaming_output_hash(rows, seed_manifests[replication_id])
        for name, values in rows.items():
            partitions[name][(replication_id,)] = values
        identity = identities[replication_id]
        seed = seed_manifests[replication_id]
        status_partitions[(replication_id,)] = [
            {
                "replication_id": replication_id,
                "joint_scenario_id": identity.joint_scenario_id,
                "scenario_realization_hash": identity.scenario_realization_hash,
                "catalog_hash": identity.catalog_hash,
                "complete": True,
                "seed_manifest_json": canonical_json_text(seed),
                "seed_manifest_hash": scientific_hash(seed),
                "movement_row_count": len(rows["movements"]),
                "activity_row_count": len(rows["activity_intervals"]),
                "task_outcome_row_count": len(rows["task_outcomes"]),
                "operational_event_row_count": len(rows["operational_events"]),
                "processed_heap_events": result.processed_heap_events,
                "maximum_queue_size": result.maximum_queue_size,
            }
        ]
    replication_axis = (PartitionAxis(name="replication_id", values=tuple(sorted(identities))),)
    catalog_rows = {
        (): [
            {
                "fleet_id": key.fleet_id,
                "vehicle_id": key.vehicle_id,
                "catalog_index": index,
                "catalog_id": catalog.catalog_id,
                "catalog_hash": catalog.catalog_hash,
                "physical_metadata_hash": catalog.physical_metadata_hash,
            }
            for index, key in enumerate(catalog.vehicle_keys)
        ]
    }
    table_data = (
        PartitionedTableData(
            "activity_intervals",
            ACTIVITY_SCHEMA,
            replication_axis,
            partitions["activity_intervals"],
            ("replication_id", "fleet_id", "vehicle_id", "activity_index"),
        ),
        PartitionedTableData(
            "movements",
            MOVEMENT_SCHEMA,
            replication_axis,
            partitions["movements"],
            ("replication_id", "fleet_id", "vehicle_id", "movement_index"),
        ),
        PartitionedTableData(
            "operational_events",
            OPERATIONAL_EVENT_SCHEMA,
            replication_axis,
            partitions["operational_events"],
            ("replication_id", "event_index"),
        ),
        PartitionedTableData(
            "replication_status",
            REPLICATION_STATUS_SCHEMA,
            replication_axis,
            status_partitions,
            ("replication_id",),
        ),
        PartitionedTableData(
            "task_outcomes",
            TASK_OUTCOME_SCHEMA,
            replication_axis,
            partitions["task_outcomes"],
            ("replication_id", "fleet_id", "task_id"),
        ),
        PartitionedTableData(
            "vehicle_catalog",
            VEHICLE_CATALOG_SCHEMA,
            (),
            catalog_rows,
            ("fleet_id", "vehicle_id"),
        ),
    )
    stationary_rows = {
        (replication_id,): [
            {
                "replication_id": replication_id,
                "location_id": location.location_id,
                "location_json": location.model_dump_json(),
            }
            for location in result_by_id[replication_id].stationary_locations
        ]
        for replication_id in sorted(identities)
    }
    if any(stationary_rows.values()):
        table_data += (
            PartitionedTableData(
                "stationary_locations",
                pa.schema(
                    [
                        ("replication_id", pa.string(), False),
                        ("location_id", pa.string(), False),
                        ("location_json", pa.string(), False),
                    ]
                ),
                replication_axis,
                stationary_rows,
                ("replication_id", "location_id"),
            ),
        )
    scientific_config = {
        "stationary_locations_hash": scientific_hash(
            [stationary_rows[key] for key in sorted(stationary_rows)]
        ),
        "simulation": resolved_config,
        "catalog_id": catalog.catalog_id,
        "replication_ids": list(sorted(identities)),
        "replication_output_hashes": output_hashes,
    }
    identity = ScientificIdentity(
        schema_version="2.0",
        artifact_kind="simulation",
        resolved_config=scientific_config,
        resolved_config_hash=scientific_hash(scientific_config),
        dependency_hashes={item.role: item.content_hash for item in dependencies},
        algorithm_versions={
            "event_kernel": EVENT_KERNEL_VERSION,
            "executor": EXECUTOR_VERSION,
            "random_cruise": (
                RANDOM_CRUISE_VERSION
                if any(
                    interval.kind == "movement" and interval.movement_kind == "cruise"
                    for result in results
                    for execution in result.execution_outcomes
                    for interval in execution.realized_intervals
                )
                else "operational.random_cruise@1"
            ),
            "depot_return": DEPOT_RETURN_VERSION,
            "storage": SIMULATION_STORAGE_VERSION,
        },
        random_stream_version=RNG_DERIVATION_VERSION,
        catalog_hash=catalog.catalog_hash,
        replication_set_hash=replication_set.replication_set_hash,
    )
    return publish_partitioned_artifact(
        artifact_root=artifact_root,
        collection="simulations",
        identity=identity,
        dependencies=dependencies,
        tables=table_data,
        worker_count=worker_count,
    ).reference


class SimulationArtifactReader:
    """Verified predicate-ready access to retained simulation movements."""

    def __init__(self, artifact_root: Path, reference: ArtifactRef) -> None:
        if reference.artifact_kind != "simulation":
            raise ValueError("simulation reader requires a simulation artifact reference")
        self.artifact = verify_partitioned_artifact(
            artifact_root=artifact_root,
            collection="simulations",
            reference=reference,
        )
        self._tables = {table.name: table for table in self.artifact.manifest.tables}
        status = read_partitioned_table(
            self.artifact,
            table_name="replication_status",
            schema=REPLICATION_STATUS_SCHEMA,
        ).to_pylist()
        if not status or not all(row["complete"] for row in status):
            raise ValueError("simulation artifact has no complete replication set")
        self.replication_ids = tuple(sorted(row["replication_id"] for row in status))
        catalog = read_partitioned_table(
            self.artifact,
            table_name="vehicle_catalog",
            schema=VEHICLE_CATALOG_SCHEMA,
        ).to_pylist()
        self.vehicle_keys = tuple(
            VehicleKey(fleet_id=row["fleet_id"], vehicle_id=row["vehicle_id"]) for row in catalog
        )

    @property
    def reference(self) -> ArtifactRef:
        return self.artifact.reference

    def read_stationary_locations(self):
        if "stationary_locations" not in self._tables:
            return {}
        schema = pa.schema(
            [
                ("replication_id", pa.string(), False),
                ("location_id", pa.string(), False),
                ("location_json", pa.string(), False),
            ]
        )
        rows = read_partitioned_table(
            self.artifact, table_name="stationary_locations", schema=schema
        ).to_pylist()
        from mobile_sensing.contracts import LocationRef

        values = {}
        for row in rows:
            location = LocationRef.model_validate_json(row["location_json"])
            if location.location_id in values and location != values[location.location_id]:
                raise ValueError(
                    "Retained stationary location identity changes across replications"
                )
            values[location.location_id] = location
        return values

    def read_activities(self):
        return read_partitioned_table(
            self.artifact, table_name="activity_intervals", schema=ACTIVITY_SCHEMA
        ).to_pandas()

    def read_movements(self, replication_ids: Sequence[str] | None = None):
        requested = (
            self.replication_ids if replication_ids is None else tuple(sorted(replication_ids))
        )
        if not requested or not set(requested) <= set(self.replication_ids):
            raise ValueError("movement replication filter is empty or outside the complete set")
        table = self._tables["movements"]
        axis_values = tuple(table.partition_axes[0].values)
        indices = [axis_values.index(replication_id) for replication_id in requested]
        return read_partitioned_table(
            self.artifact,
            table_name="movements",
            schema=MOVEMENT_SCHEMA,
            partition_indices=indices,
        ).to_pandas()
