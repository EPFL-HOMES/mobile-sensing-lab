"""Per-condition Boolean semantics and compatibility with retained grid filters."""

import itertools
import json

import geopandas as gpd
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from shapely.geometry import LineString, box

from mobile_sensing.api import create_app
from mobile_sensing.application.configuration_check import check_configuration
from mobile_sensing.application.grid_filter import grid_filter_projection, select_grid_cells
from mobile_sensing.application.project_models import GridFilter, GridFilterRule
from mobile_sensing.application.project_resolution import resolve_project
from mobile_sensing.application.resource_tables import publish_tables, read_table
from mobile_sensing.application.run_models import RunOptions
from mobile_sensing.application.run_pipeline import mobility_projection, run_project
from mobile_sensing.artifacts import verify_partitioned_artifact
from mobile_sensing.contracts import ArtifactDependency
from mobile_sensing.exposure import ExposureArtifactReader
from tests.application.test_grid_filter import filtered_fixture
from tests.environment.test_environment_editor import Cancellation
from tests.support.environment_fixtures import RecordedProgress


def _feature(name, logic="any"):
    return GridFilterRule(kind="spatial_feature", feature=name, logic=logic)


def _road(logic="any"):
    return GridFilterRule(kind="road_network", logic=logic)


def test_all_condition_assignments_match_boolean_oracle_and_cache_projection():
    names = ("population", "shops", "residential")
    truth = tuple(itertools.product((False, True), repeat=4))
    cell_ids = tuple(f"cell_{index:02d}" for index in range(len(truth)))
    cells = gpd.GeoDataFrame(
        {"cell_id": cell_ids},
        geometry=[box(2 * index, 0, 2 * index + 1, 1) for index in range(len(truth))],
        crs="EPSG:2056",
    )
    lines = [
        LineString([(2 * index + 0.1, 0.5), (2 * index + 0.9, 0.5)])
        for index, values in enumerate(truth)
        if values[-1]
    ]
    # A point-only road contact must not turn the all-false cell into a road cell.
    lines.append(LineString([(-1, 2), (0, 1)]))
    roads = gpd.GeoDataFrame(
        {"edge_id": [f"road_{index}" for index in range(len(lines))]}, geometry=lines, crs=cells.crs
    )
    features = pd.DataFrame(
        [
            (cell, name, float(values[index]))
            for cell, values in zip(cell_ids, truth)
            for index, name in enumerate(names)
        ],
        columns=["cell_id", "feature", "value"],
    )
    geometry = {"road_edges": roads, "grid_cells": cells}
    # Every condition independently takes off/any/must: 81 filter configurations.
    for modes in itertools.product((None, "any", "must"), repeat=4):
        rules = tuple(
            _feature(names[index], logic) if index < 3 else _road(logic)
            for index, logic in enumerate(modes)
            if logic is not None
        )
        selection = GridFilter(rules=rules)
        expected = tuple(
            cell
            for cell, values in zip(cell_ids, truth)
            if all(values[index] for index, mode in enumerate(modes) if mode == "must")
            and (
                "any" not in modes
                or any(values[index] for index, mode in enumerate(modes) if mode == "any")
            )
        )
        actual = select_grid_cells(cell_ids, features, selection, **geometry)
        assert actual == expected, modes
        projected = GridFilter.model_validate_json(json.dumps(grid_filter_projection(selection)))
        assert select_grid_cells(cell_ids, features, projected, **geometry) == expected, modes
        assert grid_filter_projection(GridFilter(rules=tuple(reversed(rules)))) == (
            grid_filter_projection(selection)
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"kind": "spatial_feature"},
        {"kind": "spatial_feature", "feature": ""},
        {"kind": "spatial_feature", "feature": "  "},
        {"kind": "spatial_feature", "feature": "uniform"},
        {"kind": "road_network", "feature": "population"},
    ],
)
def test_invalid_rule_conditions_fail_explicitly(payload):
    with pytest.raises(ValueError):
        GridFilterRule.model_validate(payload)


@pytest.mark.parametrize(
    "rules", [(_feature("population"), _feature("population", "must")), (_road(), _road("must"))]
)
def test_duplicate_conditions_are_rejected_across_logic_groups(rules):
    with pytest.raises(ValueError, match="unique"):
        GridFilter(rules=rules)


@pytest.mark.parametrize(
    "legacy", [{"features": ("population",)}, {"require_road_intersection": True}]
)
def test_new_rules_cannot_mix_with_legacy_conditions(legacy):
    with pytest.raises(ValueError, match="legacy conditions"):
        GridFilter(rules=(_feature("shops"),), **legacy)


@pytest.mark.parametrize("features", [(), ("population",), ("population", "shops")])
@pytest.mark.parametrize("match", ["any", "all"])
@pytest.mark.parametrize("roads", [False, True])
def test_legacy_cache_projection_is_byte_compatible(features, match, roads):
    selection = GridFilter(features=features, match=match, require_road_intersection=roads)
    expected = {"features": list(features), "match": match}
    if roads:
        expected["require_road_intersection"] = True
    assert grid_filter_projection(selection) == expected
    assert selection.feature_names == features
    assert selection.needs_road_geometry == roads
    assert selection.enabled == bool(features or roads)
    assert [rule.logic for rule in selection.effective_rules if rule.kind == "spatial_feature"] == (
        ["any" if match == "any" else "must"] * len(features)
    )


@pytest.mark.parametrize("match", ["any", "all"])
@pytest.mark.parametrize("roads", [False, True])
def test_representable_new_rules_reuse_legacy_cache_shape(match, roads):
    legacy = GridFilter(
        features=("population", "shops"), match=match, require_road_intersection=roads
    )
    current = GridFilter(rules=legacy.effective_rules)
    assert grid_filter_projection(current) == grid_filter_projection(legacy)


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf")])
def test_invalid_selected_feature_values_fail_with_new_rules(value):
    frame = pd.DataFrame([("a", "population", value)], columns=["cell_id", "feature", "value"])
    with pytest.raises(ValueError, match="finite and nonnegative"):
        select_grid_cells(("a",), frame, GridFilter(rules=(_feature("population"),)))


def test_empty_selected_domain_and_missing_road_geometry_fail():
    frame = pd.DataFrame([("a", "population", 0.0)], columns=["cell_id", "feature", "value"])
    with pytest.raises(ValueError, match="retains no cells"):
        select_grid_cells(("a",), frame, GridFilter(rules=(_feature("population", "must"),)))
    with pytest.raises(ValueError, match="prepared road and grid geometry"):
        select_grid_cells(("a",), frame, GridFilter(rules=(_road(), _feature("population"))))


def test_mixed_rules_agree_across_preview_configuration_support_and_exposure(tmp_path):
    root, config, selected = filtered_fixture(tmp_path)
    prepared = config.prepared_environment
    original = read_table(root, prepared.features, "grid_features")
    shops = original.loc[original.feature == "uniform"].assign(
        feature="shops", value=lambda frame: frame.cell_id.eq(selected[1]).astype(float)
    )
    ref = publish_tables(
        root,
        config={"fixture": "mixed-grid-filter"},
        dependencies=[
            ArtifactDependency(
                role="features",
                artifact_id=prepared.features.artifact_id,
                content_hash=prepared.features.content_hash,
            )
        ],
        algorithm="test-grid-features@1",
        frames={"grid_features": pd.concat([original, shops])},
        keys={"grid_features": ("cell_id", "feature")},
    )
    prepared = prepared.model_copy(
        update={"features": ref, "feature_names": ("population", "shops", "uniform")}
    )
    selection = GridFilter(rules=(_feature("population", "must"), _feature("shops"), _road()))
    config = config.model_copy(update={"prepared_environment": prepared, "grid_filter": selection})
    assert "rules" in grid_filter_projection(selection)
    assert config.artifact_payload()["grid_filter"] == selection.model_dump(mode="json")
    kwargs = dict(cancellation=Cancellation(), progress=RecordedProgress())
    assert check_configuration(root, config, **kwargs).validation_level == "configuration"
    missing = config.model_copy(update={"grid_filter": GridFilter(rules=(_feature("missing"),))})
    with pytest.raises(ValueError, match="grid filter feature is absent"):
        check_configuration(root, missing, **kwargs)
    client = TestClient(create_app(root))
    response = client.post(
        "/api/v1/workbench/grid-filter/preview",
        json={
            "prepared_environment": prepared.model_dump(mode="json"),
            "grid_filter": selection.model_dump(mode="json"),
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["retained_cell_count"] == len(selected)
    resolved = resolve_project(root, config, **kwargs)
    assert set(read_table(root, resolved.validated.reference, "spatial_support").cell_id) == set(
        selected
    )
    run = run_project(
        root, config, name="Mixed rules", source_revision_id=None, options=RunOptions(), **kwargs
    )
    assert ExposureArtifactReader(root).axes(run.exposure)["cell_ids"] == selected
    artifact = verify_partitioned_artifact(
        artifact_root=root, collection="datasets", reference=resolved.validated.reference
    )
    assert artifact.manifest.scientific_identity.resolved_config["project"][
        "grid_filter"
    ] == selection.model_dump(mode="json")


def test_legacy_artifact_payload_and_equivalent_mobility_cache_remain_stable(tmp_path):
    _, config, _ = filtered_fixture(tmp_path)
    for selection in (GridFilter(), GridFilter(features=("population",), match="all")):
        legacy = config.model_copy(update={"grid_filter": selection})
        expected = legacy.model_dump(mode="json")
        expected["grid_filter"].pop("rules")
        assert legacy.artifact_payload() == expected
        current = legacy.model_copy(
            update={"grid_filter": GridFilter(rules=selection.effective_rules)}
        )
        assert mobility_projection(current) == mobility_projection(legacy)
