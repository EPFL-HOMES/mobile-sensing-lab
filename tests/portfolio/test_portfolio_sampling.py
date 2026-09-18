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
    CatalogIdentity,
    ExposureConfig,
    JointReplicationIdentity,
    MovementInterval,
    PortfolioConfig,
    SeedManifest,
    VehicleKey,
    VehicleState,
    VehicleStatus,
    scientific_hash,
    stable_id,
)
from mobile_sensing.exposure import publish_exposure_artifact
from mobile_sensing.portfolio import (
    PortfolioArtifactReader,
    PortfolioResourceLimits,
    UtilityWeightResource,
    build_portfolio_plan,
    draw_sampling_design,
    evaluate_samples,
)
from mobile_sensing.simulation import (
    ExecutionLifecycleStatus,
    ExecutionOutcome,
    KernelResult,
    TaskLifecycleStatus,
    TaskOutcome,
    VehicleOutcome,
    publish_simulation_results,
)
from tests.exposure.test_exposure_storage import _artifact_setup


CRS = "EPSG:2056"
EVIDENCE = (
    Path(__file__).resolve().parents[2]
    / "tests"
    / "fixtures"
    / "release_baselines"
    / "analytic_samples.json"
)


def _portfolio_config(
    exposure: ArtifactRef,
    fleets: dict[str, tuple[int, ...]],
    *,
    sampling_rounds: int,
    sampling_seed: int = 19,
    budgets: tuple[int, ...] = (100,),
) -> PortfolioConfig:
    return PortfolioConfig.model_validate(
        {
            "schema_version": "2.0",
            "exposure_id": exposure.artifact_id,
            "utility": {
                "kind": "exponential_saturation",
                "saturation_s": 1.0,
                "weights_ref": "uniform_weights",
            },
            "count_enumeration": {
                "fleets": {
                    fleet_id: {"count_levels": levels} for fleet_id, levels in fleets.items()
                }
            },
            "budgets": {"levels_minor": budgets},
            "costs": {
                "unit": "CHF",
                "minor_unit_scale": 100,
                "by_fleet_minor": {fleet_id: 10 for fleet_id in fleets},
            },
            "sampling_rounds": sampling_rounds,
            "sampling_seed": sampling_seed,
            "sampling_design": "joint_replication_uniform_vehicle",
            "comparison_resolution": {
                "mean_utility": 1e-12,
                "std_utility": 1e-12,
            },
        }
    )


def _uniform_weights() -> UtilityWeightResource:
    return UtilityWeightResource(
        weights_id="uniform_weights",
        kind="uniform_spatial_duration_temporal",
        provenance="M08A analytic uniform cell and duration weights",
    )


def _two_vehicle_exposure(
    tmp_path: Path, bin_edges_s: tuple[float, ...] = (0.0, 2.0)
) -> tuple[ArtifactRef, CatalogIdentity]:
    keys = (
        VehicleKey(fleet_id="fleet", vehicle_id="inactive"),
        VehicleKey(fleet_id="fleet", vehicle_id="moving"),
    )
    metadata_hash = scientific_hash({"fixture": "m08a-two-vehicle"})
    catalog_hash = scientific_hash(
        {
            "vehicle_keys": [[key.fleet_id, key.vehicle_id] for key in keys],
            "physical_metadata_hash": metadata_hash,
        }
    )
    catalog = CatalogIdentity(
        catalog_id=stable_id("catalog", catalog_hash),
        vehicle_keys=keys,
        physical_metadata_hash=metadata_hash,
        catalog_hash=catalog_hash,
    )
    inactive_state = VehicleState(
        key=keys[0],
        status=VehicleStatus.INACTIVE,
        current_execution_id=None,
        committed_location_id="node",
        available_at_s=0.0,
        remaining_capacity=None,
        execution_generation=0,
    )
    moving_state = VehicleState(
        key=keys[1],
        status=VehicleStatus.OFF_SHIFT,
        current_execution_id=None,
        committed_location_id="node",
        available_at_s=2.0,
        remaining_capacity=None,
        execution_generation=1,
    )
    execution = ExecutionOutcome(
        execution_id="execution",
        vehicle=keys[1],
        fleet_id="fleet",
        task_id="task",
        execution_generation=1,
        assigned_at_s=0.0,
        planned_end_s=2.0,
        realized_end_s=2.0,
        status=ExecutionLifecycleStatus.COMPLETED,
        realized_intervals=(
            MovementInterval(
                kind="movement",
                start_s=0.0,
                end_s=2.0,
                movement_kind="service_pickup",
                edge_id="inside",
                edge_start_fraction=0.0,
                edge_end_fraction=1.0,
            ),
        ),
    )
    result = KernelResult(
        replication_id="r1",
        simulation_start_s=0.0,
        end_s=2.0,
        task_outcomes=(
            TaskOutcome(
                fleet_id="fleet",
                task_id="task",
                kind="service",
                source_policy=None,
                release_s=0.0,
                status=TaskLifecycleStatus.COMPLETED,
                assigned_at_s=0.0,
                terminal_time_s=2.0,
                vehicle_id="moving",
                execution_id="execution",
            ),
        ),
        vehicle_outcomes=(
            VehicleOutcome(
                vehicle=keys[0], state=inactive_state, entered_at_s=None, exited_at_s=None
            ),
            VehicleOutcome(vehicle=keys[1], state=moving_state, entered_at_s=0.0, exited_at_s=2.0),
        ),
        execution_outcomes=(execution,),
        idle_activity_intervals=(),
        lifecycle_events=(),
        processed_heap_events=0,
        maximum_queue_size=0,
    )
    replication = JointReplicationIdentity(
        replication_id="r1",
        joint_scenario_id="joint_r1",
        scenario_realization_hash=scientific_hash({"replication": "r1"}),
        catalog_hash=catalog.catalog_hash,
    )
    environment = ArtifactRef(
        artifact_id="environment_m08a",
        artifact_kind="environment",
        content_hash=scientific_hash({"environment": "m08a"}),
    )
    simulation = publish_simulation_results(
        artifact_root=tmp_path,
        catalog=catalog,
        replications=(replication,),
        results=(result,),
        seed_manifests={"r1": SeedManifest(master_seed=1, streams=())},
        resolved_config={"fixture": "m08a"},
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
            "length_m": [2.0],
            "geometry": [LineString([(0.0, 0.5), (2.0, 0.5)])],
        },
        geometry="geometry",
        crs=CRS,
    )
    grid = gpd.GeoDataFrame(
        {"cell_id": ["cell"], "sensing_geometry": [box(0.0, 0.0, 2.0, 1.0)]},
        geometry="sensing_geometry",
        crs=CRS,
    )
    exposure = publish_exposure_artifact(
        artifact_root=tmp_path,
        simulation=simulation,
        environment=environment,
        road_edges=road,
        grid_cells=grid,
        working_crs=CRS,
        config=ExposureConfig(
            schema_version="2.0",
            simulation_id=simulation.artifact_id,
            sensing_geometry_id="grid_m08a",
            bin_edges_s=bin_edges_s,
            active_movement_kinds=("service_pickup",),
        ),
    )
    return exposure, catalog


def test_analytic_nonlinear_samples_include_inactive_vehicle_and_exact_draws(
    tmp_path: Path,
) -> None:
    exposure, catalog = _two_vehicle_exposure(tmp_path)
    config = _portfolio_config(exposure, {"fleet": (0, 1, 2)}, sampling_rounds=8, sampling_seed=19)
    evaluated = evaluate_samples(
        artifact_root=tmp_path,
        exposure=exposure,
        config=config,
        weights=_uniform_weights(),
    )
    coarse_config = _portfolio_config(
        exposure, {"fleet": (0, 2)}, sampling_rounds=8, sampling_seed=19
    )
    assert (
        draw_sampling_design(
            coarse_config,
            build_portfolio_plan(artifact_root=tmp_path, exposure=exposure, config=coarse_config),
        )
        == evaluated.design
    )
    budget_limited = _portfolio_config(
        exposure,
        {"fleet": (0, 1, 2)},
        sampling_rounds=8,
        sampling_seed=19,
        budgets=(10,),
    )
    budget_plan = build_portfolio_plan(
        artifact_root=tmp_path, exposure=exposure, config=budget_limited
    )
    assert [item.count_by_fleet["fleet"] for item in budget_plan.counts] == [0, 1]
    assert [item.total_cost_minor for item in budget_plan.counts] == [0, 10]
    selected_by_sample: dict[str, tuple[str, ...]] = {}
    for row in evaluated.selection_rows:
        selected_by_sample.setdefault(row["sample_id"], ())
        selected_by_sample[row["sample_id"]] += (row["vehicle_id"],)
    count_by_id = {
        item.portfolio_id: item.count_by_fleet["fleet"] for item in evaluated.plan.counts
    }
    one_vehicle = [row for row in evaluated.sample_rows if count_by_id[row["portfolio_id"]] == 1]
    assert [selected_by_sample[row["sample_id"]] for row in one_vehicle] == [
        ("inactive",),
        ("moving",),
        ("inactive",),
        ("inactive",),
        ("moving",),
        ("moving",),
        ("moving",),
        ("inactive",),
    ]
    saturated = 0.9999
    assert [row["utility"] for row in one_vehicle] == pytest.approx(
        [0.0, saturated, 0.0, 0.0, saturated, saturated, saturated, 0.0]
    )
    assert math.fsum(row["utility"] for row in one_vehicle) / len(one_vehicle) == pytest.approx(
        saturated / 2.0
    )
    assert saturated / 2.0 != pytest.approx(0.99)
    assert any(not rows for rows in evaluated.matrix_rows.values())
    assert len(evaluated.sample_rows) == 3 * 8
    assert len(evaluated.matrix_metadata) == 4
    assert tuple(catalog.vehicle_keys) == evaluated.plan.axes["vehicle_keys"]
    assert len({row["sample_id"] for row in one_vehicle}) == 8
    assert len({row["matrix_id"] for row in one_vehicle}) == 2
    samples_by_round = {
        (count_by_id[row["portfolio_id"]], row["round_id"]): row for row in evaluated.sample_rows
    }
    for round_id in range(8):
        one = set(selected_by_sample[samples_by_round[(1, round_id)]["sample_id"]])
        two = set(selected_by_sample[samples_by_round[(2, round_id)]["sample_id"]])
        assert one < two
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    assert evidence["replications_R"] == 1
    assert evidence["sampling_rounds_J"] == 8
    # Retained historical evidence uses the pre-v4 exponential time scale.
    assert evidence["analytic_one_vehicle_mean"] == pytest.approx(-math.expm1(-2.0) / 2.0)
    assert evidence["utility_of_mean_exposure"] == pytest.approx(-math.expm1(-1.0))
    assert evidence["orderings"] == [
        item.model_dump(mode="json") for item in evaluated.design.orderings
    ]
    published = HeadlessApplication(tmp_path).evaluate_portfolio_samples(
        exposure, config, _uniform_weights()
    )
    assert published.reference.model_dump(mode="json") == evidence["portfolio_artifact_v4"]


def test_sampling_is_joint_prefix_stable_and_independent_of_budget_and_grid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, exposure, _, _, _, _ = _artifact_setup(tmp_path)
    coarse = _portfolio_config(
        exposure,
        {"fleet_a": (0, 1), "fleet_b": (0, 1)},
        sampling_rounds=7,
        budgets=(20,),
    )
    larger_j = coarse.model_copy(update={"sampling_rounds": 11})
    changed_budget_payload = coarse.model_dump(mode="json")
    changed_budget_payload["budgets"] = {"levels_minor": [10, 20]}
    changed_budget = PortfolioConfig.model_validate_json(json.dumps(changed_budget_payload))
    plan = build_portfolio_plan(artifact_root=tmp_path, exposure=exposure, config=coarse)
    design = draw_sampling_design(coarse, plan)
    assert plan.preview.replications_R == 3
    assert plan.preview.sampling_rounds_J == 7
    assert (
        draw_sampling_design(
            larger_j,
            build_portfolio_plan(artifact_root=tmp_path, exposure=exposure, config=larger_j),
        ).rounds[:7]
        == design.rounds
    )
    changed_design = draw_sampling_design(
        changed_budget,
        build_portfolio_plan(artifact_root=tmp_path, exposure=exposure, config=changed_budget),
    )
    assert changed_design == design
    fine_payload = coarse.model_dump(mode="json")
    fine_payload["count_enumeration"]["fleets"] = {
        "fleet_a": {"min_count": 0, "max_count": 1, "step": 1, "include_max": True},
        "fleet_b": {"min_count": 0, "max_count": 1, "step": 1, "include_max": True},
    }
    fine = PortfolioConfig.model_validate_json(json.dumps(fine_payload))
    assert (
        draw_sampling_design(
            fine, build_portfolio_plan(artifact_root=tmp_path, exposure=exposure, config=fine)
        )
        == design
    )

    def forbidden_mobility(*args, **kwargs):
        raise AssertionError("portfolio evaluation invoked the mobility kernel")

    monkeypatch.setattr("mobile_sensing.simulation.run_event_kernel", forbidden_mobility)
    evaluated = evaluate_samples(
        artifact_root=tmp_path,
        exposure=exposure,
        config=coarse,
        weights=_uniform_weights(),
    )
    full_id = next(
        item.portfolio_id
        for item in evaluated.plan.counts
        if item.count_by_fleet == {"fleet_a": 1, "fleet_b": 1}
    )
    full_samples = [row for row in evaluated.sample_rows if row["portfolio_id"] == full_id]
    assert len(full_samples) == 7
    assert all(row["total_exposure_s"] in {0.0, 10.0} for row in full_samples)
    assert [row["selected_joint_replication_id"] for row in full_samples] == [
        item.selected_joint_replication_id for item in design.rounds
    ]


def test_sampling_converges_on_uniform_two_vehicle_allocation_with_declared_bound(
    tmp_path: Path,
) -> None:
    exposure, _ = _two_vehicle_exposure(tmp_path)
    config = _portfolio_config(exposure, {"fleet": (1,)}, sampling_rounds=2_000, sampling_seed=91)
    plan = build_portfolio_plan(artifact_root=tmp_path, exposure=exposure, config=config)
    design = draw_sampling_design(config, plan)
    moving_count = sum(
        permutations["fleet"][0].vehicle_id == "moving" for permutations in design.permutations
    )
    assert abs(moving_count / 2_000 - 0.5) <= 0.035


def test_preview_blocks_resolution_and_storage_before_exposure_matrix_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, exposure, _, _, _, _ = _artifact_setup(tmp_path)
    config = _portfolio_config(
        exposure,
        {"fleet_a": (0, 1), "fleet_b": (0, 1)},
        sampling_rounds=4,
    )
    blocked_plan = build_portfolio_plan(
        artifact_root=tmp_path,
        exposure=exposure,
        config=config,
        limits=PortfolioResourceLimits(max_count_portfolios=3),
    )
    preview = blocked_plan.preview
    assert preview.blocked
    assert preview.feasible_count_portfolios is None
    assert "count portfolio grid exceeds max_count_portfolios" in preview.blocking_reasons
    with pytest.raises(ValueError, match="blocked portfolio plan"):
        draw_sampling_design(config, blocked_plan)

    def forbidden(*args, **kwargs):
        raise AssertionError("blocked portfolio evaluation read sparse exposure rows")

    monkeypatch.setattr(
        "mobile_sensing.exposure.ExposureArtifactReader.read_sparse_batches", forbidden
    )
    with pytest.raises(ValueError, match="max_count_portfolios"):
        evaluate_samples(
            artifact_root=tmp_path,
            exposure=exposure,
            config=config,
            weights=_uniform_weights(),
            limits=PortfolioResourceLimits(max_count_portfolios=3),
        )


def test_portfolio_artifact_and_cli_preserve_R_and_J_and_all_sample_lineage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exposure, _ = _two_vehicle_exposure(tmp_path)
    config = _portfolio_config(exposure, {"fleet": (0, 1, 2)}, sampling_rounds=5, sampling_seed=41)
    reference_path = tmp_path / "exposure.json"
    config_path = tmp_path / "portfolio.json"
    weights_path = tmp_path / "weights.json"
    reference_path.write_text(exposure.model_dump_json(), encoding="utf-8")
    config_path.write_text(config.model_dump_json(), encoding="utf-8")
    weights_path.write_text(_uniform_weights().model_dump_json(), encoding="utf-8")

    assert (
        main(
            [
                "preview-portfolios",
                "--artifact-root",
                str(tmp_path),
                "--exposure",
                str(reference_path),
                "--config",
                str(config_path),
            ]
        )
        == 0
    )
    preview = json.loads(capsys.readouterr().out)
    assert (preview["replications_R"], preview["sampling_rounds_J"]) == (1, 5)

    assert (
        main(
            [
                "evaluate-portfolio-samples",
                "--artifact-root",
                str(tmp_path),
                "--exposure",
                str(reference_path),
                "--config",
                str(config_path),
                "--weights",
                str(weights_path),
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert (payload["replications_R"], payload["sampling_rounds_J"]) == (1, 5)
    assert payload["portfolio_samples"] == 15
    reader = PortfolioArtifactReader(tmp_path, ArtifactRef.model_validate(payload["artifact"]))
    metadata = reader.read("portfolio_metadata").to_pylist()
    assert metadata == [
        {
            "exposure_id": exposure.artifact_id,
            "replications_R": 1,
            "sampling_rounds_J": 5,
            "count_portfolios_P": 3,
            "portfolio_sample_count": 15,
            "unique_matrix_count": payload["unique_sample_matrices"],
            "variability_interpretation": (
                "allocation_variability_conditional_on_one_operational_realization"
            ),
            "complete": True,
        }
    ]
    samples = reader.read("portfolio_samples").to_pylist()
    matrices = reader.read("sample_matrices").to_pylist()
    assert len(samples) == 15
    assert all(row["complete"] for row in matrices)
    assert {row["matrix_id"] for row in samples} == {row["matrix_id"] for row in matrices}
    assert any(row["nonzero_rows"] == 0 for row in matrices)
    assert HeadlessApplication(tmp_path).preview_portfolios(exposure, config).sampling_rounds_J == 5
