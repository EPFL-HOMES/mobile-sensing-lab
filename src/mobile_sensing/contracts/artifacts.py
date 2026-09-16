"""Immutable artifact, axis, catalog, replication, and lineage contracts."""

from __future__ import annotations

import itertools
from pathlib import PurePosixPath
from typing import ClassVar, Literal, Self, TypeAlias

from pydantic import JsonValue, field_validator, model_validator

from mobile_sensing.contracts.base import (
    CANONICAL_JSON_VERSION,
    CapabilityKey,
    ContractModel,
    FiniteFloat,
    NonNegativeFloat,
    NonNegativeInt,
    OpaqueId,
    PositiveFloat,
    PositiveInt,
    SchemaVersion,
    Sha256,
    UInt64,
    UtcDateTime,
    canonical_json_bytes,
    scientific_hash,
)
from mobile_sensing.contracts.issues import ValidationIssue
from mobile_sensing.contracts.records import VehicleKey


class ArtifactRef(ContractModel):
    artifact_id: OpaqueId
    artifact_kind: Literal[
        "dataset", "environment", "simulation", "exposure", "portfolio", "export"
    ]
    content_hash: Sha256


class EnvironmentArtifactRef(ArtifactRef):
    artifact_kind: Literal["environment"]


class RawGeographicResource(ContractModel):
    role: str
    dataset_id: OpaqueId
    content_hash: Sha256
    source_crs: str


class RawGeographicBundle(ContractModel):
    scientific_identity_excluded_fields: ClassVar[frozenset[str]] = frozenset(
        {"bundle_id", "retrieved_at_utc"}
    )

    schema_version: SchemaVersion
    bundle_id: OpaqueId
    provider: CapabilityKey
    provider_version: str
    query_hash: Sha256
    boundary: RawGeographicResource
    network: RawGeographicResource
    features: tuple[RawGeographicResource, ...] = ()
    retrieved_at_utc: UtcDateTime
    attribution: tuple[str, ...]
    source_coverage_limits: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_resources(self) -> Self:
        roles = [self.boundary.role, self.network.role, *(item.role for item in self.features)]
        if len(roles) != len(set(roles)):
            raise ValueError("raw geographic bundle resource roles must be unique")
        if not self.attribution or any(item == "" for item in self.attribution):
            raise ValueError("raw geographic bundle requires nonempty attribution")
        return self


class TimeBin(ContractModel):
    time_bin_id: OpaqueId
    canonical_index: NonNegativeInt
    start_s: FiniteFloat
    end_s: FiniteFloat

    @model_validator(mode="after")
    def validate_duration(self) -> Self:
        if self.end_s <= self.start_s:
            raise ValueError("time bin must have positive duration")
        return self


class TimeAxis(ContractModel):
    time_axis_id: OpaqueId
    observation_start_s: FiniteFloat
    end_s: FiniteFloat
    bins: tuple[TimeBin, ...]

    @model_validator(mode="after")
    def validate_partition(self) -> Self:
        if self.end_s <= self.observation_start_s or not self.bins:
            raise ValueError("time axis requires a nonempty positive observation interval")
        if tuple(item.canonical_index for item in self.bins) != tuple(range(len(self.bins))):
            raise ValueError("time bins require contiguous canonical indices")
        ids = [item.time_bin_id for item in self.bins]
        if len(ids) != len(set(ids)):
            raise ValueError("time bin IDs must be unique")
        if self.bins[0].start_s != self.observation_start_s or self.bins[-1].end_s != self.end_s:
            raise ValueError("time bins must cover the complete observation interval")
        if any(left.end_s != right.start_s for left, right in zip(self.bins, self.bins[1:])):
            raise ValueError("time bins must form an exact contiguous partition")
        return self


class GridAxis(ContractModel):
    grid_axis_id: OpaqueId
    cell_ids: tuple[OpaqueId, ...]
    working_crs: str

    @field_validator("cell_ids")
    @classmethod
    def validate_cells(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or len(set(value)) != len(value):
            raise ValueError("grid cell IDs must be nonempty and unique")
        if value != tuple(sorted(value)):
            raise ValueError("grid cell IDs must be canonically sorted")
        return value


class CatalogIdentity(ContractModel):
    catalog_id: OpaqueId
    vehicle_keys: tuple[VehicleKey, ...]
    physical_metadata_hash: Sha256
    catalog_hash: Sha256

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        keys = [(item.fleet_id, item.vehicle_id) for item in self.vehicle_keys]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise ValueError("vehicle catalog keys must be canonically sorted and unique")
        expected = scientific_hash(
            {
                "vehicle_keys": [list(key) for key in keys],
                "physical_metadata_hash": self.physical_metadata_hash,
            }
        )
        if self.catalog_hash != expected:
            raise ValueError("catalog_hash does not match the frozen vehicle catalog")
        return self


class JointReplicationIdentity(ContractModel):
    replication_id: OpaqueId
    joint_scenario_id: OpaqueId
    scenario_realization_hash: Sha256
    catalog_hash: Sha256


class ReplicationSetIdentity(ContractModel):
    replications: tuple[JointReplicationIdentity, ...]
    replication_set_hash: Sha256

    @model_validator(mode="after")
    def validate_set(self) -> Self:
        ids = [item.replication_id for item in self.replications]
        if not ids or ids != sorted(ids) or len(ids) != len(set(ids)):
            raise ValueError("replication IDs must be nonempty, canonically sorted, and unique")
        if len({item.catalog_hash for item in self.replications}) != 1:
            raise ValueError("all joint replications must share one vehicle catalog")
        expected = scientific_hash([item.model_dump(mode="json") for item in self.replications])
        if self.replication_set_hash != expected:
            raise ValueError("replication_set_hash does not match its joint replications")
        return self


class SeedStreamRecord(ContractModel):
    stream_key: str
    semantic_labels: tuple[OpaqueId, ...]
    derived_seed: UInt64


class SeedManifest(ContractModel):
    derivation_version: Literal["semantic-seed@1"] = "semantic-seed@1"
    master_seed: UInt64
    streams: tuple[SeedStreamRecord, ...]

    @field_validator("streams")
    @classmethod
    def validate_streams(cls, value: tuple[SeedStreamRecord, ...]) -> tuple[SeedStreamRecord, ...]:
        keys = [item.stream_key for item in value]
        if len(keys) != len(set(keys)):
            raise ValueError("semantic stream keys must be unique")
        if keys != sorted(keys):
            raise ValueError("semantic stream keys must be canonically sorted")
        return value


class ArtifactDependency(ContractModel):
    role: str
    artifact_id: OpaqueId
    content_hash: Sha256


PartitionValue: TypeAlias = str | int | bool


class PartitionAxis(ContractModel):
    name: str
    values: tuple[PartitionValue, ...]

    @model_validator(mode="after")
    def validate_axis(self) -> Self:
        if self.name == "" or not self.values:
            raise ValueError("partition axis requires a nonempty name and values")
        if len({type(value) for value in self.values}) != 1:
            raise ValueError("partition axis values must use one scalar type")
        canonical = tuple(sorted(self.values, key=canonical_json_bytes))
        if len({canonical_json_bytes(value) for value in canonical}) != len(canonical):
            raise ValueError("partition axis values must be unique")
        if self.values != canonical:
            raise ValueError("partition axis values must be canonically sorted")
        return self


class PartitionManifest(ContractModel):
    partition_values: dict[str, PartitionValue]
    row_count: NonNegativeInt
    file_sha256: Sha256 | None
    complete: Literal[True] = True

    @model_validator(mode="after")
    def validate_file(self) -> Self:
        if self.row_count > 0 and self.file_sha256 is None:
            raise ValueError("nonempty partition requires a file checksum")
        return self


class TableManifest(ContractModel):
    name: str
    relative_path: str
    schema_hash: Sha256
    row_count: NonNegativeInt
    partition_axes: tuple[PartitionAxis, ...]
    partitions: tuple[PartitionManifest, ...]

    @field_validator("relative_path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if value == "" or path.is_absolute() or ".." in path.parts:
            raise ValueError("artifact table path must be a safe relative POSIX path")
        return value

    @model_validator(mode="after")
    def validate_rows(self) -> Self:
        if not self.partitions:
            raise ValueError("table manifest requires explicit complete partitions")
        axis_names = tuple(axis.name for axis in self.partition_axes)
        if len(axis_names) != len(set(axis_names)):
            raise ValueError("partition axis names must be unique")
        if axis_names != tuple(sorted(axis_names)):
            raise ValueError("partition axes must be canonically sorted")
        expected_count = 1
        for axis in self.partition_axes:
            expected_count *= len(axis.values)
        if len(self.partitions) != expected_count:
            raise ValueError("partitions must cover the complete partition-axis product")
        actual: list[tuple[PartitionValue, ...]] = []
        for partition in self.partitions:
            if set(partition.partition_values) != set(axis_names):
                raise ValueError("partition values must match the declared partition axes")
            actual.append(tuple(partition.partition_values[name] for name in axis_names))
        expected = list(itertools.product(*(axis.values for axis in self.partition_axes)))
        if actual != expected:
            raise ValueError("partitions must be unique and in canonical complete-axis order")
        if sum(partition.row_count for partition in self.partitions) != self.row_count:
            raise ValueError("table row count must equal its complete partition counts")
        return self


class RuntimeProvenance(ContractModel):
    python_version: str
    package_version: str
    worker_count: PositiveInt
    platform: str


class ScientificIdentity(ContractModel):
    schema_version: SchemaVersion
    artifact_kind: Literal[
        "dataset", "environment", "simulation", "exposure", "portfolio", "export"
    ]
    resolved_config: dict[str, JsonValue]
    resolved_config_hash: Sha256
    dependency_hashes: dict[str, Sha256]
    algorithm_versions: dict[str, str]
    random_stream_version: str | None = None
    catalog_hash: Sha256 | None = None
    replication_set_hash: Sha256 | None = None
    time_axis_hash: Sha256 | None = None
    grid_axis_hash: Sha256 | None = None
    canonical_json_version: Literal["canonical-json@1"] = CANONICAL_JSON_VERSION

    @model_validator(mode="after")
    def validate_versions(self) -> Self:
        if self.resolved_config_hash != scientific_hash(self.resolved_config):
            raise ValueError("resolved_config_hash does not match resolved_config")
        if not self.algorithm_versions or any(
            key == "" or value == "" for key, value in self.algorithm_versions.items()
        ):
            raise ValueError("scientific identity requires named algorithm versions")
        if any(key == "" for key in self.dependency_hashes):
            raise ValueError("scientific dependency roles must be nonempty")
        requirements = {
            "environment": ("grid_axis_hash",),
            "simulation": ("random_stream_version", "catalog_hash", "replication_set_hash"),
            "exposure": (
                "catalog_hash",
                "replication_set_hash",
                "time_axis_hash",
                "grid_axis_hash",
            ),
            "portfolio": (
                "random_stream_version",
                "catalog_hash",
                "replication_set_hash",
                "time_axis_hash",
                "grid_axis_hash",
            ),
        }
        missing = [
            name for name in requirements.get(self.artifact_kind, ()) if getattr(self, name) is None
        ]
        if missing:
            raise ValueError(
                f"{self.artifact_kind} scientific identity requires: {', '.join(missing)}"
            )
        return self


class ArtifactManifest(ContractModel):
    schema_version: SchemaVersion
    artifact_id: OpaqueId
    artifact_kind: Literal[
        "dataset", "environment", "simulation", "exposure", "portfolio", "export"
    ]
    content_fingerprint: Sha256
    scientific_identity: ScientificIdentity
    dependencies: tuple[ArtifactDependency, ...]
    expected_table_names: tuple[str, ...]
    tables: tuple[TableManifest, ...]
    created_at_utc: UtcDateTime
    runtime: RuntimeProvenance
    warnings: tuple[ValidationIssue, ...] = ()
    complete: Literal[True] = True

    @model_validator(mode="after")
    def validate_manifest(self) -> Self:
        if self.scientific_identity.schema_version != self.schema_version:
            raise ValueError("manifest and scientific identity schema versions must match")
        if self.scientific_identity.artifact_kind != self.artifact_kind:
            raise ValueError("manifest and scientific identity artifact kinds must match")
        if self.content_fingerprint != scientific_hash(self.scientific_identity):
            raise ValueError("content_fingerprint must hash only the scientific identity")
        roles = [dependency.role for dependency in self.dependencies]
        table_names = [table.name for table in self.tables]
        if len(roles) != len(set(roles)):
            raise ValueError("artifact dependency roles must be unique")
        if len(table_names) != len(set(table_names)):
            raise ValueError("artifact table names must be unique")
        if (
            not self.expected_table_names
            or any(name == "" for name in self.expected_table_names)
            or self.expected_table_names != tuple(sorted(set(self.expected_table_names)))
        ):
            raise ValueError("expected table names must be nonempty, unique, and sorted")
        if tuple(table_names) != self.expected_table_names:
            raise ValueError("artifact tables must exactly match the expected table inventory")
        expected_dependencies = {
            dependency.role: dependency.content_hash for dependency in self.dependencies
        }
        if self.scientific_identity.dependency_hashes != expected_dependencies:
            raise ValueError("scientific dependency hashes must match dependency records")
        return self


class PortfolioCountRecord(ContractModel):
    portfolio_id: OpaqueId
    catalog_hash: Sha256
    count_by_fleet: dict[OpaqueId, NonNegativeInt]
    total_cost_minor: NonNegativeInt


class SamplingRoundRecord(ContractModel):
    round_id: NonNegativeInt
    selected_joint_replication_id: OpaqueId
    sampling_design: Literal["joint_replication_uniform_vehicle"]
    sampling_seed: UInt64
    seed_manifest_hash: Sha256
    replication_set_hash: Sha256
    sampling_design_hash: Sha256

    @model_validator(mode="after")
    def validate_design_identity(self) -> Self:
        expected = scientific_hash(
            {
                "sampling_design": self.sampling_design,
                "sampling_seed": self.sampling_seed,
                "seed_manifest_hash": self.seed_manifest_hash,
                "replication_set_hash": self.replication_set_hash,
            }
        )
        if self.sampling_design_hash != expected:
            raise ValueError("sampling_design_hash does not match sampling lineage")
        return self


class SamplingOrderingRecord(ContractModel):
    round_id: NonNegativeInt
    fleet_id: OpaqueId
    rank: NonNegativeInt
    vehicle_id: OpaqueId


class SparseExposureRow(ContractModel):
    replication_id: OpaqueId
    vehicle: VehicleKey
    cell_id: OpaqueId
    time_bin_id: OpaqueId
    duration_s: PositiveFloat


class ExposureReadMetadata(ContractModel):
    exposure_id: OpaqueId
    catalog_hash: Sha256
    replication_set_hash: Sha256
    grid_axis_hash: Sha256
    time_axis_hash: Sha256
    partition_manifest_hash: Sha256
    replication_ids: tuple[OpaqueId, ...]
    vehicle_keys: tuple[VehicleKey, ...]
    complete: Literal[True] = True

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        if not self.replication_ids or self.replication_ids != tuple(sorted(self.replication_ids)):
            raise ValueError("exposure replication IDs must be nonempty and canonically sorted")
        if len(set(self.replication_ids)) != len(self.replication_ids):
            raise ValueError("exposure replication IDs must be unique")
        keys = tuple((key.fleet_id, key.vehicle_id) for key in self.vehicle_keys)
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ValueError("exposure vehicle keys must be canonically sorted and unique")
        return self

    @property
    def scope_hash(self) -> Sha256:
        return scientific_hash(self)


class SparseExposureChunk(ContractModel):
    scope_hash: Sha256
    chunk_index: NonNegativeInt
    rows: tuple[SparseExposureRow, ...]

    @field_validator("rows")
    @classmethod
    def validate_rows(cls, value: tuple[SparseExposureRow, ...]) -> tuple[SparseExposureRow, ...]:
        keys = [
            (
                row.replication_id,
                row.vehicle.fleet_id,
                row.vehicle.vehicle_id,
                row.cell_id,
                row.time_bin_id,
            )
            for row in value
        ]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise ValueError("sparse exposure rows must be canonically sorted and unique")
        return value


class SensingMatrixQuery(ContractModel):
    replication_ids: tuple[OpaqueId, ...]
    vehicle_keys: tuple[VehicleKey, ...]
    cell_ids: tuple[OpaqueId, ...]
    time_bin_ids: tuple[OpaqueId, ...]
    statistic: Literal["raw", "mean", "sample_variance", "sample_std"]

    @model_validator(mode="after")
    def validate_axes(self) -> Self:
        scalar_axes = (self.replication_ids, self.cell_ids, self.time_bin_ids)
        if any(len(axis) != len(set(axis)) for axis in scalar_axes):
            raise ValueError("query axes must be unique")
        vehicle_axis = tuple((key.fleet_id, key.vehicle_id) for key in self.vehicle_keys)
        if vehicle_axis != tuple(sorted(set(vehicle_axis))):
            raise ValueError("query vehicle keys must be canonically sorted and unique")
        if self.statistic == "raw" and not self.replication_ids:
            raise ValueError("raw sensing query requires replication IDs")
        return self


class SensingMatrixValue(ContractModel):
    replication_id: OpaqueId | None = None
    cell_id: OpaqueId
    time_bin_id: OpaqueId
    value: NonNegativeFloat


class SensingMatrixSlice(ContractModel):
    exposure_id: OpaqueId
    grid_axis_hash: Sha256
    time_axis_hash: Sha256
    statistic: Literal["raw", "mean", "sample_variance", "sample_std"]
    replication_ids: tuple[OpaqueId, ...]
    vehicle_keys: tuple[VehicleKey, ...]
    cell_ids: tuple[OpaqueId, ...]
    time_bin_ids: tuple[OpaqueId, ...]
    replication_count: PositiveInt
    expected_shape: tuple[PositiveInt, ...]
    unit: Literal["s", "s^2"]
    values: tuple[SensingMatrixValue, ...]
    absent_value: Literal[0] = 0
    zero_fill: Literal["absent_sparse_rows_are_zero"] = "absent_sparse_rows_are_zero"
    complete: Literal[True] = True

    @model_validator(mode="after")
    def validate_values(self) -> Self:
        scalar_axes = (self.replication_ids, self.cell_ids, self.time_bin_ids)
        if any(not axis or len(axis) != len(set(axis)) for axis in scalar_axes):
            raise ValueError("matrix slice axes must be nonempty and unique")
        vehicle_axis = tuple((key.fleet_id, key.vehicle_id) for key in self.vehicle_keys)
        if not vehicle_axis or vehicle_axis != tuple(sorted(set(vehicle_axis))):
            raise ValueError("matrix slice vehicles must be nonempty, canonical, and unique")
        if self.replication_count != len(self.replication_ids):
            raise ValueError("replication_count must equal the selected replication axis")
        expected_shape = (
            (len(self.replication_ids), len(self.cell_ids), len(self.time_bin_ids))
            if self.statistic == "raw"
            else (len(self.cell_ids), len(self.time_bin_ids))
        )
        if self.expected_shape != expected_shape:
            raise ValueError("expected_shape disagrees with selected matrix axes")
        expected_unit = "s^2" if self.statistic == "sample_variance" else "s"
        if self.unit != expected_unit:
            raise ValueError("matrix unit disagrees with the requested statistic")
        if self.statistic == "raw" and any(value.replication_id is None for value in self.values):
            raise ValueError("raw sensing values require replication IDs")
        if self.statistic != "raw" and any(
            value.replication_id is not None for value in self.values
        ):
            raise ValueError("aggregate sensing values must not contain replication IDs")
        keys = [
            (value.replication_id or "", value.cell_id, value.time_bin_id) for value in self.values
        ]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise ValueError("sensing values must be canonically sorted and unique")
        if any(
            value.cell_id not in self.cell_ids
            or value.time_bin_id not in self.time_bin_ids
            or (
                value.replication_id is not None
                and value.replication_id not in self.replication_ids
            )
            for value in self.values
        ):
            raise ValueError("sensing values must lie on the declared selected axes")
        return self
