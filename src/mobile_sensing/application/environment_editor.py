"""Resolve named inputs into one metric environment and auditable grid features."""

import json
import math
import re
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

import geopandas as gpd
import numpy as np
import pandas as pd
from pyproj import CRS
from shapely.geometry import box, shape

from mobile_sensing.application.resource_tables import publish_tables
from mobile_sensing.application.studio_models import EnvironmentEditor, EnvironmentResult
from mobile_sensing.contracts import (
    ArtifactDependency,
    EnvironmentBuildConfig,
    EnvironmentProviderRequest,
)
from mobile_sensing.datasets.inputs import load_input
from mobile_sensing.environment import (
    LocalDatasetCatalog,
    LocalDatasetResource,
    PreparedEnvironmentReader,
)
from mobile_sensing.environment.acquisition import acquire_osm, select_feature_category
from mobile_sensing.environment.models import LocalRegionSelection
from mobile_sensing.environment.grid import _check_metric_crs


def read_input(root, identifier, *, role=None):
    metadata, path = load_input(root, identifier)
    if role and metadata["role"] not in role:
        raise ValueError(f"Input {metadata['name']} has role {metadata['role']}; expected {role}")
    if path.suffix == ".csv":
        frame = pd.read_csv(path, dtype={"cell_id": str})
    elif path.suffix == ".parquet":
        try:
            frame = gpd.read_parquet(path)
        except ValueError:
            frame = pd.read_parquet(path)
    else:
        frame = gpd.read_file(path, layer=metadata.get("layer"))
    if isinstance(frame, gpd.GeoDataFrame):
        if frame.crs is None:
            if not metadata.get("source_crs"):
                raise ValueError(
                    f"Confirm the coordinate system for {metadata['name']} before building"
                )
            frame = frame.set_crs(metadata["source_crs"])
    return frame, metadata


def metric_crs(boundary, requested):
    if requested == "auto":
        west, south, east, north = boundary.to_crs(4326).total_bounds
        if east - west > 6 or north - south > 6 or south < -80 or north > 84:
            raise ValueError("This extent requires an explicit suitable metric projection")
        requested = boundary.estimate_utm_crs().to_string()
    _check_metric_crs(requested)
    if CRS(requested).to_epsg() == 3857:
        raise ValueError("Choose a local metric projection for distance and area, not Web Mercator")
    return CRS(requested).to_string()


def parse_osm_speed(value):
    """Only explicit numeric limits are interpreted; ambiguous values use fallback."""
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(mph|km/h|kph)?\s*", str(value))
    if not match:
        return np.nan
    speed = float(match[1]) * (1.609344 if match[2] == "mph" else 1)
    return speed / 3.6 if 0 < speed <= 200 else np.nan


def aggregate_geometry(grid, source, *, name, value_column=None, unit="count"):
    """Sparse spatial intersections; points use the first sorted cell on boundaries."""
    source = source.to_crs(grid.crs).reset_index(drop=True)
    cells = grid.sort_values("cell_id").reset_index(drop=True)
    geometry = cells["sensing_geometry"] if "sensing_geometry" in cells else cells.geometry
    series = gpd.GeoSeries(geometry, crs=grid.crs)
    if source.geometry.isna().any() or (~source.geometry.is_valid).any():
        bad = source.index[source.geometry.isna() | ~source.geometry.is_valid].tolist()[:20]
        raise ValueError(f"Invalid feature geometry at source rows {bad}")
    values = (
        np.ones(len(source))
        if value_column is None
        else pd.to_numeric(source[value_column], errors="raise").to_numpy(float)
    )
    if not np.isfinite(values).all() or (values < 0).any():
        raise ValueError("Feature values must be finite and nonnegative")
    outputs, audits = [], []
    for kinds, suffix, measure in [
        ({"Point", "MultiPoint"}, "count", "point"),
        ({"LineString", "MultiLineString"}, "length_m", "line"),
        ({"Polygon", "MultiPolygon"}, "area_m2", "area"),
    ]:
        chosen = source.loc[source.geom_type.isin(kinds)].explode(index_parts=False)
        if chosen.empty:
            continue
        result = np.zeros(len(cells))
        matches = set()
        for row, geom in zip(chosen.index, chosen.geometry):
            candidates = sorted(series.sindex.query(geom, predicate="intersects"))
            if measure == "point":
                candidates = candidates[:1]
            remaining = geom
            for index in candidates:
                intersection = remaining.intersection(series.iloc[index])
                amount = (
                    1
                    if measure == "point"
                    else intersection.length if measure == "line" else intersection.area
                )
                if amount > 0:
                    if value_column is not None:
                        original = source.geometry.iloc[row]
                        divisor = (
                            (len(original.geoms) if original.geom_type == "MultiPoint" else 1)
                            if measure == "point"
                            else original.length if measure == "line" else original.area
                        )
                        amount /= divisor
                    result[index] += amount * values[row]
                    matches.add(int(row))
                    if measure == "line":
                        remaining = remaining.difference(intersection)
        label = name if value_column is not None else f"{name}:{suffix}"
        output_unit = (
            unit
            if value_column is not None or measure == "point"
            else ("m" if measure == "line" else "m2")
        )
        outputs.append(
            pd.DataFrame(
                {"cell_id": cells.cell_id, "feature": label, "value": result, "unit": output_unit}
            )
        )
        audits.append(
            {
                "feature": label,
                "source_rows": len(chosen),
                "matched_rows": len(matches),
                "unmatched_rows": len(set(chosen.index) - matches),
                "aggregated_value": float(result.sum()),
                "unit": output_unit,
                "rule": "canonical ownership for boundary points/lines; numeric values are source totals allocated by intersection fraction",
            }
        )
    if len(source) and not outputs:
        raise ValueError("Features must contain points, lines, or polygons")
    if value_column is not None and outputs:
        outputs = [
            pd.concat(outputs)
            .groupby(["cell_id", "feature", "unit"], as_index=False)["value"]
            .sum()
        ]
    return outputs, audits


OSM_FEATURE_AGGREGATION = {
    "residential": ("area",),
    "industrial": ("area",),
    "transportation": ("count",),
    "public_services": ("count",),
    "commercial": ("count", "area"),
    "leisure": ("count", "area"),
}


def aggregate_osm_category(grid, source, *, category):
    """Return one normalized, dimensionless grid indicator for one OSM category.

    Object counts use one representative point per OSM object, so polygons are
    not silently omitted. Area is intersected with the sensing grid. Composite
    categories give equal mass to each available normalized component.
    """

    try:
        metrics = OSM_FEATURE_AGGREGATION[category]
    except KeyError as exc:
        raise ValueError(f"Unsupported OSM feature category: {category}") from exc
    source = source.to_crs(grid.crs).reset_index(drop=True)
    components = []
    component_audits = []
    for metric in metrics:
        if metric == "count":
            points = source.copy()
            points.geometry = points.geometry.representative_point()
            tables, audits = aggregate_geometry(grid, points, name=f"{category}:object")
            frame = next(table for table in tables if table.feature.iloc[0].endswith(":count"))
        else:
            polygons = source.loc[source.geom_type.isin({"Polygon", "MultiPolygon"})]
            if polygons.empty:
                continue
            tables, audits = aggregate_geometry(grid, polygons, name=f"{category}:geometry")
            frame = next(table for table in tables if table.feature.iloc[0].endswith(":area_m2"))
        values = frame.value.to_numpy(float)
        total = float(values.sum())
        if total <= 0:
            continue
        components.append(values / total)
        component_audits.append(
            {
                "metric": metric,
                "raw_total": total,
                "source_rows": len(source) if metric == "count" else len(polygons),
                "aggregation": (
                    "representative-point object count"
                    if metric == "count"
                    else "intersected polygon area"
                ),
                "details": audits,
            }
        )
    if not components:
        raise ValueError(f"OSM category {category} has no positive {', '.join(metrics)} values")
    combined = np.mean(np.vstack(components), axis=0)
    combined /= combined.sum()
    cells = grid.sort_values("cell_id").reset_index(drop=True)
    output = pd.DataFrame(
        {
            "cell_id": cells.cell_id,
            "feature": category,
            "value": combined,
            "unit": "dimensionless",
        }
    )
    audit = {
        "feature": category,
        "source_rows": len(source),
        "matched_rows": None,
        "unmatched_rows": None,
        "aggregated_value": float(combined.sum()),
        "unit": "dimensionless",
        "rule": "equal-weight sum of individually normalized OSM components",
        "components": component_audits,
    }
    return output, audit


def build_environment(root, config, *, application, cancellation, progress):
    progress.update(phase="environment.validate", completed=0, total=1)
    config = EnvironmentEditor.model_validate(config)
    ZoneInfo(config.timezone)
    selected = sum(
        bool(value)
        for value in (config.boundary_input, config.region_query.strip(), config.drawn_boundary)
    )
    if selected != 1:
        raise ValueError(
            "Select exactly one study boundary: registered file, region search, or drawn polygon"
        )
    progress.update(phase="environment.validate", completed=1, total=1)
    provenance, assumptions = [], []
    if config.boundary_input:
        boundary_phase = "environment.boundary.local"
        progress.update(phase=boundary_phase, completed=0, total=1)
        boundary, metadata = read_input(root, config.boundary_input, role={"boundary"})
        provenance.append(metadata)
    elif config.drawn_boundary:
        boundary_phase = "environment.boundary.drawn"
        progress.update(phase=boundary_phase, completed=0, total=1)
        geo = config.drawn_boundary
        if geo.get("type") == "FeatureCollection":
            boundary = gpd.GeoDataFrame.from_features(geo["features"], crs=4326)
        else:
            boundary = gpd.GeoDataFrame(geometry=[shape(geo.get("geometry", geo))], crs=4326)
        provenance.append({"drawn_boundary": config.drawn_boundary})
    else:
        boundary_phase = "environment.boundary.osm"
        progress.update(phase=boundary_phase, completed=0, total=1)
        boundary, receipt = acquire_osm(
            root, operation="boundary", query=config.region_query, cancellation=cancellation
        )
        provenance.append(receipt)
    progress.update(phase=boundary_phase, completed=1, total=1)
    if (
        not isinstance(boundary, gpd.GeoDataFrame)
        or boundary.empty
        or not boundary.geom_type.isin(["Polygon", "MultiPolygon"]).all()
    ):
        raise ValueError("Study boundary requires nonempty polygon geometry and a declared CRS")
    progress.update(phase="environment.crs", completed=0, total=1)
    crs = metric_crs(boundary, config.working_crs)
    boundary = boundary.to_crs(crs)
    if not boundary.geometry.is_valid.all():
        raise ValueError("Repair invalid boundary geometry explicitly before building")
    study = boundary.geometry.union_all()
    routing_extent = study.buffer(config.routing_buffer_m)
    progress.update(phase="environment.crs", completed=1, total=1)
    if config.network_input:
        progress.update(phase="environment.network.local", completed=0, total=1)
        roads, metadata = read_input(root, config.network_input, role={"network"})
        provenance.append(metadata)
        # Retain complete supplied road coverage, including routes outside the sensing region.
        roads = roads.to_crs(crs).rename(columns={v: k for k, v in config.road_columns.items()})
        routing_extent = routing_extent.union(box(*roads.total_bounds))
    else:
        progress.update(phase="environment.network.osm", completed=0, total=1)
        polygon = gpd.GeoSeries([routing_extent], crs=crs).to_crs(4326).iloc[0]
        roads, receipt = acquire_osm(
            root,
            operation="network",
            query=config.region_query,
            polygon=polygon,
            cancellation=cancellation,
            progress=progress,
            progress_phase="environment.network.osm",
        )
        roads = roads.to_crs(crs)
        provenance.append(receipt)
    progress.update(
        phase="environment.network.local" if config.network_input else "environment.network.osm",
        completed=1,
        total=1,
    )
    if not {"u", "v", "key"} <= set(roads):
        raise ValueError("Map directed road identity columns u, v, key before building")
    progress.update(phase="environment.speed", completed=0, total=1)
    if config.speed_source == "constant":
        speed = {"kind": "constant_speed", "speed_mps": config.speed_kph / 3.6}
        assumptions.append(f"Uncalibrated uniform road speed: {config.speed_kph} km/h")
    else:
        if config.speed_source == "osm":
            roads["studio_speed_mps"] = roads.get(
                "maxspeed", pd.Series(index=roads.index, dtype=object)
            ).map(parse_osm_speed)
            assumptions.append("OSM maxspeed tags are speed limits, not measured traffic speeds")
        else:
            if not config.speed_input:
                raise ValueError("Select a registered road-speed table")
            speeds, metadata = read_input(root, config.speed_input, role={"speed"})
            provenance.append(metadata)
            if not {"u", "v", "key", config.speed_column} <= set(speeds):
                raise ValueError(
                    "Speed table requires u, v, key and the selected speed column in km/h"
                )
            if speeds.duplicated(["u", "v", "key"]).any():
                raise ValueError("Speed table contains duplicate directed road keys")
            speeds = speeds.copy()
            for key in ("u", "v", "key"):
                roads[key] = roads[key].astype(str)
                speeds[key] = speeds[key].astype(str)
            values = pd.to_numeric(speeds[config.speed_column], errors="raise")
            if ((values.dropna() <= 0) | (values.dropna() > 200)).any():
                raise ValueError("Road speeds must be in km/h and within (0, 200]")
            speeds["studio_speed_mps"] = values / 3.6
            roads = roads.merge(
                speeds[["u", "v", "key", "studio_speed_mps"]],
                on=["u", "v", "key"],
                how="left",
                validate="many_to_one",
            )
        speed = {
            "kind": "edge_speed",
            "field": "studio_speed_mps",
            "fallback_speed_mps": config.speed_kph / 3.6,
        }
        assumptions.append(
            f"Missing or ambiguous speeds use explicit fallback {config.speed_kph} km/h"
        )
    progress.update(phase="environment.speed", completed=1, total=1)
    staging = Path(root) / "environment_sources"
    staging.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=staging) as temporary:
        folder = Path(temporary)
        resources = []

        def save(name, role, data):
            path = folder / f"{name}.parquet"
            data.to_parquet(path, index=False)
            resources.append(LocalDatasetResource(name, role, path, crs))

        save("study", "boundary", boundary)
        save("routing_extent", "boundary", gpd.GeoDataFrame(geometry=[routing_extent], crs=crs))
        save("roads", "network", roads)
        if config.grid_input:
            grid, metadata = read_input(root, config.grid_input, role={"grid"})
            provenance.append(metadata)
            save("grid", "grid", grid.to_crs(crs))
            grid_config = {"kind": "uploaded", "dataset_id": "grid"}
            features = {"grid": "grid"}
        else:
            west, south, east, north = study.bounds
            if (
                math.ceil((east - west) / config.grid_size_m)
                * math.ceil((north - south) / config.grid_size_m)
                > 2_000_000
            ):
                raise ValueError("Grid exceeds two million candidate cells; increase grid size")
            grid_config = {
                "kind": "regular",
                "cell_size_m": config.grid_size_m,
                "origin_easting_m": math.floor(west / config.grid_size_m) * config.grid_size_m,
                "origin_northing_m": math.floor(south / config.grid_size_m) * config.grid_size_m,
            }
            features = {}
        catalog = LocalDatasetCatalog(
            resources,
            network_dataset_id="roads",
            feature_dataset_ids=features,
            region_selections={"routing": LocalRegionSelection("routing_extent")},
        )
        common = {
            "schema_version": "2.0",
            "provider": "environment.local_files@1",
            "boundary": {"kind": "uploaded", "dataset_id": "study"},
            "routing_extent_ref": "routing",
        }
        request = EnvironmentProviderRequest.model_validate_json(
            json.dumps({**common, "target_crs": crs, "requested_features": list(features)})
        )
        build = EnvironmentBuildConfig.model_validate_json(
            json.dumps(
                {
                    **common,
                    "working_crs": crs,
                    "network_source": {
                        "kind": "supplied",
                        "dataset_id": "roads",
                        "topology_policy": config.topology_policy,
                    },
                    "travel_time_profiles": [{"profile_id": "default", "source": speed}],
                    "snapping": {"max_distance_m": config.snap_distance_m},
                    "grid": grid_config,
                }
            )
        )
        progress.update(phase="environment.core", completed=0, total=1)
        reference = application.prepare_environment(
            catalog, request, build, cancellation=cancellation, progress=progress
        )
        progress.update(phase="environment.core", completed=1, total=1)
    environment = PreparedEnvironmentReader(root).read(reference)
    grid = environment.grid_cells
    feature_frames = [
        pd.DataFrame(
            {"cell_id": grid.cell_id, "feature": "uniform", "value": 1.0, "unit": "dimensionless"}
        )
    ]
    audits = []
    names = set()
    feature_total = len(config.features) + len(config.osm_features)
    feature_completed = 0
    for selection in config.features:
        progress.update(
            phase="environment.features.local",
            completed=feature_completed,
            total=feature_total,
        )
        cancellation.raise_if_cancelled()
        if selection.name in names or selection.name == "uniform":
            raise ValueError("Feature names must be unique; uniform is reserved")
        names.add(selection.name)
        data, metadata = read_input(
            root, selection.input_id, role={"population", "feature", "weight"}
        )
        provenance.append(metadata)
        if selection.year is not None:
            if "year" not in data:
                raise ValueError("Selected feature year requires a year column")
            data = data.loc[data.year == selection.year].copy()
        if data.empty:
            raise ValueError(f"No source rows for feature {selection.name}")
        if "cell_id" in data and not isinstance(data, gpd.GeoDataFrame):
            if data.cell_id.duplicated().any():
                raise ValueError(f"Duplicate cell_id in feature {selection.name}")
            values = pd.to_numeric(data[selection.value_column], errors="raise")
            if not np.isfinite(values).all() or (values < 0).any():
                raise ValueError("Feature weights must be finite and nonnegative")
            matched = data.cell_id.isin(grid.cell_id)
            result = grid[["cell_id"]].merge(
                data.assign(value=values)[["cell_id", "value"]], on="cell_id", how="left"
            )
            result["value"] = result.value.fillna(0)
            feature_frames.append(result.assign(feature=selection.name, unit=selection.unit))
            audits.append(
                {
                    "feature": selection.name,
                    "source_rows": len(data),
                    "matched_rows": int(matched.sum()),
                    "unmatched_rows": int((~matched).sum()),
                    "aggregated_value": float(result.value.sum()),
                    "unit": selection.unit,
                    "rule": "cell_id join; missing cells retained with zero",
                }
            )
        else:
            if not isinstance(data, gpd.GeoDataFrame):
                if not metadata.get("source_crs"):
                    raise ValueError(f"Confirm coordinate meaning and CRS for {metadata['name']}")
                x, y = pd.to_numeric(data[selection.x_column]), pd.to_numeric(
                    data[selection.y_column]
                )
                offset = (
                    selection.source_cell_size_m / 2
                    if selection.coordinate_anchor == "lower_left"
                    else 0
                )
                if offset:
                    _check_metric_crs(metadata["source_crs"])
                data = gpd.GeoDataFrame(
                    data,
                    geometry=gpd.points_from_xy(x + offset, y + offset),
                    crs=metadata["source_crs"],
                )
            tables, reports = aggregate_geometry(
                grid,
                data,
                name=selection.name,
                value_column=selection.value_column or None,
                unit=selection.unit,
            )
            feature_frames.extend(tables)
            audits.extend(reports)
        feature_completed += 1
        progress.update(
            phase="environment.features.local",
            completed=feature_completed,
            total=feature_total,
        )
    osm_sources = {}
    if config.osm_features:
        categories = tuple(dict.fromkeys(config.osm_features))
        batch = len(categories) > 1
        acquisition_phase = (
            "environment.features.osm.batch"
            if batch
            else f"environment.features.osm.{categories[0]}"
        )
        progress.update(
            phase=acquisition_phase,
            completed=feature_completed,
            total=feature_total,
        )
        polygon = boundary.to_crs(4326).geometry.union_all()
        data, receipt = acquire_osm(
            root,
            operation="features",
            query=config.region_query,
            polygon=polygon,
            category=categories[0] if not batch else None,
            categories=categories if batch else None,
            cancellation=cancellation,
            progress=progress,
            progress_phase=acquisition_phase,
        )
        provenance.append(receipt)
        osm_sources = {
            category: select_feature_category(data, category) if batch else data
            for category in categories
        }
    for category in config.osm_features:
        data = osm_sources[category]
        table, report = aggregate_osm_category(grid, data, category=category)
        feature_frames.append(table)
        audits.append(report)
        feature_completed += 1
        progress.update(
            phase=f"environment.features.osm.{category}",
            completed=feature_completed,
            total=feature_total,
        )
    feature_frame = pd.concat(feature_frames, ignore_index=True)
    if feature_frame.duplicated(["cell_id", "feature"]).any():
        raise ValueError("Selected inputs produce duplicate feature names")
    feature_config = {
        "editor": config.model_dump(mode="json"),
        "provenance": provenance,
        "audit": audits,
        "working_crs": crs,
        "assumptions": assumptions,
    }
    progress.update(phase="environment.publish_features", completed=0, total=1)
    feature_ref = publish_tables(
        root,
        config=feature_config,
        dependencies=[
            ArtifactDependency(
                role="environment",
                artifact_id=reference.artifact_id,
                content_hash=reference.content_hash,
            )
        ],
        algorithm="grid-features@2",
        frames={"grid_features": feature_frame},
        keys={"grid_features": ("cell_id", "feature")},
    )
    progress.update(phase="environment.publish_features", completed=1, total=1)
    progress.update(phase="environment.complete", completed=1, total=1)
    return EnvironmentResult(
        artifact=reference,
        features=feature_ref,
        feature_names=tuple(sorted(feature_frame.feature.unique())),
        working_crs=crs,
        timezone=config.timezone,
        assumptions=tuple(assumptions),
    )
