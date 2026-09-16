"""Compare bounded release artifacts by decoded content, including R/J lineage."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pyarrow.parquet as pq

from mobile_sensing.artifacts import partition_file, verify_partitioned_artifact
from mobile_sensing.contracts import ArtifactRef, EnvironmentArtifactRef, canonical_json_text
from mobile_sensing.environment import PreparedEnvironmentReader
from mobile_sensing.datasets.storage import verified_dataset_directory

COLLECTIONS = {
    "environment": "environments",
    "gtfs_reconstruction": "datasets",
    "scenario_validation": "scenario_validations",
    "simulation": "simulations",
    "exposure": "exposures",
    "portfolio_samples": "portfolios",
    "portfolio_analysis": "portfolios",
}
MAX_TABLE_ROWS = 1_000_000


def _json_value(value):
    if isinstance(value, bytes):
        return {"binary_hex": value.hex()}
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def _hash(value) -> str:
    return hashlib.sha256(canonical_json_text(value).encode()).hexdigest()


def artifact_evidence(root: Path, role: str, ref: dict) -> dict:
    """Verify publication/checksums, then compare every declared table and manifest."""
    collection = COLLECTIONS[role]
    reference = ArtifactRef.model_validate(ref)
    directory = root / collection / reference.artifact_id
    if role == "environment":
        PreparedEnvironmentReader(root).read(EnvironmentArtifactRef.model_validate(ref))
        manifest = json.loads((directory / "manifest.json").read_text())
    elif role == "gtfs_reconstruction":
        _, verified_manifest = verified_dataset_directory(reference, artifact_root=root)
        manifest = verified_manifest.model_dump(mode="json")
    else:
        verified = verify_partitioned_artifact(
            artifact_root=root, collection=collection, reference=reference
        )
        manifest = verified.manifest.model_dump(mode="json")
    tables = {}
    for table in manifest["tables"]:
        if table["row_count"] > MAX_TABLE_ROWS:
            raise ValueError("release comparator table exceeds its bounded row limit")
        paths = (
            [directory / table["relative_path"]]
            if role in {"environment", "gtfs_reconstruction"}
            else [
                partition_file(directory / table["relative_path"], index)
                for index, partition in enumerate(table["partitions"])
                if partition["row_count"]
            ]
        )
        row_hashes = []
        for path in paths:
            if not path.exists() and table["row_count"] == 0:
                continue
            for batch in pq.ParquetFile(path).iter_batches(batch_size=10_000):
                row_hashes.extend(_hash(_json_value(row)) for row in batch.to_pylist())
        if len(row_hashes) != table["row_count"]:
            raise AssertionError(f"decoded row count differs: {role}.{table['name']}")
        # Canonical multiset comparison preserves repeated rows, including draws.
        tables[table["name"]] = {
            "row_count": len(row_hashes),
            "decoded_multiset_sha256": _hash(sorted(row_hashes)),
        }
        for partition in table["partitions"]:
            partition.pop("file_sha256")
    manifest.pop("created_at_utc")
    manifest["runtime"].pop("worker_count")
    return {"reference": ref, "manifest": manifest, "tables": tables}


def compare(root_a: Path, report_a: Path, root_b: Path, report_b: Path) -> dict:
    first = json.loads(report_a.read_text())
    second = json.loads(report_b.read_text())
    for report in (first, second):
        if set(report["artifacts"]) != set(COLLECTIONS):
            raise AssertionError(
                "release comparison requires all seven artifact roles, including portfolio"
            )
    artifacts = {}
    for role in COLLECTIONS:
        if first["artifacts"][role] != second["artifacts"][role]:
            raise AssertionError(f"{role} artifact identity differs")
        left = artifact_evidence(root_a, role, first["artifacts"][role])
        right = artifact_evidence(root_b, role, second["artifacts"][role])
        if left != right:
            raise AssertionError(f"canonical manifest/content differs for {role}")
        artifacts[role] = left
    fields = sorted(set(first) - {"timing_s", "memory", "portfolio_timing_s"})
    if set(first) != set(second):
        raise AssertionError("release report fields differ")
    for field in fields:
        if first[field] != second[field]:
            raise AssertionError(f"release report field differs: {field}")
    return {
        "schema_version": "m12-run-comparison@2",
        "numeric_content_equal": True,
        "artifact_identity_equal": True,
        "report_fields_compared": fields,
        "artifacts": artifacts,
        "comparison_note": "All seven artifacts verified. Decoded row multisets preserve multiplicity; manifests include configuration, dependencies, axes, completeness and schemas. Excluded: creation time, worker count, file checksums and measured performance. Parquet bytes are verified locally but not compared between runs.",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root_a", type=Path)
    parser.add_argument("report_a", type=Path)
    parser.add_argument("root_b", type=Path)
    parser.add_argument("report_b", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = compare(args.root_a, args.report_a, args.root_b, args.report_b)
    rendered = canonical_json_text(result) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered)
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
