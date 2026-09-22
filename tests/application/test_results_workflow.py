from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from mobile_sensing.api import create_app
from mobile_sensing.api.models import MatrixQueryRequest
from mobile_sensing.api.queries import ArtifactCatalog, query_matrix, query_table
from mobile_sensing.application import HeadlessApplication
from mobile_sensing.contracts import ExecutionOptions, PortfolioConfig, VehicleKey
from mobile_sensing.jobs import JobStoreLimits
from mobile_sensing.portfolio import PortfolioAnalysisArtifactReader
from tests.exposure.test_exposure_storage import _artifact_setup
from tests.application.test_headless_application import _bundle, _prepared, _upload_inputs
from tests.portfolio.test_portfolio_sampling import (
    _portfolio_config,
    _two_vehicle_exposure,
    _uniform_weights,
)
from tests.portfolio.test_portfolio_analysis import _anticorrelated_exposure


def _analysis(tmp_path):
    exposure, _ = _two_vehicle_exposure(tmp_path)
    config = _portfolio_config(
        exposure,
        {"fleet": (0, 1, 2)},
        sampling_rounds=8,
        budgets=(0, 10, 20),
    )
    application = HeadlessApplication(tmp_path)
    samples = application.evaluate_portfolio_samples(exposure, config, _uniform_weights()).reference
    analysis = application.summarize_portfolios(samples, config).reference
    return analysis


def test_frontier_projects_saved_fleet_costs_in_display_units(tmp_path) -> None:
    exposure = _anticorrelated_exposure(tmp_path)
    payload = _portfolio_config(
        exposure, {"fleet_a": (0, 1), "fleet_b": (0, 1)}, sampling_rounds=2
    ).model_dump(mode="json")
    payload["costs"]["by_fleet_minor"] = {"fleet_a": 7, "fleet_b": 13}
    config = PortfolioConfig.model_validate_json(json.dumps(payload))
    application = HeadlessApplication(tmp_path)
    samples = application.evaluate_portfolio_samples(exposure, config, _uniform_weights()).reference
    analysis = application.summarize_portfolios(samples, config).reference
    budget = (
        PortfolioAnalysisArtifactReader(tmp_path, analysis).read("budget_levels").to_pylist()[0]
    )
    response = TestClient(create_app(tmp_path)).get(
        f"/api/v1/portfolio-frontiers/{analysis.artifact_id}",
        params={"budget_id": budget["budget_id"]},
    )
    assert response.status_code == 200, response.text
    points = response.json()["points"]
    both = next(row for row in points if row["count_by_fleet"] == {"fleet_a": 1, "fleet_b": 1})
    assert both["cost_by_fleet"] == {"fleet_a": 0.07, "fleet_b": 0.13}
    assert both["total_cost"] == 0.20
    empty = next(row for row in points if row["count_by_fleet"] == {"fleet_a": 0, "fleet_b": 0})
    assert empty["cost_by_fleet"] == {"fleet_a": 0.0, "fleet_b": 0.0}
    assert empty["total_cost"] == 0.0

    filtered = TestClient(create_app(tmp_path)).get(
        f"/api/v1/portfolio-frontiers/{analysis.artifact_id}",
        params=[("budget_id", budget["budget_id"]), ("fleet_id", "fleet_a")],
    )
    assert filtered.status_code == 200, filtered.text
    filtered_payload = filtered.json()
    assert filtered_payload["available_fleet_ids"] == ["fleet_a", "fleet_b"]
    assert filtered_payload["selected_fleet_ids"] == ["fleet_a"]
    assert filtered_payload["best_mean_portfolio_id"] in {
        row["portfolio_id"] for row in filtered_payload["points"]
    }
    assert all(row["count_by_fleet"]["fleet_b"] == 0 for row in filtered_payload["points"])
    assert filtered_payload["frontier_portfolio_count"] == sum(
        bool(row["nondominated"]) for row in filtered_payload["points"]
    )

    invalid = TestClient(create_app(tmp_path)).get(
        f"/api/v1/portfolio-frontiers/{analysis.artifact_id}",
        params=[("budget_id", budget["budget_id"]), ("fleet_id", "missing")],
    )
    assert invalid.status_code == 422

    series = TestClient(create_app(tmp_path)).get(
        f"/api/v1/portfolio-budget-series/{analysis.artifact_id}",
        params=[("fleet_id", "fleet_a")],
    )
    assert series.status_code == 200, series.text
    series_payload = series.json()
    assert series_payload["selected_fleet_ids"] == ["fleet_a"]
    assert series_payload["rows"]
    assert all(
        row["portfolio"]["count_by_fleet"]["fleet_b"] == 0
        and 0
        <= row["coverage_p05_fraction"]
        <= row["coverage_p50_fraction"]
        <= row["coverage_p95_fraction"]
        <= 1
        for row in series_payload["rows"]
    )


def test_operation_table_time_filters_are_half_open_and_cursor_bound(tmp_path) -> None:
    _, simulation, exposure, _, _, _, _ = _artifact_setup(tmp_path)
    catalog = ArtifactCatalog(tmp_path)
    limits = JobStoreLimits()
    included = query_table(
        catalog,
        simulation.artifact_id,
        "movements",
        filters={"replication_id": "r1"},
        page_size=100,
        cursor=None,
        limits=limits,
        time_start_s=5.0,
        time_end_s=10.0,
    )
    assert included.total_matching_count == 1
    excluded = query_table(
        catalog,
        simulation.artifact_id,
        "movements",
        filters={"replication_id": "r1"},
        page_size=100,
        cursor=None,
        limits=limits,
        time_start_s=10.0,
        time_end_s=20.0,
    )
    assert excluded.total_matching_count == 0
    with pytest.raises(ValueError, match="less than"):
        query_table(
            catalog,
            simulation.artifact_id,
            "movements",
            filters={},
            page_size=100,
            cursor=None,
            limits=limits,
            time_start_s=10.0,
            time_end_s=10.0,
        )
    matrix = query_matrix(
        tmp_path,
        MatrixQueryRequest(
            resource_id=exposure.artifact_id,
            kind="vehicle_exposure",
            replication_ids=("r1",),
            vehicle_keys=(VehicleKey(fleet_id="fleet_a", vehicle_id="v1"),),
            statistic="realization",
        ),
        limits,
    )
    assert matrix["selected_replication_count"] == 1
    assert matrix["replications_R"] == 3
    assert matrix["sampling_rounds_J"] is None


def test_browser_json_matrix_arrays_cross_http_boundary(tmp_path) -> None:
    exposure, _ = _two_vehicle_exposure(tmp_path)
    response = TestClient(create_app(tmp_path)).post(
        "/api/v1/matrix-queries",
        json={
            "resource_id": exposure.artifact_id,
            "kind": "vehicle_exposure",
            "replication_ids": ["r1"],
            "vehicle_keys": [{"fleet_id": "fleet", "vehicle_id": "inactive"}],
            "cell_ids": [],
            "time_bin_ids": [],
            "statistic": "realization",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["overall_value"] == 0.0


def test_real_operation_map_uses_filtered_recorded_directed_movements(tmp_path) -> None:
    application, environment = _prepared(tmp_path)
    demand, supply, locations = _upload_inputs(application, environment, tmp_path)
    validated = application.validate_scenario(
        environment, _bundle(environment, demand, supply, locations)
    )
    simulation = application.run_simulation(
        validated,
        ExecutionOptions(
            workers=1,
            memory_limit_bytes=16 * 1024 * 1024,
            progress_frequency_events=2,
        ),
    ).reference
    client = TestClient(create_app(application.artifact_root))
    grid_response = client.get(f"/api/v1/maps/{environment.reference.artifact_id}/grid")
    assert grid_response.status_code == 200, grid_response.text
    assert grid_response.json()["returned_count"] > 0
    replication_id = validated.replications[0].replication_id
    fleet_id = validated.vehicle_specs[0].key.fleet_id
    response = client.get(
        f"/api/v1/maps/{simulation.artifact_id}/movements",
        params={"replication_id": replication_id, "fleet_id": fleet_id},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["is_complete"] and payload["returned_count"] > 0
    assert payload["filters"]["replication_id"] == replication_id
    assert all(
        feature["properties"]["replication_id"] == replication_id
        and feature["properties"]["fleet_id"] == fleet_id
        for feature in payload["features"]
    )
    assert all(feature["geometry"]["type"] == "LineString" for feature in payload["features"])
    summary = client.get(
        f"/api/v1/operation-summaries/{simulation.artifact_id}",
        params={"replication_id": replication_id, "fleet_id": fleet_id},
    )
    assert summary.status_code == 200, summary.text
    summary_payload = summary.json()
    _, outcomes = ArtifactCatalog(application.artifact_root).table(
        simulation.artifact_id, "task_outcomes"
    )
    exported = [
        row
        for row in outcomes.to_pylist()
        if row["replication_id"] == replication_id and row["fleet_id"] == fleet_id
    ]
    assert summary_payload["released_task_count"] == len(exported)
    assert summary_payload["assignment_wait_denominator"] == sum(
        row["assignment_wait_s"] is not None for row in exported
    )
    assert summary_payload["carry_in_task_count"] == 0
    assert summary_payload["complete"]
    mean_map = client.get(
        f"/api/v1/maps/{simulation.artifact_id}/movements",
        params={"aggregation": "mean_edge_usage", "fleet_id": fleet_id},
    )
    assert mean_map.status_code == 200, mean_map.text
    _, all_movements = ArtifactCatalog(application.artifact_root).table(
        simulation.artifact_id, "movements"
    )
    expected_duration = sum(
        row["end_s"] - row["start_s"]
        for row in all_movements.to_pylist()
        if row["fleet_id"] == fleet_id
    ) / len(validated.replications)
    assert sum(
        feature["properties"]["duration_s"] for feature in mean_map.json()["features"]
    ) == pytest.approx(expected_duration)
    assert mean_map.json()["replications_R"] == len(validated.replications)
    assert (
        client.get(
            f"/api/v1/maps/{simulation.artifact_id}/movements",
            params={"aggregation": "mean_edge_usage", "replication_id": replication_id},
        ).status_code
        == 422
    )
    limited = TestClient(
        create_app(application.artifact_root, limits=JobStoreLimits(max_map_features=1))
    ).get(
        f"/api/v1/maps/{simulation.artifact_id}/movements",
        params={"replication_id": replication_id},
    )
    assert limited.status_code == 413
    assert limited.json()["code"] == "QUERY_LIMIT_EXCEEDED"
    assert client.get(f"/api/v1/maps/{simulation.artifact_id}/movements").status_code == 422
    assert (
        client.get(
            f"/api/v1/operation-summaries/{simulation.artifact_id}",
            params={"replication_id": "missing"},
        ).status_code
        == 404
    )


def test_result_queries_match_portfolio_exports_and_preserve_R_J_and_covariance(
    tmp_path,
) -> None:
    analysis = _analysis(tmp_path)
    reader = PortfolioAnalysisArtifactReader(tmp_path, analysis)
    expected_statistics = reader.read("portfolio_statistics").to_pylist()
    client = TestClient(create_app(tmp_path))
    statistics = client.get(
        f"/api/v1/results/{analysis.artifact_id}/portfolio_statistics",
        params={"page_size": 100},
    )
    assert statistics.status_code == 200
    assert statistics.json()["items"] == expected_statistics
    metadata = reader.read("portfolio_analysis_metadata").to_pylist()[0]
    sample_manifest = client.get(f"/api/v1/portfolio-samples/{reader.sample_reference.artifact_id}")
    assert sample_manifest.status_code == 200
    assert sample_manifest.json()["artifact_id"] == reader.sample_reference.artifact_id
    budget = reader.read("budget_levels").to_pylist()[-1]
    frontier = client.get(
        f"/api/v1/portfolio-frontiers/{analysis.artifact_id}",
        params={"budget_id": budget["budget_id"]},
    )
    assert frontier.status_code == 200, frontier.text
    frontier_payload = frontier.json()
    expected_members = {
        row["portfolio_id"]
        for row in reader.read("budget_frontiers").to_pylist()
        if row["budget_id"] == budget["budget_id"]
    }
    assert {row["portfolio_id"] for row in frontier_payload["points"]} == expected_members
    assert (frontier_payload["replications_R"], frontier_payload["sampling_rounds_J"]) == (
        metadata["replications_R"],
        metadata["sampling_rounds_J"],
    )
    selected = next(
        row for row in expected_statistics if json.loads(row["count_by_fleet_json"])["fleet"] == 1
    )
    samples = client.get(
        f"/api/v1/results/{reader.sample_reference.artifact_id}/portfolio_samples",
        params={"portfolio_id": selected["portfolio_id"], "page_size": 100},
    ).json()["items"]
    assert len(samples) == 8
    sample = samples[0]
    selection = client.get(
        f"/api/v1/results/{reader.sample_reference.artifact_id}/sample_selection",
        params={"sample_id": sample["sample_id"], "page_size": 100},
    ).json()["items"]
    assert len(selection) == 1
    matrix = client.post(
        "/api/v1/matrix-queries",
        json={
            "resource_id": reader.sample_reference.artifact_id,
            "kind": "portfolio_sample",
            "portfolio_id": selected["portfolio_id"],
            "round_id": sample["round_id"],
            "statistic": "realization",
        },
    ).json()
    assert (matrix["replications_R"], matrix["sampling_rounds_J"]) == (1, 8)
    assert sum(row["value"] for row in matrix["values"]) == pytest.approx(
        sample["total_exposure_s"]
    )

    covariance_exposure = _anticorrelated_exposure(tmp_path / "covariance")
    # Use the actual two-fleet covariance artifact catalog, not marginal standard deviations.
    covariance_catalog = ArtifactCatalog(tmp_path / "covariance").table(
        covariance_exposure.artifact_id, "vehicle_catalog"
    )[1]
    keys = tuple(
        VehicleKey(fleet_id=row["fleet_id"], vehicle_id=row["vehicle_id"])
        for row in covariance_catalog.to_pylist()
    )
    aggregate = query_matrix(
        tmp_path / "covariance",
        MatrixQueryRequest(
            resource_id=covariance_exposure.artifact_id,
            kind="operational_aggregate",
            vehicle_keys=keys,
            statistic="std",
        ),
        JobStoreLimits(),
    )
    assert aggregate["replications_R"] == 2
    assert aggregate["sampling_rounds_J"] is None
    assert aggregate["values"] == []
    assert all(row["value"] == pytest.approx(0.0) for row in aggregate["time_summary"])
    assert aggregate["overall_value"] == pytest.approx(0.0)
