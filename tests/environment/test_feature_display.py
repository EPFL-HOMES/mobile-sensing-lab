"""Source geometry and derived-grid map semantics remain distinct."""

import pytest
import geopandas as gpd
from shapely.geometry import Point, MultiPoint, LineString, box
from mobile_sensing.api.workbench_maps import source_feature_preview, feature_preview
from mobile_sensing.application.studio_models import FeatureSelection
from mobile_sensing.datasets.inputs import snapshot_input, InputRegistration
from mobile_sensing.jobs import JobStoreLimits


def test_mixed_source_geometry_values_crs_year_and_limits(tmp_path):
    source = tmp_path / "features.parquet"
    gpd.GeoDataFrame(
        {"value": [2.0, 3.0, 4.0], "year": [2024, 2025, 2025]},
        geometry=[
            Point(500000, 4180000),
            LineString([(500000, 4180000), (500100, 4180100)]),
            box(500000, 4180000, 500100, 4180100),
        ],
        crs=32610,
    ).to_parquet(source)
    item = snapshot_input(
        tmp_path, InputRegistration(path=str(source), name="Mixed features", role="feature")
    )
    selection = FeatureSelection(
        input_id=item["input_id"], name="Mixed", value_column="value", unit="units"
    )
    result = source_feature_preview(tmp_path, selection, JobStoreLimits())
    assert result["crs"] == "EPSG:4326"
    assert result["geometry_types"] == ["LineString", "Point", "Polygon"]
    assert [f["properties"]["value"] for f in result["features"]] == [2.0, 3.0, 4.0]
    assert result["color_scale"] == {
        "positive_p95": pytest.approx(3.9),
        "maximum": 4.0,
        "positive_count": 3,
        "scope": "complete_query",
    }
    assert result["features"][0]["geometry"]["coordinates"][0] == pytest.approx(-123)
    filtered = source_feature_preview(
        tmp_path, selection.model_copy(update={"year": 2025}), JobStoreLimits()
    )
    assert [f["properties"]["source_row"] for f in filtered["features"]] == [1, 2]
    assert filtered["color_scale"]["positive_p95"] == pytest.approx(3.95)
    with pytest.raises(ValueError, match="limit"):
        source_feature_preview(tmp_path, selection, JobStoreLimits(max_map_features=2))


def test_source_color_limit_weights_original_rows_once_before_multipart_explosion(tmp_path):
    source = tmp_path / "multipart.parquet"
    gpd.GeoDataFrame(
        {"value": [10.0, 1000.0]},
        geometry=[
            MultiPoint([(6.6 + index / 10000, 46.5) for index in range(20)]),
            Point(6.7, 46.5),
        ],
        crs=4326,
    ).to_parquet(source)
    item = snapshot_input(
        tmp_path, InputRegistration(path=str(source), name="Multipart feature", role="feature")
    )
    result = source_feature_preview(
        tmp_path,
        FeatureSelection(input_id=item["input_id"], name="Multipart", value_column="value"),
        JobStoreLimits(),
    )
    assert result["source_count"] == 2
    assert result["returned_count"] == 21
    assert result["color_scale"] == {
        "positive_p95": pytest.approx(950.5),
        "maximum": 1000.0,
        "positive_count": 2,
        "scope": "complete_query",
    }
    assert result["features"][-1]["properties"]["value"] == 1000.0


def test_composite_grid_features_follow_environment_dependencies(tmp_path):
    from tests.application.test_runs_analysis import config_fixture
    from mobile_sensing.application.resource_tables import read_table, publish_tables
    from mobile_sensing.contracts import ArtifactDependency

    root, config = config_fixture(tmp_path)
    base = config.prepared_environment.features
    frame = read_table(root, base, "grid_features")
    derived = publish_tables(
        root,
        config={"rule": "identity fixture"},
        dependencies=[
            ArtifactDependency(
                role="base_features", artifact_id=base.artifact_id, content_hash=base.content_hash
            )
        ],
        algorithm="composite-fixture@1",
        frames={"grid_features": frame},
        keys={"grid_features": ("cell_id", "feature")},
    )
    name = frame.feature.iloc[0]
    assert (
        feature_preview(root, derived.artifact_id, name, JobStoreLimits())["features"]
        == feature_preview(root, base.artifact_id, name, JobStoreLimits())["features"]
    )
