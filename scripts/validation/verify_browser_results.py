"""Independently check the artifacts produced by the real installed browser flow."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path

import pyarrow.parquet as pq

from mobile_sensing.api.queries import ArtifactCatalog
from mobile_sensing.jobs import JobStore


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    store = JobStore(args.root)
    projects = store.list_projects()
    assert len(projects) == 1
    jobs = store.list_jobs(project_id=projects[0].project_id)
    completed = [job for job in jobs if job.status == "completed"]
    simulations = [job for job in completed if job.kind == "simulation"]
    assert len(simulations) == 1, "portfolio actions must not create another mobility execution"
    catalog = ArtifactCatalog(args.root)

    def table(job, name):
        return catalog.table(job.result["artifact"]["artifact_id"], name)[1].to_pylist()

    simulation = simulations[0]
    replication_ids = {row["replication_id"] for row in table(simulation, "replication_status")}
    assert len(replication_ids) == 2
    outcomes = table(simulation, "task_outcomes")
    assert len(outcomes) == 4 and all(row["status"] == "completed" for row in outcomes)
    exposure = next(job for job in completed if job.kind == "exposure")
    durations = table(exposure, "exposure")
    totals = {
        replication: math.fsum(
            row["duration_s"] for row in durations if row["replication_id"] == replication
        )
        for replication in replication_ids
    }
    assert set(totals.values()) == {10.0}
    samples = next(
        job
        for job in completed
        if job.kind == "portfolio" and job.result.get("portfolio_stage") == "samples"
    )
    analysis = next(
        job
        for job in completed
        if job.kind == "portfolio" and job.result.get("portfolio_stage") == "analysis"
    )
    metadata = table(samples, "portfolio_metadata")[0]
    assert metadata["replications_R"] == 2 and metadata["sampling_rounds_J"] == 100
    assert metadata["portfolio_sample_count"] == 200
    rounds = table(samples, "sampling_rounds")
    assert len(rounds) == 100
    assert {row["selected_joint_replication_id"] for row in rounds} <= replication_ids
    sample_rows = table(samples, "portfolio_samples")
    statistics = table(analysis, "portfolio_statistics")
    nonempty = next(row for row in statistics if row["total_cost_minor"] > 0)
    selected = [row for row in sample_rows if row["portfolio_id"] == nonempty["portfolio_id"]]
    assert len(selected) == 100
    assert all(row["total_exposure_s"] == 10.0 for row in selected)
    expected_utility = 0.013115644676245552
    assert math.isclose(nonempty["utility_mean"], expected_utility, rel_tol=1e-14)
    assert nonempty["utility_sample_std"] == 0.0
    exports = []
    for job in completed:
        if job.kind != "export":
            continue
        path = args.root / job.result["download_path"]
        assert (path.parent / "_SUCCESS").is_file()
        checksum = hashlib.sha256(path.read_bytes()).hexdigest()
        assert checksum == job.result["export"]["sha256"]
        if path.suffix == ".csv":
            with path.open() as stream:
                rows = list(csv.DictReader(stream))
            assert len(rows) == len(outcomes)
        else:
            rows = pq.read_table(path).to_pylist()
            assert rows == statistics
        exports.append({"path": str(path), "row_count": len(rows), "sha256": checksum})
    assert len(exports) >= 2
    cancelled = [job for job in jobs if job.status == "cancelled"]
    for job in cancelled:
        assert not job.result
        try:
            resource = store.get_resource(job.resource_id)
        except KeyError:
            pass  # Cancellation may remove the unpublished output reservation.
        else:
            assert resource.metadata.get("artifact") is None
    result = {
        "project": projects[0].model_dump(mode="json"),
        "revisions": [
            revision.model_dump(mode="json")
            for revision in store.list_revisions(projects[0].project_id)
        ],
        "jobs": [job.model_dump(mode="json") for job in jobs],
        "replications_R": 2,
        "sampling_rounds_J": 100,
        "exposure_total_s_by_replication": totals,
        "displayed_nonempty_mean_utility": expected_utility,
        "displayed_sample_exposure_s": 10.0,
        "all_J_draws_retained": True,
        "exports_verified": exports,
        "cancelled_jobs_without_completed_output": len(cancelled),
    }
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(
        f"Verified browser artifacts, R=2, J=100, {len(exports)} exports, {len(cancelled)} cancellations"
    )


if __name__ == "__main__":
    main()
