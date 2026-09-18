import pytest

from mobile_sensing.application.project_models import (
    ProjectConfig,
    FleetEditor,
    DemandEditor,
    SupplyEditor,
    SimulationEditor,
    PortfolioEditor,
    PortfolioFleetEditor,
)
from mobile_sensing.application.run_models import RunOptions
from mobile_sensing.application.run_pipeline import run_project, run_analysis
from mobile_sensing.application.services import HeadlessApplication
from mobile_sensing.portfolio import PortfolioArtifactReader, PortfolioAnalysisArtifactReader
from mobile_sensing.portfolio.reconstruction import ReconstructedMatrices
from mobile_sensing.portfolio.models import UtilityWeightResource, CellWeight
from tests.application.test_authoring import environment
from tests.environment.test_environment_editor import Cancellation
from tests.support.environment_fixtures import RecordedProgress
from tests.portfolio.test_portfolio_sampling import (
    _two_vehicle_exposure,
    _portfolio_config,
    _uniform_weights,
)


def config_fixture(tmp_path):
    root, editor, prepared = environment(tmp_path)
    config = ProjectConfig(
        environment=editor,
        prepared_environment=prepared,
        fleets=(
            FleetEditor(
                fleet_id="f",
                name="F",
                demand=DemandEditor(task_volume=4),
                supply=SupplyEditor(fleet_size=2),
            ),
        ),
        simulation=SimulationEditor(replications=2),
    )
    return root, config


def test_reconstruct_storage_matches_materialized_nonlinear_samples_and_statistics(tmp_path):
    exposure, _ = _two_vehicle_exposure(tmp_path)
    config = _portfolio_config(exposure, {"fleet": (0, 1, 2)}, sampling_rounds=8)
    reconstructed = config.model_copy(update={"sample_matrix_storage": "reconstruct"})
    application = HeadlessApplication(tmp_path)
    legacy = application.evaluate_portfolio_samples(exposure, config, _uniform_weights())
    current = application.evaluate_portfolio_samples(exposure, reconstructed, _uniform_weights())
    left, right = [
        PortfolioArtifactReader(tmp_path, result.reference) for result in (legacy, current)
    ]
    for table in ("portfolio_samples", "sample_selection", "sampling_rounds", "sample_matrices"):
        assert left.read(table).to_pylist() == right.read(table).to_pylist()
    assert right.read("sample_exposure").num_rows == 0
    matrices = ReconstructedMatrices(tmp_path, right)
    for row in left.read("sample_exposure").to_pylist():
        assert matrices[row["matrix_id"]][(row["cell_id"], row["time_bin_id"])] == row["duration_s"]
    a = application.summarize_portfolios(legacy.reference, config)
    b = application.summarize_portfolios(current.reference, reconstructed)
    for table in ("portfolio_statistics", "portfolio_sensing_statistics", "budget_frontiers"):
        assert (
            PortfolioAnalysisArtifactReader(tmp_path, a.reference).read(table).to_pylist()
            == PortfolioAnalysisArtifactReader(tmp_path, b.reference).read(table).to_pylist()
        )


def test_run_rebins_movement_and_portfolio_uses_actual_source(tmp_path, monkeypatch):
    root, config = config_fixture(tmp_path)
    kwargs = dict(options=RunOptions(), cancellation=Cancellation(), progress=RecordedProgress())
    original = run_project(root, config, name="Original day", source_revision_id="r1", **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("Reusable movement invoked mobility")

    monkeypatch.setattr(HeadlessApplication, "run_simulation", forbidden)
    changed = config.model_copy(
        update={
            "simulation": config.simulation.model_copy(update={"temporal_resolution_minutes": 30.0})
        }
    )
    rebinned = run_project(
        root, changed, name="Half-hour reporting", source_revision_id="r2", **kwargs
    )
    assert rebinned.mobility_reused and rebinned.simulation == original.simulation
    assert (
        rebinned.exposure != original.exposure
        and original.config.simulation.temporal_resolution_minutes == 15
    )
    editor = PortfolioEditor(
        source_run_id=original.run_id,
        sampling_runs=4,
        budgets=(0, 0.25, 0.5),
        fleets=(PortfolioFleetEditor(fleet_id="f", counts=(0, 1, 2), unit_cost=0.25),),
    )
    result = run_analysis(root, editor, name="Sensor analysis", source_revision_id="r3", **kwargs)
    assert (
        result.replications == 2
        and result.sampling_runs == 4
        and result.source_run_id == original.run_id
    )
    assert result.count_portfolios == 3
    reader = PortfolioArtifactReader(root, result.samples)
    assert reader.read("sample_exposure").num_rows == 0
    assert (
        next(
            item for item in reader.artifact.manifest.dependencies if item.role == "exposure"
        ).artifact_id
        == original.exposure.artifact_id
    )


def test_composite_http_autosaves_and_finishes_in_worker(tmp_path):
    from fastapi.testclient import TestClient
    from mobile_sensing.api import create_app
    from mobile_sensing.jobs import LocalCoordinator, JobStore

    root, config = config_fixture(tmp_path)
    client = TestClient(create_app(root))
    project = client.post("/api/v1/projects", json={"name": "Example"}).json()
    response = client.post(
        "/api/v1/workbench/runs",
        headers={"X-Project-ID": project["project_id"]},
        json={"name": "Full day", "config": config.model_dump(mode="json")},
    )
    assert response.status_code == 202, response.text
    store = JobStore(root)
    assert store.list_revisions(project["project_id"])[0].payload["schema_version"] == "3.4"
    with LocalCoordinator(root, max_workers=1) as coordinator:
        coordinator.run_once()
    job = store.get_job(response.json()["job_id"])
    assert job.status == "completed", job.error_message
    runs = client.get("/api/v1/workbench/runs", params={"project_id": project["project_id"]})
    assert runs.status_code == 200 and runs.json()[0]["name"] == "Full day"
    curve = client.post("/api/v1/workbench/utility-curve", json={"saturation_minutes": 15}).json()
    assert curve["utility"][10] == pytest.approx(0.99)
    copied = client.post(
        f"/api/v1/workbench/projects/{project['project_id']}/copy",
        json={"config": config.model_dump(mode="json")},
    )
    assert copied.status_code == 201, copied.text
    copy_id = copied.json()["project_id"]
    assert client.delete(f"/api/v1/projects/{project['project_id']}").status_code == 204
    linked = client.get("/api/v1/workbench/runs", params={"project_id": copy_id})
    assert linked.status_code == 200 and linked.json()[0]["run_id"] == runs.json()[0]["run_id"]


def test_factored_weights_and_curve_match_evaluation(tmp_path):
    from mobile_sensing.exposure import ExposureArtifactReader

    exposure, _ = _two_vehicle_exposure(tmp_path)
    config = _portfolio_config(exposure, {"fleet": (2,)}, sampling_rounds=2)
    axes = ExposureArtifactReader(tmp_path).axes(exposure)
    weights = UtilityWeightResource(
        weights_id=config.utility.weights_ref,
        kind="spatial_duration_temporal",
        spatial_values=tuple(
            CellWeight(cell_id=cell, raw_weight=1.0) for cell in sorted(axes["cell_ids"])
        ),
        provenance="Analytic uniform spatial factor",
    )
    application = HeadlessApplication(tmp_path)
    baseline = application.evaluate_portfolio_samples(exposure, config, _uniform_weights())
    factored = application.evaluate_portfolio_samples(exposure, config, weights)
    assert (
        PortfolioArtifactReader(tmp_path, baseline.reference).read("portfolio_samples").to_pylist()
        == PortfolioArtifactReader(tmp_path, factored.reference)
        .read("portfolio_samples")
        .to_pylist()
    )
    for kind in ("linear_capped", "binary"):
        changed = config.model_copy(
            update={"utility": config.utility.model_copy(update={"kind": kind})}
        )
        result = application.evaluate_portfolio_samples(exposure, changed, weights)
        assert all(
            row["utility"] == 1.0
            for row in PortfolioArtifactReader(tmp_path, result.reference)
            .read("portfolio_samples")
            .to_pylist()
        )


def test_explicit_migration_and_retry_preserve_source_revision(tmp_path):
    from mobile_sensing.application.migration import migrate_project
    from fastapi.testclient import TestClient
    from mobile_sensing.api import create_app
    from mobile_sensing.jobs import LocalCoordinator, JobStore

    root, config = config_fixture(tmp_path)
    client = TestClient(create_app(root))
    store = JobStore(root)
    project = store.create_project("Legacy", "")
    payload = {"schema_version": "m10-ui-draft@1", "authoring": config.model_dump(mode="json")}
    old = store.create_revision(project.project_id, None, payload)
    migrated = migrate_project(payload, old.revision_id)
    assert migrated.config.source_revision == old.revision_id and migrated.notices
    assert store.get_revision(project.project_id, old.revision_id).payload == payload

    job, _ = store.submit(
        kind="test_probe", operation="probe", payload={"steps": 1}, project_id=project.project_id
    )
    store.request_cancel(job.job_id)
    assert store.get_job(job.job_id).status == "cancelled"
    assert client.post(f"/api/v1/jobs/{job.job_id}/retry").status_code == 200
    with LocalCoordinator(root, max_workers=1) as coordinator:
        coordinator.run_once()
    assert store.get_job(job.job_id).status == "completed"
    assert store.get_revision(project.project_id, old.revision_id).payload == payload


def test_whole_window_queries_preserve_zeros_and_joint_covariance(tmp_path):
    from mobile_sensing.api.models import MatrixQueryRequest
    from mobile_sensing.api.queries import query_matrix
    from mobile_sensing.jobs import JobStoreLimits
    from tests.portfolio.test_portfolio_analysis import _anticorrelated_exposure

    exposure = _anticorrelated_exposure(tmp_path)
    result = query_matrix(
        tmp_path,
        MatrixQueryRequest(
            resource_id=exposure.artifact_id,
            kind="operational_aggregate",
            statistic="std",
            temporal_aggregation="sum",
        ),
        JobStoreLimits(),
    )
    assert result["overall_value"] == 0.0
    assert all(row["value"] == 0.0 for row in result["time_summary"])
    from mobile_sensing.exposure import ExposureArtifactReader

    fleet_ids = {
        key.fleet_id for key in ExposureArtifactReader(tmp_path).axes(exposure)["vehicle_keys"]
    }
    config = _portfolio_config(
        exposure, {fleet: (0, 1) for fleet in fleet_ids}, sampling_rounds=8
    ).model_copy(update={"sample_matrix_storage": "reconstruct"})
    application = HeadlessApplication(tmp_path)
    samples = application.evaluate_portfolio_samples(exposure, config, _uniform_weights())
    analysis = application.summarize_portfolios(samples.reference, config)
    counts = (
        PortfolioArtifactReader(tmp_path, samples.reference).read("portfolio_counts").to_pylist()
    )
    import json

    portfolio_id = next(
        row["portfolio_id"]
        for row in counts
        if sum(json.loads(row["count_by_fleet_json"]).values()) == 2
    )
    result = query_matrix(
        tmp_path,
        MatrixQueryRequest(
            resource_id=analysis.reference.artifact_id,
            kind="portfolio_summary",
            portfolio_id=portfolio_id,
            statistic="std",
            temporal_aggregation="sum",
        ),
        JobStoreLimits(),
    )
    assert result["overall_value"] == 0.0 and result["sampling_rounds_J"] == 8


def test_window_movement_projection_preserves_geometry_and_duration(tmp_path):
    from shapely.geometry import shape
    from shapely.ops import unary_union
    from mobile_sensing.api.queries import query_map
    from mobile_sensing.jobs import JobStoreLimits
    from mobile_sensing.simulation.storage import SimulationArtifactReader

    from tests.application.test_headless_application import _prepared, _upload_inputs, _bundle
    from mobile_sensing.contracts import ExecutionOptions

    application, prepared = _prepared(tmp_path)
    demand, supply, locations = _upload_inputs(application, prepared, tmp_path)
    validated = application.validate_scenario(
        prepared, _bundle(prepared, demand, supply, locations)
    )
    executed = application.run_simulation(
        validated, ExecutionOptions(memory_limit_bytes=512 * 1024**2)
    )
    root = application.artifact_root
    reader = SimulationArtifactReader(root, executed.reference)
    movements = reader.read_movements().to_dict("records")
    assert movements
    first = movements[0]
    duration = first["end_s"] - first["start_s"]
    kwargs = dict(
        bbox=None,
        replication_id=first["replication_id"],
        fleet_id=None,
        vehicle_id=first["vehicle_id"],
        time_start_s=first["start_s"] + duration / 4,
        time_end_s=first["end_s"] - duration / 4,
        limits=JobStoreLimits(),
    )
    raw = query_map(root, executed.reference.artifact_id, "movements", **kwargs)
    aggregate = query_map(
        root, executed.reference.artifact_id, "movements", aggregation="edge_usage", **kwargs
    )
    assert raw["features"] and aggregate["features"]
    assert unary_union([shape(f["geometry"]) for f in raw["features"]]).equals(
        unary_union([shape(f["geometry"]) for f in aggregate["features"]])
    )
    assert sum(f["properties"]["duration_s"] for f in aggregate["features"]) == pytest.approx(
        duration / 2
    )
