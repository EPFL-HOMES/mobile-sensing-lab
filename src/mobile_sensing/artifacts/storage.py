"""Atomic Parquet publication with explicit complete partition products."""

from __future__ import annotations

import hashlib
import importlib.metadata
import itertools
import os
import platform
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from mobile_sensing.contracts import (
    ArtifactDependency,
    ArtifactManifest,
    ArtifactRef,
    PartitionAxis,
    PartitionManifest,
    RuntimeProvenance,
    ScientificIdentity,
    TableManifest,
    ValidationIssue,
    canonical_json_bytes,
    scientific_hash,
    stable_id,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _schema_hash(schema: pa.Schema) -> str:
    return hashlib.sha256(schema.serialize().to_pybytes()).hexdigest()


def partition_file(table_directory: Path, partition_index: int) -> Path:
    """Return the canonical coarse partition path for a manifest position."""

    return table_directory / f"part-{partition_index:06d}.parquet"


@dataclass(frozen=True, slots=True)
class PartitionedTableData:
    name: str
    schema: pa.Schema
    partition_axes: tuple[PartitionAxis, ...]
    rows_by_partition: Mapping[tuple[str | int | bool, ...], Sequence[Mapping[str, Any]]]
    key_columns: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.name or "/" in self.name or "\\" in self.name:
            raise ValueError("table name must be a nonempty path component")
        if not self.key_columns or any(
            column not in self.schema.names for column in self.key_columns
        ):
            raise ValueError("table key columns must be nonempty schema fields")
        axis_names = tuple(axis.name for axis in self.partition_axes)
        if axis_names != tuple(sorted(axis_names)) or len(axis_names) != len(set(axis_names)):
            raise ValueError("partition axes must have unique canonical names")
        expected = set(
            itertools.product(*(axis.values for axis in self.partition_axes))
            if self.partition_axes
            else {()}
        )
        if set(self.rows_by_partition) != expected:
            raise ValueError("rows must cover the exact declared partition product")


@dataclass(frozen=True, slots=True)
class VerifiedArtifact:
    reference: ArtifactRef
    directory: Path
    manifest: ArtifactManifest


def _canonical_rows(
    specification: PartitionedTableData,
    partition_values: tuple[str | int | bool, ...],
) -> list[dict[str, Any]]:
    schema_fields = frozenset(specification.schema.names)
    axis_names = tuple(axis.name for axis in specification.partition_axes)
    normalized = []
    for raw in specification.rows_by_partition[partition_values]:
        row = dict(raw)
        if frozenset(row) != schema_fields:
            missing = sorted(schema_fields - frozenset(row))
            extra = sorted(frozenset(row) - schema_fields)
            raise ValueError(
                f"table {specification.name!r} row fields disagree with schema; "
                f"missing={missing}, extra={extra}"
            )
        if any(
            row[name] != value for name, value in zip(axis_names, partition_values, strict=True)
        ):
            raise ValueError(f"table {specification.name!r} row is in the wrong partition")
        key = tuple(row[column] for column in specification.key_columns)
        if any(value is None for value in key):
            raise ValueError(f"table {specification.name!r} key fields cannot be null")
        normalized.append(row)
    normalized.sort(key=lambda row: tuple(row[column] for column in specification.key_columns))
    keys = [tuple(row[column] for column in specification.key_columns) for row in normalized]
    if len(keys) != len(set(keys)):
        raise ValueError(f"table {specification.name!r} keys must be unique")
    return normalized


def _write_table(specification: PartitionedTableData, staging: Path) -> TableManifest:
    relative_path = f"tables/{specification.name}"
    directory = staging / relative_path
    directory.mkdir(parents=True)
    axis_names = tuple(axis.name for axis in specification.partition_axes)
    partition_product = (
        list(itertools.product(*(axis.values for axis in specification.partition_axes)))
        if specification.partition_axes
        else [()]
    )
    partitions = []
    total_rows = 0
    for index, values in enumerate(partition_product):
        rows = _canonical_rows(specification, values)
        total_rows += len(rows)
        checksum = None
        if rows:
            table = pa.Table.from_pylist(rows, schema=specification.schema)
            path = partition_file(directory, index)
            pq.write_table(table, path, compression="zstd", use_dictionary=True)
            checksum = _sha256(path)
        partitions.append(
            PartitionManifest(
                partition_values=dict(zip(axis_names, values, strict=True)),
                row_count=len(rows),
                file_sha256=checksum,
            )
        )
    return TableManifest(
        name=specification.name,
        relative_path=relative_path,
        schema_hash=_schema_hash(specification.schema),
        row_count=total_rows,
        partition_axes=specification.partition_axes,
        partitions=tuple(partitions),
    )


def publish_partitioned_artifact(
    *,
    artifact_root: Path,
    collection: str,
    identity: ScientificIdentity,
    dependencies: tuple[ArtifactDependency, ...],
    tables: Sequence[PartitionedTableData],
    warnings: tuple[ValidationIssue, ...] = (),
    worker_count: int = 1,
) -> VerifiedArtifact:
    """Publish all declared tables atomically, or verify an immutable cache hit."""

    if not collection or "/" in collection or "\\" in collection:
        raise ValueError("artifact collection must be a nonempty path component")
    ordered = tuple(sorted(tables, key=lambda item: item.name))
    if not ordered or len({item.name for item in ordered}) != len(ordered):
        raise ValueError("artifact tables must be nonempty with unique names")
    content_fingerprint = scientific_hash(identity)
    reference = ArtifactRef(
        artifact_id=stable_id(identity.artifact_kind, content_fingerprint),
        artifact_kind=identity.artifact_kind,
        content_hash=content_fingerprint,
    )
    final = Path(artifact_root).resolve() / collection / reference.artifact_id
    if final.exists():
        return verify_partitioned_artifact(
            artifact_root=artifact_root,
            collection=collection,
            reference=reference,
        )
    final.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{identity.artifact_kind}-staging-", dir=final.parent
    ) as name:
        staging = Path(name)
        manifests = tuple(_write_table(table, staging) for table in ordered)
        try:
            package_version = importlib.metadata.version("mobile-sensing")
        except importlib.metadata.PackageNotFoundError:
            package_version = "0.1.0"
        manifest = ArtifactManifest(
            schema_version="2.0",
            artifact_id=reference.artifact_id,
            artifact_kind=identity.artifact_kind,
            content_fingerprint=content_fingerprint,
            scientific_identity=identity,
            dependencies=dependencies,
            expected_table_names=tuple(item.name for item in ordered),
            tables=manifests,
            created_at_utc=datetime.now(timezone.utc),
            runtime=RuntimeProvenance(
                python_version=platform.python_version(),
                package_version=package_version,
                worker_count=worker_count,
                platform=platform.platform(),
            ),
            warnings=warnings,
        )
        (staging / "manifest.json").write_bytes(canonical_json_bytes(manifest))
        (staging / "_SUCCESS").write_bytes(b"")
        try:
            os.replace(staging, final)
        except FileExistsError:
            pass
    return verify_partitioned_artifact(
        artifact_root=artifact_root,
        collection=collection,
        reference=reference,
    )


def verify_partitioned_artifact(
    *, artifact_root: Path, collection: str, reference: ArtifactRef
) -> VerifiedArtifact:
    """Validate success marker, manifest, exact files, schemas, counts, and checksums."""

    directory = Path(artifact_root).resolve() / collection / reference.artifact_id
    if not (directory / "_SUCCESS").is_file():
        raise FileNotFoundError(f"incomplete artifact: {reference.artifact_id}")
    manifest = ArtifactManifest.model_validate_json((directory / "manifest.json").read_bytes())
    if (
        manifest.artifact_id != reference.artifact_id
        or manifest.artifact_kind != reference.artifact_kind
        or manifest.content_fingerprint != reference.content_hash
    ):
        raise ValueError("artifact reference does not match its manifest")
    expected_files: set[Path] = set()
    for table in manifest.tables:
        table_directory = directory / table.relative_path
        if not table_directory.is_dir():
            raise ValueError(f"artifact table directory is missing: {table.name}")
        for index, partition in enumerate(table.partitions):
            path = partition_file(table_directory, index)
            if partition.row_count == 0:
                if path.exists():
                    raise ValueError(
                        f"empty partition unexpectedly has a file: {table.name}[{index}]"
                    )
                continue
            expected_files.add(path.resolve())
            if not path.is_file():
                raise ValueError(f"artifact partition is missing: {table.name}[{index}]")
            if _sha256(path) != partition.file_sha256:
                raise ValueError(f"artifact partition checksum failed: {table.name}[{index}]")
            schema = pq.read_schema(path)
            if _schema_hash(schema) != table.schema_hash:
                raise ValueError(f"artifact partition schema failed: {table.name}[{index}]")
            if pq.read_metadata(path).num_rows != partition.row_count:
                raise ValueError(f"artifact partition row count failed: {table.name}[{index}]")
    expected_files.update(
        {
            (directory / "manifest.json").resolve(),
            (directory / "_SUCCESS").resolve(),
        }
    )
    actual_files = {path.resolve() for path in directory.rglob("*") if path.is_file()}
    if actual_files != expected_files:
        raise ValueError("artifact contains missing or unmanifested files")
    return VerifiedArtifact(reference=reference, directory=directory, manifest=manifest)


def read_partitioned_table(
    artifact: VerifiedArtifact,
    *,
    table_name: str,
    schema: pa.Schema,
    partition_indices: Sequence[int] | None = None,
    filters: Any | None = None,
) -> pa.Table:
    """Read selected complete coarse partitions with Parquet predicate pushdown."""

    try:
        manifest = next(item for item in artifact.manifest.tables if item.name == table_name)
    except StopIteration as exc:
        raise KeyError(f"artifact has no table {table_name!r}") from exc
    if _schema_hash(schema) != manifest.schema_hash:
        raise ValueError(f"reader schema disagrees with table {table_name!r}")
    indices = (
        tuple(range(len(manifest.partitions)))
        if partition_indices is None
        else tuple(partition_indices)
    )
    if len(set(indices)) != len(indices) or any(
        index < 0 or index >= len(manifest.partitions) for index in indices
    ):
        raise ValueError("partition indices must be unique and in range")
    tables = []
    directory = artifact.directory / manifest.relative_path
    for index in indices:
        if manifest.partitions[index].row_count == 0:
            continue
        tables.append(pq.read_table(partition_file(directory, index), filters=filters))
    if not tables:
        return pa.Table.from_pylist([], schema=schema)
    result = pa.concat_tables(tables)
    if result.schema != schema:
        raise ValueError(f"loaded schema disagrees with table {table_name!r}")
    return result
