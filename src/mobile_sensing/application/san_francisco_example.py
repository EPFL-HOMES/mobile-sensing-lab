"""OSM-only geography and an explicitly synthetic San Francisco taxi fleet."""

from pathlib import Path
import json

import geopandas as gpd
import pandas as pd
from shapely.geometry import box

from mobile_sensing.application.example_build import demonstration_portfolio
from mobile_sensing.application.environment_editor import build_environment
from mobile_sensing.application.project_models import (
    ProjectConfig,
    FleetEditor,
    DemandEditor,
    SupplyEditor,
    SimulationEditor,
    TemporalInterval,
    ShiftGroup,
    SpatialFeatureWeight,
)
from mobile_sensing.application.studio_models import EnvironmentEditor, FeatureSelection
from mobile_sensing.application.services import HeadlessApplication
from mobile_sensing.application.run_pipeline import run_project, run_analysis
from mobile_sensing.application.run_models import RunOptions, RunView
from mobile_sensing.datasets.inputs import InputRegistration, snapshot_input
from mobile_sensing.environment.acquisition import acquire_osm

EXAMPLE_NAME = "[Example] San Francisco — Taxi Weekday"


def simplify_osm_roads(edges, grid):
    """Contract shape nodes, retaining full geometry and every grid snap coordinate."""
    import networkx as nx
    import numpy as np
    import osmnx as ox
    from scipy.spatial import cKDTree

    if any(len(geometry.coords) != 2 for geometry in edges.geometry):
        raise ValueError("OSM shape contraction requires unsimplified two-point segments")
    graph = nx.MultiDiGraph(crs=edges.crs)
    for row in edges.itertuples():
        for node, xy in ((row.u, row.geometry.coords[0]), (row.v, row.geometry.coords[-1])):
            graph.add_node(node, x=xy[0], y=xy[1])
        graph.add_edge(
            row.u,
            row.v,
            key=row.key,
            one_way=row.one_way,
            maxspeed=row.maxspeed or "",
            highway=row.highway,
            geometry=row.geometry,
        )
    ids = sorted(graph.nodes)
    projected = gpd.GeoSeries(
        gpd.points_from_xy([graph.nodes[n]["x"] for n in ids], [graph.nodes[n]["y"] for n in ids]),
        crs=edges.crs,
    ).to_crs(grid.crs)
    tree = cKDTree(np.column_stack((projected.x, projected.y)))
    centroids = grid.geometry.centroid
    coordinates = np.column_stack((centroids.x, centroids.y))
    distances, _ = tree.query(coordinates)
    # Preserve all distance ties as well as the closest coordinate itself.
    keep = {
        ids[index]
        for candidates in tree.query_ball_point(coordinates, distances + 1e-7)
        for index in candidates
    }
    for node in keep:
        graph.nodes[node]["grid_snap"] = True
    simplified = ox.simplification.simplify_graph(
        graph,
        node_attrs_include=["grid_snap"],
        edge_attrs_differ=["one_way", "maxspeed", "highway"],
        remove_rings=False,
        track_merged=True,
    )
    result = ox.convert.graph_to_gdfs(simplified, nodes=False).reset_index()
    original_length = float(edges.to_crs(grid.crs).length.sum())
    simplified_length = float(result.to_crs(grid.crs).length.sum())
    if abs(original_length - simplified_length) > 1e-6 or not keep <= set(simplified.nodes):
        raise ValueError("OSM contraction failed its geometry/snap-node conservation check")
    result["merged_edges"] = result.get(
        "merged_edges", pd.Series(index=result.index, dtype=object)
    ).map(lambda value: json.dumps(value) if isinstance(value, list) else "")
    report = {
        "algorithm": "osmnx-shape-node-contraction@1",
        "osmnx_version": ox.__version__,
        "raw_nodes": len(graph),
        "simplified_nodes": len(simplified),
        "raw_arcs": graph.number_of_edges(),
        "simplified_arcs": simplified.number_of_edges(),
        "retained_grid_snap_nodes": len(keep),
        "raw_length_m": original_length,
        "simplified_length_m": simplified_length,
        "length_residual_m": simplified_length - original_length,
        "rules": "Retain intersections, rings, attribute changes and all nearest-grid-node ties; preserve intermediate geometry vertices and direction.",
    }
    return result, report


def prepare_san_francisco(root, *, cancellation, progress):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    if (root / "example-config.json").is_file():
        return ProjectConfig.model_validate_json((root / "example-config.json").read_bytes())
    boundary_path = root / "sf-boundary.parquet"
    if not boundary_path.exists():
        boundary, receipt = acquire_osm(
            root,
            operation="boundary",
            query="San Francisco, California, USA",
            cancellation=cancellation,
        )
        geometry = (
            boundary.to_crs(4326)
            .geometry.union_all()
            .intersection(box(-122.53, 37.70, -122.35, 37.84))
        )
        gpd.GeoDataFrame(
            {"name": ["San Francisco urban region"]}, geometry=[geometry], crs=4326
        ).to_parquet(boundary_path, index=False)
        (root / "sf-boundary-provenance.json").write_text(
            json.dumps({"source": receipt, "clip_bounds": [-122.53, 37.70, -122.35, 37.84]})
        )
    boundary = gpd.read_parquet(boundary_path)
    polygon = boundary.geometry.union_all()
    roads_path = root / "sf-roads.parquet"
    if not roads_path.exists():
        extent = (
            gpd.GeoSeries([boundary.to_crs(32610).geometry.union_all().buffer(1000)], crs=32610)
            .to_crs(4326)
            .iloc[0]
        )
        roads, _ = acquire_osm(
            root,
            operation="network",
            query="San Francisco urban region",
            polygon=extent,
            cancellation=cancellation,
        )
        roads.to_parquet(roads_path, index=False)

    def register(path, name, role):
        return snapshot_input(
            root, InputRegistration(path=str(path.resolve()), name=name, role=role)
        )["input_id"]

    features = []
    for category in ("residential", "commercial"):
        path = root / f"sf-{category}.parquet"
        if not path.exists():
            data, _ = acquire_osm(
                root,
                operation="features",
                query="San Francisco urban region",
                polygon=polygon,
                category=category,
                cancellation=cancellation,
            )
            data.to_parquet(path, index=False)
        data = gpd.read_parquet(path).to_crs(32610)
        data = data.loc[data.geometry.notna() & ~data.geometry.is_empty].copy()
        data.geometry = data.geometry.make_valid()
        if category == "residential":
            data = data[data.geom_type.isin(["Polygon", "MultiPolygon"])].copy()
            data["value"] = data.geometry.area
            unit, name = "m2", "residential_area"
        else:
            data.geometry = data.geometry.representative_point()
            data["value"] = 1.0
            unit, name = "count", "commercial_locations"
        if data.empty or data.value.sum() <= 0:
            raise ValueError(f"OSM has no usable {category} feature support")
        derived = root / f"sf-{category}-feature.parquet"
        data[["value", "geometry"]].to_parquet(derived, index=False)
        features.append(
            FeatureSelection(
                input_id=register(derived, f"OSM {name}", "feature"),
                name=name,
                value_column="value",
                unit=unit,
            )
        )
    editor = EnvironmentEditor(
        boundary_input=register(boundary_path, "San Francisco urban boundary · OSM", "boundary"),
        network_input=register(roads_path, "San Francisco directed roads · OSM", "network"),
        working_crs="EPSG:32610",
        timezone="America/Los_Angeles",
        grid_size_m=100,
        speed_source="constant",
        speed_kph=20,
        features=tuple(features),
        osm_features=("transportation", "public_services", "leisure"),
    )
    prepared = build_environment(
        root,
        editor,
        application=HeadlessApplication(root),
        cancellation=cancellation,
        progress=progress,
    )
    from mobile_sensing.api.queries import query_environment

    original = query_environment(root, prepared.artifact)
    roads, contraction = simplify_osm_roads(gpd.read_parquet(roads_path), original.grid_cells)
    contracted_path = root / "sf-roads-simplified.parquet"
    roads.to_parquet(contracted_path, index=False)
    (root / "sf-road-simplification.json").write_text(json.dumps(contraction, indent=2))
    editor = editor.model_copy(
        update={
            "network_input": register(
                contracted_path,
                "San Francisco OSM roads · retained geometry, contracted shape nodes",
                "network",
            )
        }
    )
    prepared = build_environment(
        root,
        editor,
        application=HeadlessApplication(root),
        cancellation=cancellation,
        progress=progress,
    )
    prepared = prepared.model_copy(
        update={
            "assumptions": (
                *prepared.assumptions,
                "OSM urban study window excludes offshore islands; SFO airport is outside this study region.",
                "OSM residential, commercial, transportation, public-service and leisure features are spatial activity proxies, not measured population or requests.",
                "Synthetic taxi fleet, demand profile and service times; no claim of citywide market calibration.",
                "Uniform 20 km/h is an uncalibrated urban operating-speed assumption, not measured traffic.",
            ),
        }
    )
    fleets = (
        FleetEditor(
            fleet_id="taxi",
            name="Taxi",
            demand=DemandEditor(
                task_type="od",
                volume_mode="expected",
                task_volume=2000,
                generation_timing="online",
                start_time="00:00",
                end_time="24:00",
                temporal_mode="shares",
                time_profile=tuple(
                    TemporalInterval(start_time=a, end_time=b, value=p)
                    for a, b, p in [
                        ("00:00", "06:00", 0.06),
                        ("06:00", "09:00", 0.16),
                        ("09:00", "12:00", 0.13),
                        ("12:00", "16:00", 0.21),
                        ("16:00", "19:00", 0.23),
                        ("19:00", "21:00", 0.12),
                        ("21:00", "24:00", 0.09),
                    ]
                ),
                spatial_feature="residential_area",
                spatial_weights=(
                    SpatialFeatureWeight(feature="residential_area", weight=0.35),
                    SpatialFeatureWeight(feature="commercial_locations", weight=0.25),
                    SpatialFeatureWeight(feature="transportation", weight=0.20),
                    SpatialFeatureWeight(feature="public_services", weight=0.10),
                    SpatialFeatureWeight(feature="leisure", weight=0.10),
                ),
                destination_spatial_weights=(
                    SpatialFeatureWeight(feature="residential_area", weight=0.30),
                    SpatialFeatureWeight(feature="commercial_locations", weight=0.30),
                    SpatialFeatureWeight(feature="transportation", weight=0.20),
                    SpatialFeatureWeight(feature="public_services", weight=0.10),
                    SpatialFeatureWeight(feature="leisure", weight=0.10),
                ),
                destination_feature="residential_area",
                pickup_seconds=60,
                service_seconds=30,
            ),
            supply=SupplyEditor(
                fleet_size=100,
                operating_start="00:00",
                operating_end="24:00",
                activation="uniform_bounded",
                latest_start="16:00",
                work_hours=8,
                spatial_feature="residential_area",
                spatial_weights=(
                    SpatialFeatureWeight(feature="residential_area", weight=0.6),
                    SpatialFeatureWeight(feature="commercial_locations", weight=0.4),
                ),
                post_service="random_cruise",
                capacity_mode="occupancy",
                capacity=1,
                shift_groups=tuple(
                    ShiftGroup(name=n, count=c, start_time=a, latest_start=b)
                    for n, c, a, b in [
                        ("Night", 10, "00:00", "00:00"),
                        ("Morning", 30, "05:00", "06:00"),
                        ("Daytime", 30, "09:00", "11:00"),
                        ("Afternoon", 30, "15:00", "16:00"),
                    ]
                ),
            ),
        ),
    )
    config = ProjectConfig(
        environment=editor,
        prepared_environment=prepared,
        fleets=fleets,
        simulation=SimulationEditor(
            replications=10, temporal_resolution_minutes=60, timezone="America/Los_Angeles"
        ),
    )
    (root / "example-config.json").write_text(config.model_dump_json(indent=2))
    receipts = [json.loads(p.read_text()) for p in (root / "osm_cache").glob("*/receipt.json")]
    (root / "input-provenance.json").write_text(
        json.dumps(
            {
                "geography": "OpenStreetMap only; ODbL attribution retained",
                "osm_receipts": receipts,
                "road_simplification": contraction,
                "boundary_selection": json.loads(
                    (root / "sf-boundary-provenance.json").read_text()
                ),
                "parameter_evidence": [
                    "https://www.sfmta.com/notices/taxi-upfront-fare-pilot-2024-q3-q4-report",
                    "https://www.sfmta.com/sf-taxi",
                    "https://www.sfmta.com/notices/taxi-upfront-fare-pilot-2024-q2-report",
                    "https://www.sfcta.org/blogs/transportation-board-approves-eco-friendly-downtown-delivery-study-final-report",
                ],
                "scope": "Synthetic taxi operator: 100 taxis / expected 2000 intra-region requests. Counts, feature mixtures and profiles are workload assumptions, not observed SFMTA totals. No airport trips. One eight-hour shift per vehicle; no previous-day carry-in.",
            },
            indent=2,
        )
    )
    return config


def compute_san_francisco(root, config, *, cancellation, progress):
    root = Path(root)
    options = RunOptions(workers=4, memory_limit_bytes=8 * 1024**3, job_timeout_s=14400)
    run = run_project(
        root,
        config,
        name="San Francisco · Typical weekday · Full day",
        source_revision_id=None,
        options=options,
        cancellation=cancellation,
        progress=progress,
    )
    (root / "example-run.json").write_text(run.model_dump_json(indent=2))
    analysis, _ = analyze_san_francisco(root, run, cancellation=cancellation, progress=progress)
    return run, analysis


def analyze_san_francisco(root, run, *, cancellation, progress):
    """Retain both objectives using the same physical run and allocation sample."""
    root = Path(root)
    options = RunOptions(workers=4, memory_limit_bytes=8 * 1024**3, job_timeout_s=14400)
    analysis = run_analysis(
        root,
        demonstration_portfolio(
            run,
            risk_metric="p05",
            budgets=tuple(float(value) for value in range(10, 101, 10)),
            saturation_minutes=5.0,
        ),
        name="Worst-case utility · 5-minute saturation",
        source_revision_id=None,
        options=options,
        cancellation=cancellation,
        progress=progress,
    )
    (root / "example-analysis.json").write_text(analysis.model_dump_json(indent=2))
    standard_deviation = run_analysis(
        root,
        analysis.config.model_copy(update={"risk_metric": "std"}),
        name="Standard-deviation utility · 5-minute saturation",
        source_revision_id=None,
        options=options,
        cancellation=cancellation,
        progress=progress,
    )
    (root / "example-analysis-std.json").write_text(standard_deviation.model_dump_json(indent=2))
    return analysis, standard_deviation


def main():
    """Rebuild release inputs/results through the same public application services."""
    import argparse
    from mobile_sensing.application.example_build import BuildCancellation, BuildProgress

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--stage", choices=("prepare", "compute", "analyze"), default="prepare")
    args = parser.parse_args()
    cancellation, progress = BuildCancellation(args.root), BuildProgress(args.root)
    if args.stage == "analyze":
        run = RunView.model_validate_json((args.root / "example-run.json").read_bytes())
        analyze_san_francisco(args.root, run, cancellation=cancellation, progress=progress)
        return
    config = prepare_san_francisco(args.root, cancellation=cancellation, progress=progress)
    if args.stage == "compute":
        compute_san_francisco(args.root, config, cancellation=cancellation, progress=progress)


if __name__ == "__main__":
    main()
