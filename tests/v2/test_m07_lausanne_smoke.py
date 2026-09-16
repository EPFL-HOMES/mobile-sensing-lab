from __future__ import annotations

import json
from pathlib import Path

from mobile_sensing.application import LausanneSmokeConfig, run_lausanne_smoke


ROOT = Path(__file__).resolve().parents[2]


def test_bounded_lausanne_vertical_smoke_writes_complete_operations_and_e(
    lausanne_repaired_environment,
) -> None:
    environment, artifact_root = lausanne_repaired_environment
    config = LausanneSmokeConfig.model_validate_json(
        (ROOT / "docs" / "templates" / "m07" / "lausanne_smoke.json").read_bytes()
    )
    report = run_lausanne_smoke(
        artifact_root=artifact_root,
        data_root=ROOT / "data" / "Lausanne",
        config=config,
        prepared_environment=environment,
    )
    assert report["service_date"] == "2026-01-14"
    assert report["route_ids"] == ["92-13-H-j26-1"]
    assert report["replications_R"] == 2
    assert report["portfolio_sampling_rounds_J"] is None
    assert report["physical_vehicle_count"] == 3
    assert len(report["selected_gtfs_task_ids"]) == 4
    assert report["logical_vehicle_exposure_matrices"] == 6
    assert report["reporting_bin_count"] == 8
    assert report["simulation_output_rows"]["activity_intervals"] > 0
    assert report["simulation_output_rows"]["movements"] > 0
    assert report["simulation_output_rows"]["operational_events"] > 0
    assert report["exposure_output_rows"]["vehicle_catalog"] == 3
    assert report["exposure_output_rows"]["replication_status"] == 2
    assert report["exposure_output_rows"]["edge_grid_pieces"] > 0
    assert "synthetic_demand" in report["assumptions"]
    assert "inferred_vehicle_duties" in report["assumptions"]
    assert "uncalibrated_constant_road_speed_mps=8.33333333333333" in report["assumptions"]
    assert report["population_weight_diagnostics"]["uniform_proxy_used"] is False
    assert report["population_weight_diagnostics"]["positive_weight_location_count"] >= 2
    assert all(
        counts["lausanne_transit"] == 4
        and counts["synthetic_location"] > 0
        and counts["synthetic_od"] > 0
        for counts in report["task_counts_by_replication"].values()
    )

    recorded = json.loads(
        (ROOT / "tests/v2/fixtures/release_baselines/m07_lausanne_smoke.json").read_text()
    )
    for key in (
        "configuration_hash",
        "service_date",
        "route_ids",
        "study_municipalities",
        "routing_extent_municipalities",
        "replications_R",
        "portfolio_sampling_rounds_J",
        "selected_gtfs_vehicle_ids",
        "selected_gtfs_task_ids",
        "physical_vehicle_count",
        "task_counts_by_replication",
        "source_diagnostics",
        "population_weight_diagnostics",
        "assumptions",
        "scenario_impact",
        "artifacts",
        "simulation_output_rows",
        "exposure_output_rows",
        "logical_vehicle_exposure_matrices",
        "grid_cell_count",
        "reporting_bin_count",
    ):
        assert recorded[key] == report[key]
