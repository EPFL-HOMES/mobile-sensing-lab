import pytest
from fastapi.testclient import TestClient

from mobile_sensing.api import create_app
from mobile_sensing.api.fleet_queries import fleet_summary
from mobile_sensing.api.models import MatrixQueryRequest
from mobile_sensing.api.queries import query_matrix
from mobile_sensing.jobs import JobStoreLimits
from tests.exposure.test_exposure_storage import _artifact_setup


def test_mean_coverage_uses_replications_including_zero_not_union(tmp_path):
    _, _, exposure, *_ = _artifact_setup(tmp_path)
    mean = query_matrix(
        tmp_path,
        MatrixQueryRequest(
            resource_id=exposure.artifact_id,
            kind="operational_aggregate",
            statistic="mean",
            temporal_aggregation="sum",
        ),
        JobStoreLimits(),
    )
    assert mean["positive_cell_count"] == 1
    assert mean["mean_coverage_fraction"] == pytest.approx(2 / 3)
    assert mean["coverage_denominator_cell_count"] == 1
    assert mean["coverage_semantics"] == (
        "mean_within_observation_any_time_spatial_coverage_road_intersecting_cells"
    )
    assert mean["overall_value"] == pytest.approx(20 / 3)
    assert mean["replications_R"] == 3
    zero = query_matrix(
        tmp_path,
        MatrixQueryRequest(
            resource_id=exposure.artifact_id,
            kind="operational_aggregate",
            statistic="mean",
            temporal_aggregation="sum",
            time_bin_ids=(mean["source_time_bin_ids"][1],),
        ),
        JobStoreLimits(),
    )
    assert zero["mean_coverage_fraction"] == 0
    assert zero["overall_value"] == 0


def test_mean_fleet_release_cohorts_and_active_time_bins(tmp_path):
    _, _, exposure, *_ = _artifact_setup(tmp_path)
    summary = fleet_summary(tmp_path, exposure.artifact_id, limits=JobStoreLimits())
    assert summary["replications_R"] == 3
    assert sum(row["mean_released_tasks"] for row in summary["fleets"]) == pytest.approx(2 / 3)
    assert summary["time_summary"][0]["mean_arrivals"] == pytest.approx(2 / 3)
    assert summary["time_summary"][1]["mean_arrivals"] == 0
    for row in summary["fleets"]:
        if row["mean_released_tasks"]:
            assert row["mean_completion_rate"] == 1
            assert row["completion_rate_replications"] == 1
        assert row["mean_active_vehicles"] == row["catalog_size"]
    clipped = fleet_summary(
        tmp_path, exposure.artifact_id, time_start_s=10, time_end_s=20, limits=JobStoreLimits()
    )
    assert all(row["mean_released_tasks"] == 0 for row in clipped["fleets"])
    assert all(row["mean_completion_rate"] is None for row in clipped["fleets"])
    assert sum(row["mean_carry_in_tasks"] for row in clipped["fleets"]) == pytest.approx(2 / 3)
    with pytest.raises(ValueError, match="boundaries"):
        fleet_summary(tmp_path, exposure.artifact_id, time_start_s=5, limits=JobStoreLimits())
    response = TestClient(create_app(tmp_path)).get(
        f"/api/v1/fleet-summaries/{exposure.artifact_id}"
    )
    assert response.status_code == 200, response.text
    assert response.json() == summary
