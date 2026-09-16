from __future__ import annotations

import json
import math
from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import LineString, box

from mobile_sensing.application import HeadlessApplication
from mobile_sensing.cli import main
from mobile_sensing.contracts import (
    ArtifactDependency,
    ArtifactRef,
    ExposureConfig,
    JointReplicationIdentity,
    PortfolioConfig,
    SeedManifest,
    scientific_hash,
)
from mobile_sensing.exposure import publish_exposure_artifact
from mobile_sensing.portfolio import (
    PortfolioAnalysisArtifactReader,
    build_budget_frontiers,
    quantize_objective,
    summarize_scalar_samples,
)
from mobile_sensing.simulation import publish_simulation_results
from tests.v2.test_m06_exposure_storage import _catalog, _kernel_result
from tests.v2.test_m08a_portfolio_sampling import (
    _portfolio_config,
    _two_vehicle_exposure,
    _uniform_weights,
)


EVIDENCE = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "v2"
    / "fixtures"
    / "release_baselines"
    / "m08b_portfolio_analysis.json"
)


def _analysis_config(config: PortfolioConfig, *, budgets: tuple[int, ...]) -> PortfolioConfig:
    payload = config.model_dump(mode="json")
    payload["budgets"] = {"levels_minor": list(budgets)}
    return PortfolioConfig.model_validate_json(json.dumps(payload))


def _sample_reference(
    tmp_path: Path, exposure: ArtifactRef, config: PortfolioConfig
) -> ArtifactRef:
    return (
        HeadlessApplication(tmp_path)
        .evaluate_portfolio_samples(exposure, config, _uniform_weights())
        .reference
    )


def _anticorrelated_exposure(tmp_path: Path) -> ArtifactRef:
    catalog = _catalog()
    replication_ids = ("r1", "r2")
    results = tuple(
        _kernel_result(replication_id, catalog.vehicle_keys[index])
        for index, replication_id in enumerate(replication_ids)
    )
    replications = tuple(
        JointReplicationIdentity(
            replication_id=replication_id,
            joint_scenario_id=f"joint_{replication_id}",
            scenario_realization_hash=scientific_hash({"replication": replication_id}),
            catalog_hash=catalog.catalog_hash,
        )
        for replication_id in replication_ids
    )
    environment = ArtifactRef(
        artifact_id="environment_m08b_covariance",
        artifact_kind="environment",
        content_hash=scientific_hash({"environment": "m08b_covariance"}),
    )
    simulation = publish_simulation_results(
        artifact_root=tmp_path,
        catalog=catalog,
        replications=replications,
        results=results,
        seed_manifests={
            replication_id: SeedManifest(master_seed=index, streams=())
            for index, replication_id in enumerate(replication_ids, start=1)
        },
        resolved_config={"fixture": "m08b_joint_covariance"},
        dependencies=(
            ArtifactDependency(
                role="environment",
                artifact_id=environment.artifact_id,
                content_hash=environment.content_hash,
            ),
        ),
    )
    road = gpd.GeoDataFrame(
        {
            "edge_id": ["inside"],
            "length_m": [10.0],
            "geometry": [LineString([(0.0, 5.0), (10.0, 5.0)])],
        },
        geometry="geometry",
        crs="EPSG:2056",
    )
    grid = gpd.GeoDataFrame(
        {"cell_id": ["a"], "sensing_geometry": [box(0.0, 0.0, 10.0, 10.0)]},
        geometry="sensing_geometry",
        crs="EPSG:2056",
    )
    return publish_exposure_artifact(
        artifact_root=tmp_path,
        simulation=simulation,
        environment=environment,
        road_edges=road,
        grid_cells=grid,
        working_crs="EPSG:2056",
        config=ExposureConfig(
            schema_version="2.0",
            simulation_id=simulation.artifact_id,
            sensing_geometry_id="grid_m08b_covariance",
            bin_edges_s=(0.0, 10.0, 20.0),
            active_movement_kinds=("service_pickup",),
        ),
    )


def test_known_scalar_statistics_and_decimal_half_even_quantization() -> None:
    summary = summarize_scalar_samples([1.0, 2.0, 4.0])
    assert summary.mean == pytest.approx(7.0 / 3.0)
    assert summary.sample_variance == pytest.approx(7.0 / 3.0)
    assert summary.sample_std == pytest.approx(math.sqrt(7.0 / 3.0))
    assert summary.conditional_mean_se == pytest.approx(math.sqrt(7.0 / 9.0))
    assert (summary.p05, summary.p50, summary.p95) == pytest.approx((1.1, 2.0, 3.8))
    assert quantize_objective(0.5e-12, 1e-12) == 0
    assert quantize_objective(1.5e-12, 1e-12) == 2


def test_nonlinear_J_statistics_sparse_zeros_frontiers_and_complete_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    exposure, _ = _two_vehicle_exposure(tmp_path)
    config = _portfolio_config(
        exposure,
        {"fleet": (0, 1, 2)},
        sampling_rounds=8,
        sampling_seed=19,
        budgets=(0, 10, 20),
    )
    sample_reference = _sample_reference(tmp_path, exposure, config)

    def forbidden_mobility(*args, **kwargs):
        raise AssertionError("M08B invoked mobility")

    monkeypatch.setattr("mobile_sensing.simulation.run_event_kernel", forbidden_mobility)
    result = HeadlessApplication(tmp_path).summarize_portfolios(sample_reference, config)
    reader = PortfolioAnalysisArtifactReader(tmp_path, result.reference)
    statistics = reader.read("portfolio_statistics").to_pylist()
    by_id = {item["portfolio_id"]: item for item in statistics}
    by_count = {json.loads(row["count_by_fleet_json"])["fleet"]: row for row in statistics}
    saturated = -math.expm1(-2.0)
    one = by_count[1]
    assert (one["replications_R"], one["sampling_rounds_J"], one["sample_count"]) == (1, 8, 8)
    assert one["utility_mean"] == pytest.approx(saturated / 2.0)
    assert one["utility_sample_variance"] == pytest.approx(2.0 * saturated**2 / 7.0)
    assert one["utility_sample_std"] == pytest.approx(saturated * math.sqrt(2.0 / 7.0))
    assert one["conditional_mean_se"] == pytest.approx(saturated / math.sqrt(28.0))
    assert (one["utility_p05"], one["utility_p50"], one["utility_p95"]) == pytest.approx(
        (0.0, saturated / 2.0, saturated)
    )
    assert one["utility_mean"] != pytest.approx(-math.expm1(-1.0))

    sensing = reader.read("portfolio_sensing_statistics").to_pylist()
    sensing_by_count = {
        json.loads(by_id[row["portfolio_id"]]["count_by_fleet_json"])["fleet"]: row
        for row in sensing
    }
    assert 0 not in sensing_by_count
    assert sensing_by_count[1]["mean_duration_s"] == pytest.approx(1.0)
    assert sensing_by_count[1]["sample_variance_s2"] == pytest.approx(8.0 / 7.0)
    assert sensing_by_count[1]["nonzero_sample_count"] == 4
    assert sensing_by_count[2]["mean_duration_s"] == pytest.approx(2.0)
    assert sensing_by_count[2]["sample_std_s"] == pytest.approx(0.0)

    metadata = reader.read("portfolio_analysis_metadata").to_pylist()[0]
    assert metadata["replications_R"] == 1
    assert metadata["sampling_rounds_J"] == 8
    assert metadata["absent_sparse_rows_are_zero"]
    assert metadata["variability_interpretation"] == (
        "allocation_variability_conditional_on_one_operational_realization"
    )
    assert "no_confidence_or_global_optimum_claim" in metadata["inference_scope"]

    budgets = {row["budget_minor"]: row for row in reader.read("budget_levels").to_pylist()}
    memberships = reader.read("budget_frontiers").to_pylist()
    assert all(
        (row["replications_R"], row["sampling_rounds_J"]) == (1, 8)
        for row in (*budgets.values(), *memberships)
    )
    assert budgets[0]["feasible_portfolio_count"] == 1
    assert budgets[0]["frontier_portfolio_count"] == 1
    assert budgets[10]["frontier_portfolio_count"] == 2
    frontier_20 = [row for row in memberships if row["budget_minor"] == 20 and row["nondominated"]]
    assert [
        json.loads(by_id[row["portfolio_id"]]["count_by_fleet_json"])["fleet"]
        for row in frontier_20
    ] == [2]
    assert all(row["feasible"] and row["unspent_minor"] >= 0 for row in memberships)

    # The M08B reader is the full export surface: it delegates all immutable M08A lineage.
    assert reader.read("portfolio_samples").num_rows == 24
    assert reader.read("sample_selection").num_rows == 24
    assert reader.read("sample_matrices").num_rows > 0
    assert reader.read("sample_exposure").num_rows > 0

    config_path = tmp_path / "analysis_config.json"
    samples_path = tmp_path / "samples.json"
    config_path.write_text(config.model_dump_json(), encoding="utf-8")
    samples_path.write_text(sample_reference.model_dump_json(), encoding="utf-8")
    assert (
        main(
            [
                "summarize-portfolios",
                "--artifact-root",
                str(tmp_path),
                "--samples",
                str(samples_path),
                "--config",
                str(config_path),
            ]
        )
        == 0
    )
    cli = json.loads(capsys.readouterr().out)
    assert (cli["replications_R"], cli["sampling_rounds_J"]) == (1, 8)
    assert cli["sample_artifact"] == sample_reference.model_dump(mode="json")
    assert json.loads(EVIDENCE.read_text(encoding="utf-8"))["analysis_artifact"] == (
        result.reference.model_dump(mode="json")
    )


def test_joint_replication_covariance_is_preserved(tmp_path: Path) -> None:
    exposure = _anticorrelated_exposure(tmp_path)
    config = _portfolio_config(
        exposure,
        {"fleet_a": (1,), "fleet_b": (1,)},
        sampling_rounds=9,
        budgets=(20,),
    )
    payload = config.model_dump(mode="json")
    payload["utility"]["kind"] = "linear_diagnostic"
    config = PortfolioConfig.model_validate_json(json.dumps(payload))
    samples = _sample_reference(tmp_path, exposure, config)
    analysis = HeadlessApplication(tmp_path).summarize_portfolios(samples, config)
    reader = PortfolioAnalysisArtifactReader(tmp_path, analysis.reference)
    sample_rows = reader.read("portfolio_samples").to_pylist()
    assert {row["total_exposure_s"] for row in sample_rows} == {10.0}
    statistic = reader.read("portfolio_statistics").to_pylist()[0]
    assert statistic["replications_R"] == 2
    assert statistic["sampling_rounds_J"] == 9
    assert statistic["utility_mean"] == pytest.approx(5.0)
    assert statistic["utility_sample_std"] == pytest.approx(0.0)


def test_J_one_disables_frontier_and_uses_null_variance(tmp_path: Path) -> None:
    exposure, _ = _two_vehicle_exposure(tmp_path)
    config = _portfolio_config(exposure, {"fleet": (0, 1)}, sampling_rounds=1, budgets=(10,))
    samples = _sample_reference(tmp_path, exposure, config)
    analysis = HeadlessApplication(tmp_path).summarize_portfolios(samples, config)
    reader = PortfolioAnalysisArtifactReader(tmp_path, analysis.reference)
    for row in reader.read("portfolio_statistics").to_pylist():
        assert row["replications_R"] == 1 and row["sampling_rounds_J"] == 1
        assert row["utility_sample_variance"] is None
        assert row["utility_sample_std"] is None
        assert row["conditional_mean_se"] is None
    budget = reader.read("budget_levels").to_pylist()[0]
    assert not budget["frontier_enabled"]
    assert budget["frontier_portfolio_count"] is None
    assert all(
        row["nondominated"] is None and row["std_comparison_key"] is None
        for row in reader.read("budget_frontiers").to_pylist()
    )


def test_exact_budget_reuse_no_feasible_and_expansion_guard(tmp_path: Path) -> None:
    exposure, _ = _two_vehicle_exposure(tmp_path)
    source = _portfolio_config(exposure, {"fleet": (1, 2)}, sampling_rounds=3, budgets=(20,))
    samples = _sample_reference(tmp_path, exposure, source)
    changed_payload = source.model_dump(mode="json")
    changed_payload["costs"]["by_fleet_minor"] = {"fleet": 7}
    changed_payload["budgets"] = {"levels_minor": [6, 7, 14]}
    changed = PortfolioConfig.model_validate_json(json.dumps(changed_payload))
    analysis = HeadlessApplication(tmp_path).summarize_portfolios(samples, changed)
    reader = PortfolioAnalysisArtifactReader(tmp_path, analysis.reference)
    budget_rows = {row["budget_minor"]: row for row in reader.read("budget_levels").to_pylist()}
    assert budget_rows[6]["feasible_portfolio_count"] == 0
    assert budget_rows[7]["feasible_portfolio_count"] == 1
    assert budget_rows[14]["feasible_portfolio_count"] == 2
    assert not [
        row for row in reader.read("budget_frontiers").to_pylist() if row["budget_minor"] == 6
    ]
    assert reader.sample_reference == samples

    pruned = _portfolio_config(exposure, {"fleet": (0, 1, 2)}, sampling_rounds=3, budgets=(10,))
    pruned_samples = _sample_reference(tmp_path, exposure, pruned)
    expanded = _analysis_config(pruned, budgets=(20,))
    with pytest.raises(ValueError, match="absent from the M08A artifact"):
        HeadlessApplication(tmp_path).summarize_portfolios(pruned_samples, expanded)


def test_tied_dominated_and_tradeoff_frontier_points_are_count_portfolios() -> None:
    rows = (
        {
            "portfolio_id": "count_a",
            "count_by_fleet_json": '{"fleet":1}',
            "total_cost_minor": 10,
            "utility_mean": 1.0,
            "utility_sample_std": 1.0,
        },
        {
            "portfolio_id": "count_b",
            "count_by_fleet_json": '{"fleet":2}',
            "total_cost_minor": 10,
            "utility_mean": 1.0,
            "utility_sample_std": 1.0,
        },
        {
            "portfolio_id": "count_c",
            "count_by_fleet_json": '{"fleet":3}',
            "total_cost_minor": 10,
            "utility_mean": 0.5,
            "utility_sample_std": 2.0,
        },
        {
            "portfolio_id": "count_d",
            "count_by_fleet_json": '{"fleet":4}',
            "total_cost_minor": 10,
            "utility_mean": 2.0,
            "utility_sample_std": 2.0,
        },
    )
    _, memberships = build_budget_frontiers(
        rows,
        budgets=(10,),
        unit="CHF",
        minor_unit_scale=100,
        mean_resolution=1e-12,
        std_resolution=1e-12,
        replications_R=3,
        sampling_rounds_J=7,
        frontier_enabled=True,
    )
    nondominated = {row["portfolio_id"] for row in memberships if row["nondominated"]}
    assert nondominated == {"count_a", "count_b", "count_d"}
    ties = {row["portfolio_id"]: row["tie_group_id"] for row in memberships}
    assert ties["count_a"] == ties["count_b"]
    assert all(row["portfolio_id"].startswith("count_") for row in memberships)
