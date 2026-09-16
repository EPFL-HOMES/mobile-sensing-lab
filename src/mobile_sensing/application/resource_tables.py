"""Publish compact derived tables through the shared immutable artifact store."""

import json
import pandas as pd
import pyarrow as pa
from mobile_sensing.artifacts import (
    PartitionedTableData,
    publish_partitioned_artifact,
    verify_partitioned_artifact,
    partition_file,
)
from mobile_sensing.contracts import ScientificIdentity, scientific_hash, canonical_json_text


def publish_tables(root, *, config, dependencies, algorithm, frames, keys):
    config = json.loads(canonical_json_text(config))
    identity = ScientificIdentity(
        schema_version="2.0",
        artifact_kind="dataset",
        resolved_config=config,
        resolved_config_hash=scientific_hash(config),
        dependency_hashes={d.role: d.content_hash for d in dependencies},
        algorithm_versions={"derived_tables": algorithm},
    )
    tables = []
    for name, frame in frames.items():
        table = pa.Table.from_pandas(frame, preserve_index=False)
        tables.append(
            PartitionedTableData(
                name=name,
                schema=table.schema,
                partition_axes=(),
                rows_by_partition={(): table.to_pylist()},
                key_columns=keys[name],
            )
        )
    return publish_partitioned_artifact(
        artifact_root=root,
        collection="datasets",
        identity=identity,
        dependencies=tuple(dependencies),
        tables=tables,
    ).reference


def read_table(root, reference, name):
    value = verify_partitioned_artifact(
        artifact_root=root, collection="datasets", reference=reference
    )
    table = next(t for t in value.manifest.tables if t.name == name)
    path = partition_file(value.directory / table.relative_path, 0)
    return pd.read_parquet(path) if table.row_count else pd.DataFrame()
