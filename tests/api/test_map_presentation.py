"""Map color limits summarize positive query values without clipping the data."""

import math

import pytest

from mobile_sensing.api.map_presentation import color_scale_metadata
from mobile_sensing.api.models import MatrixQueryRequest
from mobile_sensing.api.queries import _bounded_geojson, query_matrix
from mobile_sensing.jobs import JobStoreLimits
from tests.portfolio.test_portfolio_sampling import _two_vehicle_exposure


def test_color_limit_excludes_zero_cells_and_retains_outlier_values():
    values = [0.0] * 1000 + list(range(1, 101)) + [1_000_000]
    original = list(values)
    metadata = color_scale_metadata(iter(values))
    assert metadata == {
        "positive_p95": 96.0,
        "maximum": 1_000_000.0,
        "positive_count": 101,
        "scope": "complete_query",
    }
    assert values == original


@pytest.mark.parametrize("values", [[], [0, -1, math.nan, math.inf, -math.inf, None]])
def test_empty_positive_domain_has_no_percentile(values):
    assert color_scale_metadata(values, scope="returned_features") == {
        "positive_p95": None,
        "maximum": 0.0,
        "positive_count": 0,
        "scope": "returned_features",
    }


def test_bounded_geojson_color_limit_preserves_complete_outlier_feature():
    features = [
        {"type": "Feature", "geometry": None, "properties": {"value": value}}
        for value in [0, *range(1, 101), 1_000_000]
    ]
    result = _bounded_geojson(features, limits=JobStoreLimits(), filters={})
    assert result["color_scale"]["positive_p95"] == 96.0
    assert result["color_scale"]["maximum"] == 1_000_000
    assert result["features"] is features
    assert result["features"][-1]["properties"]["value"] == 1_000_000
    assert result["returned_count"] == result["total_matching_count"] == len(features)
    assert result["is_complete"]


def test_window_color_limit_uses_cell_sum_not_separate_reporting_bins(tmp_path):
    exposure, _ = _two_vehicle_exposure(tmp_path, bin_edges_s=(0.0, 0.5, 2.0))
    result = query_matrix(
        tmp_path,
        MatrixQueryRequest(
            resource_id=exposure.artifact_id,
            kind="operational_aggregate",
            statistic="mean",
            temporal_aggregation="sum",
        ),
        JobStoreLimits(),
    )
    assert [row["value"] for row in result["time_summary"]] == [0.5, 1.5]
    assert [row["value"] for row in result["values"]] == [2.0]
    assert result["color_scale"] == {
        "positive_p95": 2.0,
        "maximum": 2.0,
        "positive_count": 1,
        "scope": "complete_query",
    }
