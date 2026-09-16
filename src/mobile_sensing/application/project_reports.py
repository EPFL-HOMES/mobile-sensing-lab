"""Worker-generated compact files for ordinary project browsing."""

import json
import os
import tempfile
from pathlib import Path
import pandas as pd

from mobile_sensing.api.fleet_queries import fleet_summary
from mobile_sensing.api.models import MatrixQueryRequest
from mobile_sensing.api.queries import (
    ArtifactCatalog,
    query_matrix,
    query_environment,
    _table_paths,
)
from mobile_sensing.application.run_pipeline import read_named_record
from mobile_sensing.jobs.models import JobStoreLimits


def build_report(root, identifier, kind, cancellation):
    root = Path(root)
    target = root / ".system" / "reports" / identifier
    if (target / "complete.json").is_file():
        return target
    record = read_named_record(root, identifier, kind)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as temporary:
        stage = Path(temporary)
        cancellation.raise_if_cancelled()
        if kind == "studio_run":
            summary = fleet_summary(root, record.exposure.artifact_id, limits=JobStoreLimits())
            pd.DataFrame(summary["fleets"]).to_csv(stage / "fleet_summary.csv", index=False)
            matrix = query_matrix(
                root,
                MatrixQueryRequest(
                    resource_id=record.exposure.artifact_id,
                    kind="operational_aggregate",
                    statistic="mean",
                    temporal_aggregation="sum",
                ),
                JobStoreLimits(),
            )
            durations = {row["time_bin_id"]: row["value"] for row in matrix["time_summary"]}
            pd.DataFrame(
                [
                    {**row, "mean_sensing_duration_s": durations[row["time_bin_id"]]}
                    for row in summary["time_summary"]
                ]
            ).to_csv(stage / "time_summary.csv", index=False)
            summary.update(
                mean_sensing_duration_s=matrix["overall_value"],
                mean_coverage_fraction=matrix["mean_coverage_fraction"],
            )
            (stage / "summary.json").write_text(json.dumps(summary, indent=2))
            exposure = ArtifactCatalog(root).locate(record.exposure.artifact_id)
            from mobile_sensing.contracts import EnvironmentArtifactRef

            dependency = next(d for d in exposure.manifest.dependencies if d.role == "environment")
            environment = query_environment(
                root,
                EnvironmentArtifactRef(
                    artifact_id=dependency.artifact_id,
                    artifact_kind="environment",
                    content_hash=dependency.content_hash,
                ),
            )
            grid = environment.grid_cells[["cell_id", "geometry"]].copy()
            values = {row["cell_id"]: row["value"] for row in matrix["values"]}
            grid["mean_sensing_duration_s"] = grid.cell_id.map(values).fillna(0)
            grid.to_parquet(stage / "mean_sensing.geoparquet", index=False)
        else:
            located = ArtifactCatalog(root).locate(record.frontier.artifact_id)
            for table, name in (
                ("portfolio_statistics", "portfolios.csv"),
                ("budget_frontiers", "frontier.csv"),
            ):
                frames = [pd.read_parquet(path) for path in _table_paths(located, table)]
                (pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()).to_csv(
                    stage / name, index=False
                )
        cancellation.raise_if_cancelled()
        (stage / "complete.json").write_text(
            json.dumps({"source_id": identifier, "version": "project-report@1"})
        )
        if not target.exists():
            os.replace(stage, target)
    return target
