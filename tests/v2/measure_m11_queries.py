"""Measure and cross-check the deterministic M11 acceptance workspace."""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import time
from pathlib import Path

from fastapi.testclient import TestClient

from mobile_sensing.api import create_app
from mobile_sensing.api.queries import ArtifactCatalog


def _measure(call, repeats: int = 10) -> dict[str, float]:
    elapsed = []
    for _ in range(repeats):
        started = time.perf_counter()
        response = call()
        elapsed.append(1000.0 * (time.perf_counter() - started))
        assert response.status_code == 200, response.text
    return {
        "median_ms": statistics.median(elapsed),
        "maximum_ms": max(elapsed),
        "repeats": repeats,
    }


def report(root: Path, simulation_id: str, exposure_id: str, analysis_id: str) -> dict:
    client = TestClient(create_app(root))
    catalog = ArtifactCatalog(root)
    replication = client.get(
        f"/api/v1/results/{simulation_id}/replication_status?page_size=1000"
    ).json()["items"][0]["replication_id"]
    vehicles = client.get(f"/api/v1/results/{exposure_id}/vehicle_catalog?page_size=1000").json()[
        "items"
    ]
    vehicle_keys = [
        {"fleet_id": row["fleet_id"], "vehicle_id": row["vehicle_id"]} for row in vehicles
    ]
    budget = max(
        client.get(f"/api/v1/results/{analysis_id}/budget_levels?page_size=1000").json()["items"],
        key=lambda row: row["budget_minor"],
    )
    frontier = client.get(
        f"/api/v1/portfolio-frontiers/{analysis_id}",
        params={"budget_id": budget["budget_id"]},
    ).json()
    point = max(frontier["points"], key=lambda row: row["utility_mean"])
    analysis = catalog.locate(analysis_id)
    sample_id = next(
        item.artifact_id
        for item in analysis.manifest.dependencies
        if item.role == "portfolio_samples"
    )
    sample = client.get(
        f"/api/v1/results/{sample_id}/portfolio_samples",
        params={"portfolio_id": point["portfolio_id"], "page_size": 1000},
    ).json()["items"][0]
    sample_matrix = client.post(
        "/api/v1/matrix-queries",
        json={
            "resource_id": sample_id,
            "kind": "portfolio_sample",
            "portfolio_id": point["portfolio_id"],
            "round_id": sample["round_id"],
            "statistic": "realization",
        },
    ).json()
    operation_summary = client.get(
        f"/api/v1/operation-summaries/{simulation_id}",
        params={"replication_id": replication},
    ).json()
    _, exported_outcomes = catalog.table(simulation_id, "task_outcomes")
    exported_released = sum(
        row["replication_id"] == replication for row in exported_outcomes.to_pylist()
    )
    matrix_body = {
        "resource_id": exposure_id,
        "kind": "operational_aggregate",
        "replication_ids": [replication],
        "vehicle_keys": vehicle_keys,
        "time_bin_ids": [],
        "statistic": "realization",
    }
    timings = {
        "operation_summary": _measure(
            lambda: client.get(
                f"/api/v1/operation-summaries/{simulation_id}",
                params={"replication_id": replication},
            )
        ),
        "movement_map": _measure(
            lambda: client.get(
                f"/api/v1/maps/{simulation_id}/movements",
                params={"replication_id": replication},
            )
        ),
        "sensing_matrix": _measure(lambda: client.post("/api/v1/matrix-queries", json=matrix_body)),
        "budget_frontier": _measure(
            lambda: client.get(
                f"/api/v1/portfolio-frontiers/{analysis_id}",
                params={"budget_id": budget["budget_id"]},
            )
        ),
    }
    return {
        "machine": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "measurement": "in_process_TestClient_warm_artifact_cache_not_assumed",
        },
        "replications_R": frontier["replications_R"],
        "sampling_rounds_J": frontier["sampling_rounds_J"],
        "display_export_checks": {
            "released_task_count": operation_summary["released_task_count"],
            "exported_released_task_count": exported_released,
            "portfolio_id": point["portfolio_id"],
            "utility_mean": point["utility_mean"],
            "sample_round_id": sample["round_id"],
            "sample_utility": sample["utility"],
            "sample_total_exposure_s": sample["total_exposure_s"],
            "matrix_total_exposure_s": sum(row["value"] for row in sample_matrix["values"]),
        },
        "timings": timings,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("simulation_id")
    parser.add_argument("exposure_id")
    parser.add_argument("analysis_id")
    args = parser.parse_args()
    print(
        json.dumps(
            report(args.root, args.simulation_id, args.exposure_id, args.analysis_id),
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
