"""Atomic immutable publication for normalized M03 datasets."""

from __future__ import annotations

import hashlib
import importlib.metadata
import os
import platform
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from mobile_sensing.contracts import (
    ArtifactDependency,
    ArtifactManifest,
    ArtifactRef,
    PartitionManifest,
    RuntimeProvenance,
    ScientificIdentity,
    TableManifest,
    canonical_json_bytes,
    scientific_hash,
    stable_id,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def publish_dataset(
    *,
    artifact_root: Path,
    resolved_config: dict,
    dependencies: tuple[ArtifactDependency, ...],
    algorithm_versions: dict[str, str],
    frames: dict[str, pd.DataFrame],
) -> tuple[ArtifactRef, Path]:
    names = tuple(sorted(frames))
    identity = ScientificIdentity(
        schema_version="2.0",
        artifact_kind="dataset",
        resolved_config=resolved_config,
        resolved_config_hash=scientific_hash(resolved_config),
        dependency_hashes={item.role: item.content_hash for item in dependencies},
        algorithm_versions=algorithm_versions,
    )
    fingerprint = scientific_hash(identity)
    reference = ArtifactRef(
        artifact_id=stable_id("dataset", fingerprint),
        artifact_kind="dataset",
        content_hash=fingerprint,
    )
    final = Path(artifact_root).resolve() / "datasets" / reference.artifact_id
    if final.exists():
        if not (final / "_SUCCESS").is_file():
            raise ValueError("existing dataset artifact is incomplete")
        manifest = ArtifactManifest.model_validate_json((final / "manifest.json").read_bytes())
        if manifest.content_fingerprint != fingerprint:
            raise ValueError("existing dataset artifact identity mismatch")
        for table in manifest.tables:
            partition = table.partitions[0]
            if partition.file_sha256 != _sha256(final / table.relative_path):
                raise ValueError(f"dataset artifact checksum failed for {table.name}")
        return reference, final
    final.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".dataset-staging-", dir=final.parent) as staging_name:
        staging = Path(staging_name)
        tables = []
        for name in names:
            path = staging / f"{name}.parquet"
            frames[name].to_parquet(path, index=False)
            checksum = _sha256(path)
            schema_hash = hashlib.sha256(pq.read_schema(path).serialize().to_pybytes()).hexdigest()
            tables.append(
                TableManifest(
                    name=name,
                    relative_path=path.name,
                    schema_hash=schema_hash,
                    row_count=len(frames[name]),
                    partition_axes=(),
                    partitions=(
                        PartitionManifest(
                            partition_values={},
                            row_count=len(frames[name]),
                            file_sha256=checksum,
                        ),
                    ),
                )
            )
        try:
            version = importlib.metadata.version("mobile-sensing")
        except importlib.metadata.PackageNotFoundError:
            version = "0.1.0"
        manifest = ArtifactManifest(
            schema_version="2.0",
            artifact_id=reference.artifact_id,
            artifact_kind="dataset",
            content_fingerprint=fingerprint,
            scientific_identity=identity,
            dependencies=dependencies,
            expected_table_names=names,
            tables=tuple(tables),
            created_at_utc=datetime.now(timezone.utc),
            runtime=RuntimeProvenance(
                python_version=platform.python_version(),
                package_version=version,
                worker_count=1,
                platform=platform.platform(),
            ),
        )
        (staging / "manifest.json").write_bytes(canonical_json_bytes(manifest) + b"\n")
        (staging / "_SUCCESS").write_text("complete\n", encoding="ascii")
        os.replace(staging, final)
    return reference, final


def verified_dataset_directory(
    reference: ArtifactRef, *, artifact_root: Path
) -> tuple[Path, ArtifactManifest]:
    """Resolve an immutable dataset and verify its complete declared inventory."""

    reference = ArtifactRef.model_validate(reference)
    if reference.artifact_kind != "dataset":
        raise ValueError("normalized tabular artifacts must have dataset kind")
    directory = Path(artifact_root).resolve() / "datasets" / reference.artifact_id
    if not (directory / "_SUCCESS").is_file():
        raise FileNotFoundError("dataset artifact is absent or incomplete")
    manifest = ArtifactManifest.model_validate_json((directory / "manifest.json").read_bytes())
    if (
        manifest.artifact_id != reference.artifact_id
        or manifest.content_fingerprint != reference.content_hash
    ):
        raise ValueError("dataset artifact reference does not match its manifest")
    actual = tuple(sorted(path.stem for path in directory.glob("*.parquet")))
    if actual != manifest.expected_table_names:
        raise ValueError("dataset artifact table inventory does not match its manifest")
    for table in manifest.tables:
        partition = table.partitions[0]
        if partition.file_sha256 != _sha256(directory / table.relative_path):
            raise ValueError(f"dataset artifact checksum failed for {table.name}")
    return directory, manifest
