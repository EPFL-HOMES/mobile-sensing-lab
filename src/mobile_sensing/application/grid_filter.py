"""Resolve feature and road grid selection without modifying prepared inputs."""

import numpy as np

from mobile_sensing.application.project_models import GridFilter
from mobile_sensing.application.resource_tables import read_table
from mobile_sensing.environment import PreparedEnvironmentReader
from mobile_sensing.exposure.geometry import road_intersecting_grid_cells


def grid_filter_projection(selection):
    """Preserve legacy identities and reuse their shape for representable new rules."""
    selection = GridFilter.model_validate(selection)
    if not selection.rules:
        value = selection.model_dump(mode="json", exclude={"rules"})
        if not selection.require_road_intersection:
            value.pop("require_road_intersection")
        return value
    any_rules = [rule for rule in selection.rules if rule.logic == "any"]
    must_rules = [rule for rule in selection.rules if rule.logic == "must"]
    if len(any_rules) == 1 and (
        any_rules[0].kind == "road_network"
        or any(rule.kind == "spatial_feature" for rule in must_rules)
    ):
        must_rules.extend(any_rules)
        any_rules = []
    any_features = [rule.feature for rule in any_rules if rule.kind == "spatial_feature"]
    must_features = [rule.feature for rule in must_rules if rule.kind == "spatial_feature"]
    if (any_features and must_features) or any(rule.kind == "road_network" for rule in any_rules):
        return {"rules": [rule.model_dump(mode="json") for rule in selection.rules]}
    value = {
        "features": sorted(any_features + must_features),
        "match": "all" if must_features else "any",
    }
    if any(rule.kind == "road_network" for rule in must_rules):
        value["require_road_intersection"] = True
    return value


def select_grid_cells(cell_ids, features, selection, *, road_edges=None, grid_cells=None):
    selection = GridFilter.model_validate(selection)
    cells = tuple(sorted(str(cell) for cell in cell_ids))
    if not selection.enabled:
        return cells
    feature_cells = {}
    if selection.feature_names:
        missing = set(selection.feature_names) - set(features.feature)
        if missing:
            raise ValueError(f"Grid filter features are unavailable: {', '.join(sorted(missing))}")
        selected = features.loc[features.feature.isin(selection.feature_names)]
        values = selected.value.to_numpy(float)
        if not np.isfinite(values).all() or (values < 0).any():
            raise ValueError("Grid filter feature values must be finite and nonnegative")
        if selected.duplicated(["cell_id", "feature"]).any():
            raise ValueError("Grid filter features contain duplicate cell values")
        feature_cells = {
            name: set(group.cell_id.astype(str))
            for name, group in selected.loc[values > 0].groupby("feature")
        }
    road_cells = set()
    if selection.needs_road_geometry:
        if road_edges is None or grid_cells is None:
            raise ValueError("Road grid filtering requires prepared road and grid geometry")
        road_cells = set(road_intersecting_grid_cells(road_edges, grid_cells).cell_id.astype(str))
    retained, any_cells = set(cells), set()
    any_required = False
    for rule in selection.effective_rules:
        matching = (
            road_cells if rule.kind == "road_network" else feature_cells.get(rule.feature, set())
        )
        if rule.logic == "must":
            retained &= matching
        else:
            any_required = True
            any_cells |= matching
    if any_required:
        retained &= any_cells
    result = tuple(cell for cell in cells if cell in retained)
    if not result:
        raise ValueError("Grid filter retains no cells; choose different filter conditions")
    return result


def resolve_grid_filter(root, prepared, selection, *, environment=None, features=None):
    selection = GridFilter.model_validate(selection)
    if features is None:
        features = read_table(root, prepared.features, "grid_features")
    cells = features.loc[features.feature == "uniform", "cell_id"]
    geometry = {}
    if selection.needs_road_geometry:
        if environment is None:
            roads, grid = PreparedEnvironmentReader(root).read_grid_geometry(prepared.artifact)
        else:
            roads, grid = environment.road_edges, environment.grid_cells
        cells = grid.cell_id
        geometry = {"road_edges": roads, "grid_cells": grid}
    return select_grid_cells(cells, features, selection, **geometry)
