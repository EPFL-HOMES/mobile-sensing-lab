from __future__ import annotations

from fastapi.testclient import TestClient

from mobile_sensing.api import create_app
from mobile_sensing.api.models import MatrixQueryRequest
from mobile_sensing.api.queries import (
    ArtifactCatalog,
    QueryLimitExceeded,
    query_matrix,
    query_table,
)
from mobile_sensing.application import HeadlessApplication
from mobile_sensing.contracts import ExecutionOptions
from mobile_sensing.exposure import ExposureArtifactReader
from mobile_sensing.jobs import JobStore, JobStoreLimits, LocalCoordinator
from mobile_sensing.portfolio import PortfolioAnalysisArtifactReader
from tests.application.test_headless_application import _bundle, _prepared, _upload_inputs
from tests.portfolio.test_portfolio_sampling import (
    _portfolio_config,
    _two_vehicle_exposure,
    _uniform_weights,
)


def _analysis(tmp_path):
    exposure, vehicle_catalog = _two_vehicle_exposure(tmp_path)
    config = _portfolio_config(
        exposure,
        {"fleet": (0, 1, 2)},
        sampling_rounds=8,
        budgets=(0, 10, 20),
    )
    application = HeadlessApplication(tmp_path)
    samples = application.evaluate_portfolio_samples(exposure, config, _uniform_weights()).reference
    analysis = application.summarize_portfolios(samples, config).reference
    return exposure, vehicle_catalog, analysis


def test_bounded_allowlisted_table_and_matrix_queries_preserve_R_and_J(tmp_path) -> None:
    exposure, vehicle_catalog, analysis = _analysis(tmp_path)
    catalog = ArtifactCatalog(tmp_path)
    limits = JobStoreLimits(max_page_size=2)
    page = query_table(
        catalog,
        analysis.artifact_id,
        "portfolio_statistics",
        filters={},
        page_size=2,
        cursor=None,
        limits=limits,
    )
    assert page.returned_count == 2
    assert page.total_matching_count == 3
    assert not page.is_complete and page.next_cursor is not None
    second = query_table(
        catalog,
        analysis.artifact_id,
        "portfolio_statistics",
        filters={},
        page_size=2,
        cursor=page.next_cursor,
        limits=limits,
    )
    assert second.returned_count == 1 and second.is_complete
    try:
        query_table(
            catalog,
            analysis.artifact_id,
            "portfolio_statistics",
            filters={},
            page_size=3,
            cursor=None,
            limits=limits,
        )
    except QueryLimitExceeded:
        pass
    else:
        raise AssertionError("oversized result page was accepted")

    axes = ExposureArtifactReader(tmp_path).axes(exposure)
    realization = query_matrix(
        tmp_path,
        MatrixQueryRequest(
            resource_id=exposure.artifact_id,
            kind="vehicle_exposure",
            replication_ids=("r1",),
            vehicle_keys=(vehicle_catalog.vehicle_keys[1],),
            statistic="realization",
        ),
        JobStoreLimits(),
    )
    assert realization["replications_R"] == 1
    assert realization["sampling_rounds_J"] is None
    assert realization["expected_shape"] == [1, len(axes["cell_ids"]), len(axes["time_bin_ids"])]
    try:
        query_matrix(
            tmp_path,
            MatrixQueryRequest(
                resource_id=exposure.artifact_id,
                kind="vehicle_exposure",
                replication_ids=("r1",),
                vehicle_keys=(vehicle_catalog.vehicle_keys[1],),
                statistic="realization",
            ),
            JobStoreLimits(max_matrix_bytes=256),
        )
    except QueryLimitExceeded:
        pass
    else:
        raise AssertionError("oversized matrix JSON was accepted")

    reader = PortfolioAnalysisArtifactReader(tmp_path, analysis)
    portfolio_id = next(
        row["portfolio_id"]
        for row in reader.read("portfolio_statistics").to_pylist()
        if '"fleet":1' in row["count_by_fleet_json"]
    )
    summary = query_matrix(
        tmp_path,
        MatrixQueryRequest(
            resource_id=analysis.artifact_id,
            kind="portfolio_summary",
            portfolio_id=portfolio_id,
            statistic="std",
        ),
        JobStoreLimits(),
    )
    assert (summary["replications_R"], summary["sampling_rounds_J"]) == (1, 8)
    assert summary["round_id"] is None
    assert summary["unit"] == "s"

    sample = query_matrix(
        tmp_path,
        MatrixQueryRequest(
            resource_id=reader.sample_reference.artifact_id,
            kind="portfolio_sample",
            portfolio_id=portfolio_id,
            round_id=0,
            statistic="realization",
        ),
        JobStoreLimits(),
    )
    assert (sample["replications_R"], sample["sampling_rounds_J"]) == (1, 8)
    assert sample["round_id"] == 0


def test_wgs84_map_limits_and_managed_export_job(tmp_path) -> None:
    application, environment = _prepared(tmp_path)
    artifact_root = application.artifact_root
    client = TestClient(create_app(artifact_root))
    response = client.get(f"/api/v1/maps/{environment.reference.artifact_id}/roads")
    assert response.status_code == 200
    map_value = response.json()
    assert map_value["crs"] == "EPSG:4326" and map_value["is_complete"]
    coordinates = map_value["features"][0]["geometry"]["coordinates"]
    assert -180 <= coordinates[0][0] <= 180
    limited = TestClient(create_app(artifact_root, limits=JobStoreLimits(max_map_features=1)))
    assert limited.get(f"/api/v1/maps/{environment.reference.artifact_id}/roads").status_code == 413

    _, _, analysis = _analysis(artifact_root)
    submitted = client.post(
        "/api/v1/exports",
        json={
            "resource_id": analysis.artifact_id,
            "table": "portfolio_statistics",
            "format": "csv",
        },
    )
    assert submitted.status_code == 202
    job_id = submitted.json()["job_id"]
    with LocalCoordinator(artifact_root) as coordinator:
        assert coordinator.run_once()
    snapshot = JobStore(artifact_root).get_job(job_id)
    assert snapshot.status == "completed"
    download = client.get(f"/api/v1/artifacts/{snapshot.resource_id}/download")
    assert download.status_code == 200
    assert download.headers["content-type"].startswith("text/csv")
    assert b"replications_R" in download.content


def test_coordinator_parallel_simulation_matches_sequential_scientific_result(tmp_path) -> None:
    application, environment = _prepared(tmp_path)
    demand, supply, locations = _upload_inputs(application, environment, tmp_path)
    bundle = _bundle(environment, demand, supply, locations)
    options = ExecutionOptions(
        workers=2,
        memory_limit_bytes=16 * 1024 * 1024,
        progress_frequency_events=2,
    )
    store = JobStore(application.artifact_root)
    job, _ = store.submit(
        kind="simulation",
        operation="simulations",
        payload={
            "environment": environment.reference.model_dump(mode="json"),
            "resources": bundle.model_dump(mode="json"),
            "options": options.model_dump(mode="json"),
        },
    )
    with LocalCoordinator(application.artifact_root, max_workers=2) as coordinator:
        assert coordinator.run_once()
    completed = store.get_job(job.job_id)
    assert completed.status == "completed", (completed.error_code, completed.error_message)
    assert completed.result["replications_R"] == 2
    assert "sampling_rounds_J" not in completed.result

    sequential = application.run_simulation(
        application.validate_scenario(environment, bundle),
        options.model_copy(update={"workers": 1}),
    )
    assert completed.result["artifact"] == sequential.reference.model_dump(mode="json")
    manifest = (
        ArtifactCatalog(application.artifact_root).locate(sequential.reference.artifact_id).manifest
    )
    assert manifest.runtime.worker_count == 2
