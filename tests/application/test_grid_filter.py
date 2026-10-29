import json

import geopandas as gpd
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from shapely.geometry import LineString, box

from mobile_sensing.api import create_app
from mobile_sensing.api.models import MatrixQueryRequest
from mobile_sensing.api.queries import query_matrix
from mobile_sensing.artifacts import verify_partitioned_artifact
from mobile_sensing.application.grid_filter import grid_filter_projection, select_grid_cells
from mobile_sensing.application.project_models import (
    GridFilter,
    PortfolioEditor,
    PortfolioFleetEditor,
)
from mobile_sensing.application.project_resolution import resolve_project
from mobile_sensing.application.resource_tables import publish_tables, read_table
from mobile_sensing.application.run_models import RunOptions
from mobile_sensing.application.run_pipeline import mobility_projection, run_analysis, run_project
from mobile_sensing.contracts import ArtifactDependency
from mobile_sensing.environment import PreparedEnvironmentReader
from mobile_sensing.exposure import ExposureArtifactReader
from mobile_sensing.exposure.geometry import road_intersecting_grid_cells
from mobile_sensing.portfolio import PortfolioArtifactReader
from mobile_sensing.jobs import JobStoreLimits
from tests.application.test_runs_analysis import config_fixture
from tests.environment.test_environment_editor import Cancellation
from tests.support.environment_fixtures import RecordedProgress


def test_positive_feature_filter_any_all_and_invalid_domains():
    frame = pd.DataFrame(
        [
            ("a", "population", 2),
            ("b", "population", 0),
            ("c", "population", 1),
            ("a", "shops", 0),
            ("b", "shops", 3),
            ("c", "shops", 1),
        ],
        columns=["cell_id", "feature", "value"],
    )
    assert select_grid_cells("cba", frame, GridFilter()) == ("a", "b", "c")
    assert select_grid_cells("cba", frame, GridFilter(features=("population",))) == ("a", "c")
    assert select_grid_cells("cba", frame, GridFilter(features=("shops", "population"))) == (
        "a",
        "b",
        "c",
    )
    assert select_grid_cells(
        "cba", frame, GridFilter(features=("shops", "population"), match="all")
    ) == ("c",)
    with pytest.raises(ValueError, match="unavailable"):
        select_grid_cells("abc", frame, GridFilter(features=("missing",)))
    with pytest.raises(ValueError, match="retains no cells"):
        select_grid_cells("b", frame, GridFilter(features=("population",)))
    with pytest.raises(ValueError, match="unique"):
        GridFilter(features=("population", "population"))


def test_road_condition_uses_positive_length_and_intersects_any_all_features():
    cells = gpd.GeoDataFrame(
        {"cell_id": ["a", "b", "c", "touch"]},
        geometry=[box(0, 0, 1, 1), box(1, 0, 2, 1), box(2, 0, 3, 1), box(0, 1, 1, 2)],
        crs="EPSG:2056",
    )
    roads = gpd.GeoDataFrame(
        {"edge_id": ["road"]}, geometry=[LineString([(0, 1), (1.8, 0.5)])], crs=cells.crs
    )
    features = pd.DataFrame(
        [
            ("a", "population", 2),
            ("b", "population", 1),
            ("c", "population", 2),
            ("touch", "population", 1),
            ("a", "shops", 0),
            ("b", "shops", 3),
            ("c", "shops", 2),
            ("touch", "shops", 1),
        ],
        columns=["cell_id", "feature", "value"],
    )
    kwargs = {"road_edges": roads, "grid_cells": cells}
    road_only = GridFilter(require_road_intersection=True)
    assert road_only.enabled and not GridFilter().enabled
    assert select_grid_cells(cells.cell_id, features, road_only, **kwargs) == ("a", "b")
    any_feature = road_only.model_copy(update={"features": ("population", "shops")})
    assert select_grid_cells(cells.cell_id, features, any_feature, **kwargs) == ("a", "b")
    assert select_grid_cells(
        cells.cell_id, features, any_feature.model_copy(update={"match": "all"}), **kwargs
    ) == ("b",)
    with pytest.raises(ValueError, match="prepared road and grid geometry"):
        select_grid_cells(cells.cell_id, features, road_only)
    with pytest.raises(ValueError, match="retains no cells"):
        select_grid_cells(("c", "touch"), features, road_only, **kwargs)
    assert grid_filter_projection(GridFilter(features=("population",))) == {
        "features": ["population"],
        "match": "any",
    }
    assert grid_filter_projection(road_only)["require_road_intersection"] is True


def filtered_fixture(tmp_path):
    root, config = config_fixture(tmp_path)
    prepared = config.prepared_environment
    environment = PreparedEnvironmentReader(root).read(prepared.artifact)
    road_cells = road_intersecting_grid_cells(environment.road_edges, environment.grid_cells)
    selected = tuple(sorted(road_cells.cell_id)[:2])
    original = read_table(root, prepared.features, "grid_features")
    population = original.assign(
        feature="population", value=original.cell_id.isin(selected).astype(float)
    )
    ref = publish_tables(
        root,
        config={"fixture": "grid-filter"},
        dependencies=[
            ArtifactDependency(
                role="environment",
                artifact_id=prepared.artifact.artifact_id,
                content_hash=prepared.artifact.content_hash,
            )
        ],
        algorithm="test-grid-features@1",
        frames={"grid_features": pd.concat([original, population])},
        keys={"grid_features": ("cell_id", "feature")},
    )
    prepared = prepared.model_copy(
        update={"features": ref, "feature_names": ("population", "uniform")}
    )
    return root, config.model_copy(update={"prepared_environment": prepared}), selected


def test_filter_changes_generated_support_exposure_and_portfolio_without_mutating_inputs(tmp_path):
    root, config, selected = filtered_fixture(tmp_path)
    kwargs = dict(cancellation=Cancellation(), progress=RecordedProgress())
    unfiltered = resolve_project(root, config, **kwargs)
    changed = config.model_copy(update={"grid_filter": GridFilter(features=("population",))})
    assert mobility_projection(config) != mobility_projection(changed)
    assert "grid_filter" not in mobility_projection(config)
    filtered = resolve_project(root, changed, **kwargs)
    support = read_table(root, filtered.validated.reference, "spatial_support")
    assert set(support.cell_id) == set(selected)
    assert len(read_table(root, unfiltered.validated.reference, "spatial_support")) > len(support)
    location_ids = set(support.location_id)
    assert all(
        spec.initial_location_id in location_ids for spec in filtered.validated.vehicle_specs
    )
    assert all(
        step.location_id in location_ids
        for rep in filtered.validated.replications
        for task in rep.tasks
        for step in task.steps
    )
    run = run_project(
        root, changed, name="Filtered", source_revision_id=None, options=RunOptions(), **kwargs
    )
    reader = ExposureArtifactReader(root)
    assert reader.axes(run.exposure)["cell_ids"] == selected
    coverage = query_matrix(
        root,
        MatrixQueryRequest(
            resource_id=run.exposure.artifact_id,
            kind="operational_aggregate",
            statistic="mean",
            temporal_aggregation="sum",
        ),
        JobStoreLimits(),
    )
    assert coverage["coverage_denominator_cell_count"] == len(selected)
    artifact = verify_partitioned_artifact(
        artifact_root=root, collection="exposures", reference=run.exposure
    )
    assert artifact.manifest.scientific_identity.resolved_config["exposure"][
        "included_cell_ids"
    ] == list(selected)
    analysis = run_analysis(
        root,
        PortfolioEditor(
            source_run_id=run.run_id,
            sampling_runs=3,
            budgets=(0, 1, 2),
            spatial_weight="population",
            fleets=(PortfolioFleetEditor(fleet_id="f", counts=(0, 1, 2)),),
        ),
        name="Filtered portfolio",
        source_revision_id=None,
        options=RunOptions(),
        **kwargs,
    )
    samples = PortfolioArtifactReader(root, analysis.samples)
    metadata = samples.read("portfolio_metadata").to_pylist()[0]
    assert metadata["exposure_id"] == run.exposure.artifact_id
    assert (
        samples.artifact.manifest.scientific_identity.grid_axis_hash
        == artifact.manifest.scientific_identity.grid_axis_hash
    )
    assert len(
        PreparedEnvironmentReader(root).read(config.prepared_environment.artifact).grid_cells
    ) > len(selected)


@pytest.mark.parametrize("require_roads", [False, True])
def test_preview_and_saved_revision_preserve_selection(tmp_path, monkeypatch, require_roads):
    root, config, selected = filtered_fixture(tmp_path)
    client = TestClient(create_app(root))
    payload = {
        "prepared_environment": config.prepared_environment.model_dump(mode="json"),
        "grid_filter": {
            "features": ["population"],
            "match": "all",
            "require_road_intersection": require_roads,
        },
    }

    def forbidden(*args, **kwargs):
        raise AssertionError("Grid-filter preview constructed routing state")

    monkeypatch.setattr("mobile_sensing.environment.preparation.PreparedRoutingService", forbidden)
    response = client.post("/api/v1/workbench/grid-filter/preview", json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["retained_cell_count"] == len(selected)
    assert response.json()["excluded_cell_count"] > 0
    payload["grid_filter"]["features"] = ["missing"]
    assert client.post("/api/v1/workbench/grid-filter/preview", json=payload).status_code == 422
    changed = config.model_copy(
        update={
            "grid_filter": GridFilter(
                features=("population",), match="all", require_road_intersection=require_roads
            )
        }
    )
    project = client.post("/api/v1/projects", json={"name": "Filter"}).json()
    saved = client.post(
        f"/api/v1/projects/{project['project_id']}/revisions",
        json={"base_revision_id": None, "payload": changed.model_dump(mode="json")},
    )
    assert saved.status_code == 201, saved.text
    assert json.loads(saved.text)["payload"]["grid_filter"] == {
        "rules": [],
        "features": ["population"],
        "match": "all",
        "require_road_intersection": require_roads,
    }


def test_road_only_filter_matches_preview_support_and_exposure(tmp_path):
    root, config = config_fixture(tmp_path)
    prepared = config.prepared_environment
    environment = PreparedEnvironmentReader(root).read(prepared.artifact)
    expected = tuple(
        road_intersecting_grid_cells(environment.road_edges, environment.grid_cells).cell_id
    )
    assert len(expected) < len(environment.grid_cells)
    selection = GridFilter(require_road_intersection=True)
    changed = config.model_copy(update={"grid_filter": selection})
    assert mobility_projection(changed) != mobility_projection(config)
    client = TestClient(create_app(root))
    response = client.post(
        "/api/v1/workbench/grid-filter/preview",
        json={
            "prepared_environment": prepared.model_dump(mode="json"),
            "grid_filter": selection.model_dump(mode="json"),
        },
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "total_cell_count": len(environment.grid_cells),
        "retained_cell_count": len(expected),
        "excluded_cell_count": len(environment.grid_cells) - len(expected),
    }
    kwargs = dict(cancellation=Cancellation(), progress=RecordedProgress())
    unfiltered = resolve_project(root, config, **kwargs)
    filtered = resolve_project(root, changed, **kwargs)
    support = read_table(root, filtered.validated.reference, "spatial_support")
    assert tuple(sorted(support.cell_id)) == expected
    assert len(read_table(root, unfiltered.validated.reference, "spatial_support")) > len(support)
    assert {spec.key for spec in filtered.validated.vehicle_specs} == {
        spec.key for spec in unfiltered.validated.vehicle_specs
    }
    locations = set(support.location_id)
    assert all(spec.initial_location_id in locations for spec in filtered.validated.vehicle_specs)
    assert all(
        step.location_id in locations
        for rep in filtered.validated.replications
        for task in rep.tasks
        for step in task.steps
    )
    run = run_project(
        root, changed, name="Road filter", source_revision_id=None, options=RunOptions(), **kwargs
    )
    assert ExposureArtifactReader(root).axes(run.exposure)["cell_ids"] == expected
    stored_environment = PreparedEnvironmentReader(root).read(prepared.artifact)
    assert len(stored_environment.grid_cells) == len(environment.grid_cells)
    assert tuple(stored_environment.road_edges.edge_id) == tuple(environment.road_edges.edge_id)
