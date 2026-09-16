"""Bounded geographic previews cached by immutable identity, independent of drafts."""

import json
from functools import lru_cache
from mobile_sensing.api.queries import ArtifactCatalog
import geopandas as gpd
import numpy as np
import shapely


def complete_road_display(roads, *, tolerance_m=2.0):
    """Retain the complete network; remove only duplicate display geometry."""
    if roads.crs is None or not roads.crs.is_projected:
        raise ValueError("Prepared road display requires a projected working CRS")
    metres_per_unit = roads.crs.axis_info[0].unit_conversion_factor
    unique = np.unique(shapely.to_wkb(shapely.normalize(roads.geometry.array)))
    lines = shapely.from_wkb(unique)
    # Simplify each edge before merging so all original junction endpoints remain.
    simplified = shapely.simplify(lines, tolerance_m / metres_per_unit, preserve_topology=True)
    merged = shapely.line_merge(shapely.multilinestrings(simplified))
    parts = shapely.get_parts(merged)
    projected = gpd.GeoSeries(parts, crs=roads.crs).to_crs(4326)
    coordinates = [np.round(np.asarray(line.coords)[:, :2], 6).tolist() for line in projected]
    features = [
        {
            "type": "Feature",
            "properties": {"value": 1},
            "geometry": {
                "type": "MultiLineString",
                "coordinates": coordinates[start : start + 128],
            },
        }
        for start in range(0, len(coordinates), 128)
    ]
    return features, len(parts)


def feature_preview(root, feature_id, feature, limits):
    from mobile_sensing.api.queries import _filtered_rows, query_environment, _bounded_geojson
    from mobile_sensing.contracts import EnvironmentArtifactRef

    catalog = ArtifactCatalog(root)
    located = catalog.locate(feature_id)
    if not any(table.name == "grid_features" for table in located.manifest.tables):
        raise ValueError("Select a prepared spatial feature dataset")
    pending, seen, dependency = [located], set(), None
    while pending and dependency is None:
        current = pending.pop()
        if current.manifest.artifact_id in seen:
            continue
        seen.add(current.manifest.artifact_id)
        for item in current.manifest.dependencies:
            if item.artifact_id.startswith("environment_"):
                dependency = item
                break
            if item.artifact_id.startswith("dataset_"):
                pending.append(catalog.locate(item.artifact_id))
    if dependency is None:
        raise ValueError("Feature dataset has no prepared environment dependency")
    environment = query_environment(
        root,
        EnvironmentArtifactRef(
            artifact_id=dependency.artifact_id,
            artifact_kind="environment",
            content_hash=dependency.content_hash,
        ),
    )
    rows = _filtered_rows(
        located,
        "grid_features",
        lambda row: row["feature"] == feature,
        limit=limits.max_map_features,
    )
    if not rows:
        raise ValueError("Selected spatial feature is unavailable")
    units = {row["unit"] for row in rows}
    if len(units) != 1 or len({row["cell_id"] for row in rows}) != len(rows):
        raise ValueError("Feature cells or units are inconsistent")
    values = {row["cell_id"]: row["value"] for row in rows}
    frame = environment.grid_cells[["cell_id", "geometry"]].copy()
    if set(frame.cell_id) != set(values):
        raise ValueError("Feature does not cover the complete prepared grid")
    frame["value"] = frame.cell_id.map(values)
    result = _bounded_geojson(
        geojson(frame)["features"],
        limits=limits,
        filters={"feature": feature, "environment_id": dependency.artifact_id},
    )
    resolved = located.manifest.scientific_identity.resolved_config
    inputs = {row["input_id"]: row for row in resolved.get("provenance", []) if "input_id" in row}
    sources = []
    for row in resolved.get("editor", {}).get("features", []):
        if row.get("name") == feature:
            source = inputs.get(row.get("input_id"), {})
            sources.append(
                {
                    "source": source.get("name", source.get("original_filename", feature)),
                    "input_id": row.get("input_id"),
                    "year": row.get("year"),
                }
            )
    sources.extend(row for row in resolved.get("audit", []) if row.get("feature") == feature)
    if feature == "uniform":
        sources.append({"source": "Uniform: one unit per grid cell"})
    result.update(
        feature=feature,
        unit=next(iter(units)),
        sources=sources,
        zero_cell_count=sum(value == 0 for value in values.values()),
    )
    return result


def geojson(frame):
    value = json.loads(frame.to_crs(4326).to_json(drop_id=True))
    value["crs"] = "EPSG:4326"
    return value


@lru_cache(maxsize=8)
def environment_preview(root, artifact_id, max_map_features=50_000, max_map_bytes=32 * 1024 * 1024):
    from mobile_sensing.api.queries import _bounded_geojson
    from mobile_sensing.jobs import JobStoreLimits

    limits = JobStoreLimits(max_map_features=max_map_features, max_map_bytes=max_map_bytes)
    located = ArtifactCatalog(root).locate(artifact_id)
    if located.reference.artifact_kind != "environment":
        raise ValueError("Select a prepared environment")
    boundary = gpd.read_parquet(located.directory / "boundary.parquet")
    roads = gpd.read_parquet(located.directory / "road_edges.parquet")
    grid = gpd.read_parquet(located.directory / "grid_cells.parquet")
    features, line_count = complete_road_display(roads)
    result = {
        "boundary": geojson(boundary[["geometry"]].assign(value=1)),
        "roads": _bounded_geojson(features, limits=limits, filters={"environment_id": artifact_id}),
        "road_count": len(roads),
        "preview_road_count": len(roads),
        "display_line_count": line_count,
        "simplification_m": 2.0,
        "display_geometry_version": "complete-road-lines@1",
        "cell_count": len(grid),
        "working_crs": grid.crs.to_string(),
    }
    if len(json.dumps(result, separators=(",", ":")).encode()) > max_map_bytes:
        raise ValueError("Complete network display exceeds the configured map byte limit")
    return result


def source_feature_preview(root, selection, limits):
    """Display immutable source geometry, without grid aggregation or normalization."""
    import pandas as pd
    from mobile_sensing.application.environment_editor import read_input
    from mobile_sensing.api.queries import _bounded_geojson, QueryLimitExceeded

    data, metadata = read_input(root, selection.input_id, role={"feature", "population", "weight"})
    data = data.copy()
    source_rows = dict(zip(data.index, range(len(data)), strict=True))
    if selection.year is not None:
        if "year" not in data:
            raise ValueError("Selected source year requires a year column")
        data = data.loc[data.year == selection.year].copy()
    if len(data) > limits.max_map_features:
        raise QueryLimitExceeded("Source feature limit exceeded; select a smaller source layer")
    if not isinstance(data, gpd.GeoDataFrame):
        if not metadata.get("source_crs"):
            raise ValueError("Confirm the source coordinate system in Data before viewing")
        if selection.x_column not in data or selection.y_column not in data:
            raise ValueError("This table has no mapped coordinates; use its prepared grid view")
        x = pd.to_numeric(data[selection.x_column], errors="raise")
        y = pd.to_numeric(data[selection.y_column], errors="raise")
        data = gpd.GeoDataFrame(data, geometry=gpd.points_from_xy(x, y), crs=metadata["source_crs"])
    if (
        data.geometry.isna().any()
        or data.geometry.is_empty.any()
        or not data.geometry.is_valid.all()
    ):
        raise ValueError(
            "Source contains missing, empty or invalid geometries; repair the source explicitly"
        )
    frame = (
        data[[data.geometry.name]].rename_geometry("geometry")
        if data.geometry.name != "geometry"
        else data[["geometry"]].copy()
    )
    frame["source_row"] = [source_rows[index] for index in data.index]
    if selection.value_column:
        if selection.value_column not in data:
            raise ValueError("Selected source value column is unavailable")
        values = pd.to_numeric(data[selection.value_column], errors="raise")
        if not np.isfinite(values).all() or (values < 0).any():
            raise ValueError("Source feature values must be finite and nonnegative")
        frame["value"] = values.to_numpy()
    else:
        frame["value"] = 1.0
    frame = frame.explode(index_parts=False).reset_index(drop=True)
    supported = {"Point", "MultiPoint", "LineString", "MultiLineString", "Polygon", "MultiPolygon"}
    if not frame.geom_type.isin(supported).all():
        raise ValueError("Source contains unsupported geometry components")
    result = _bounded_geojson(
        geojson(frame)["features"],
        limits=limits,
        filters={"input_id": selection.input_id, "year": selection.year},
    )
    result.update(
        unit=selection.unit if selection.value_column else "feature",
        zero_cell_count=int(frame.value.eq(0).sum()),
        sources=[{"source": metadata["name"], "input_id": selection.input_id}],
        source_count=len(data),
        geometry_types=sorted(frame.geom_type.unique().tolist()),
        representation="source",
    )
    return result
