"""Deterministic bounded diagnostic for the bundled Lausanne geographic inputs."""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

import geopandas as gpd
import networkx as nx
import numpy as np
import pandas as pd
import shapely
from scipy.spatial import cKDTree
from shapely import force_2d

from mobile_sensing.environment.provider import file_sha256
from mobile_sensing.environment.repair import diagnose_network


LAUSANNE_MUNICIPALITIES = ("Lausanne (suburban)", "Lausanne (urban)")


def build_lausanne_diagnostic(data_directory: Path) -> dict[str, object]:
    data_directory = Path(data_directory)
    boundary_path = data_directory / "boundary_lausanne.gpkg"
    roads_path = data_directory / "lausanne_roads_encoded.gpkg"
    grid_path = data_directory / "grid_100m.gpkg"
    population_path = data_directory / "population_swiss.csv"

    boundaries = gpd.read_file(boundary_path).to_crs("EPSG:2056")
    available_names = tuple(sorted(boundaries.name.astype(str)))
    selected_rows = boundaries.name.astype(str).isin(LAUSANNE_MUNICIPALITIES)
    sensing_boundary = boundaries.loc[selected_rows].geometry.union_all()
    routing_boundary = boundaries.geometry.union_all()

    roads = gpd.read_file(roads_path).to_crs("EPSG:2056")
    routing_mask = roads.geometry.intersects(routing_boundary)
    sensing_mask = roads.geometry.intersects(sensing_boundary)
    selected_roads = roads.loc[routing_mask]
    nonmergeable: list[str] = []
    invalid: list[str] = []
    endpoints: dict[str, list[tuple[float, float]]] = defaultdict(list)
    graph = nx.MultiDiGraph()
    valid_sensing_rows = 0
    valid_one_way = Counter()
    for row in selected_roads.itertuples():
        source_id = f"{row.u}/{row.v}/{row.key}"
        geometry = force_2d(row.geometry)
        merged = (
            shapely.line_merge(geometry) if geometry.geom_type == "MultiLineString" else geometry
        )
        if merged.geom_type == "MultiLineString":
            nonmergeable.append(source_id)
            continue
        if merged.geom_type != "LineString" or merged.is_empty or merged.length <= 0:
            invalid.append(source_id)
            continue
        u, v = str(row.u), str(row.v)
        endpoints[u].append(tuple(merged.coords[0]))
        endpoints[v].append(tuple(merged.coords[-1]))
        graph.add_edge(u, v, key=str(row.key))
        one_way = bool(row.one_way)
        valid_one_way[one_way] += 1
        if not one_way:
            graph.add_edge(v, u, key=f"{row.key}:reverse")
        if merged.intersects(sensing_boundary):
            valid_sensing_rows += 1
    endpoint_conflicts: list[dict[str, object]] = []
    node_coordinates: list[tuple[float, float]] = []
    for node_id, values in sorted(endpoints.items()):
        canonical = min(values)
        displacement = max(math.dist(canonical, value) for value in values)
        node_coordinates.append(canonical)
        if displacement > 0.05:
            endpoint_conflicts.append(
                {"node_id": node_id, "max_displacement_m": round(displacement, 6)}
            )

    grid = gpd.read_file(grid_path).to_crs("EPSG:2056")
    intersections = grid.geometry.intersection(sensing_boundary)
    grid_mask = (~intersections.is_empty) & (intersections.area > 1e-8)
    selected_intersections = intersections.loc[grid_mask]
    selected_grid = grid.loc[grid_mask]
    fractions = selected_intersections.area / selected_grid.geometry.area
    overlap_pairs = selected_grid.sindex.query(selected_grid.geometry, predicate="intersects")
    overlap_count = 0
    for left, right in zip(overlap_pairs[0], overlap_pairs[1]):
        if left < right and (
            selected_grid.geometry.iloc[left].intersection(selected_grid.geometry.iloc[right]).area
            > 1e-8
        ):
            overlap_count += 1

    population = pd.read_csv(population_path)
    population_year = population.loc[population.year == 2024]
    aligned = selected_grid[["easting", "northing"]].merge(
        population_year,
        on=["easting", "northing"],
        how="left",
        validate="one_to_one",
    )

    centroids = np.column_stack(
        [selected_grid.geometry.centroid.x, selected_grid.geometry.centroid.y]
    )
    tree = cKDTree(np.asarray(node_coordinates))
    snap_distances, _ = tree.query(centroids, k=1)
    sensing_source_ids = {
        f"{row.u}/{row.v}/{row.key}" for row in roads.loc[sensing_mask].itertuples()
    }
    diagnosis = diagnose_network(selected_roads)
    repairable_ids = set(
        diagnosis.row_diagnostics.loc[
            diagnosis.row_diagnostics.status == "repairable_branch", "source_record_id"
        ]
    )
    disconnected_ids = set(
        diagnosis.row_diagnostics.loc[
            diagnosis.row_diagnostics.status == "disconnected", "source_record_id"
        ]
    )
    disconnected_sensing_count = sum(
        source_id in sensing_source_ids for source_id in disconnected_ids
    )
    unresolved = []
    if repairable_ids:
        unresolved.append(
            {
                "code": "repairable_branching_multilinestring",
                "severity": "warning",
                "count": len(repairable_ids),
                "sensing_region_count": sum(
                    source_id in sensing_source_ids for source_id in repairable_ids
                ),
                "examples": sorted(repairable_ids)[:10],
                "resolution": (
                    "Use conservative_repair@1 to split existing bidirectional components "
                    "without adding geometry."
                ),
            }
        )
    if disconnected_ids:
        unresolved.append(
            {
                "code": "disconnected_multilinestring",
                "severity": "error",
                "count": len(disconnected_ids),
                "sensing_region_count": disconnected_sensing_count,
                "examples": sorted(disconnected_ids)[:10],
                "resolution": "Quarantine or replace these records; no gap is bridged automatically.",
            }
        )
    if endpoint_conflicts:
        unresolved.append(
            {
                "code": "inconsistent_node_endpoint",
                "severity": "error",
                "count": len(endpoint_conflicts),
                "examples": endpoint_conflicts[:10],
                "resolution": "Disambiguate reused source node IDs upstream.",
            }
        )
    missing_population = int(aligned.residents.isna().sum())
    if missing_population:
        unresolved.append(
            {
                "code": "population_missing_cells",
                "severity": "warning",
                "count": missing_population,
                "resolution": "Select error, zero, or uniform_proxy explicitly during preparation.",
            }
        )

    weak_components = nx.number_weakly_connected_components(graph) if graph else 0
    strong_components = nx.number_strongly_connected_components(graph) if graph else 0
    return {
        "diagnostic_version": "lausanne-geography@2",
        "external_network_access": False,
        "working_crs": "EPSG:2056",
        "selected_municipalities": list(LAUSANNE_MUNICIPALITIES),
        "routing_extent_municipalities": list(available_names),
        "inputs": {
            "boundary": {"file": boundary_path.name, "sha256": file_sha256(boundary_path)},
            "roads": {"file": roads_path.name, "sha256": file_sha256(roads_path)},
            "grid": {"file": grid_path.name, "sha256": file_sha256(grid_path)},
            "population": {
                "file": population_path.name,
                "sha256": file_sha256(population_path),
            },
        },
        "boundary": {
            "source_layer_count": len(boundaries),
            "sensing_selected_count": int(selected_rows.sum()),
            "routing_selected_count": len(boundaries),
            "sensing_area_km2": sensing_boundary.area / 1_000_000,
            "routing_area_km2": routing_boundary.area / 1_000_000,
        },
        "network": {
            "source_layer_count": len(roads),
            "routing_extent_selected_count": int(routing_mask.sum()),
            "sensing_intersection_count": int(sensing_mask.sum()),
            "valid_line_count_for_diagnostics": len(selected_roads)
            - len(nonmergeable)
            - len(invalid),
            "valid_line_sensing_intersection_count": valid_sensing_rows,
            "nonmergeable_multilinestring_count": len(nonmergeable),
            "repairable_branching_multilinestring_count": len(repairable_ids),
            "disconnected_multilinestring_count": len(disconnected_ids),
            "invalid_other_geometry_count": len(invalid),
            "endpoint_conflict_node_count": len(endpoint_conflicts),
            "diagnostic_node_count": graph.number_of_nodes(),
            "diagnostic_directed_edge_count": graph.number_of_edges(),
            "weak_component_count": weak_components,
            "strong_component_count": strong_components,
            "one_way_source_count": int(valid_one_way[True]),
            "two_way_source_count": int(valid_one_way[False]),
            "speed_fields_present": [],
            "required_speed_provenance": "assumed_static_speeds",
        },
        "grid": {
            "source_layer_count": len(grid),
            "sensing_selected_count": int(grid_mask.sum()),
            "fully_included_count": int((fractions >= 1 - 1e-10).sum()),
            "partially_included_count": int(((fractions > 1e-10) & (fractions < 1 - 1e-10)).sum()),
            "positive_area_overlap_pair_count": overlap_count,
            "boundary_area_coverage_fraction": float(
                selected_intersections.area.sum() / sensing_boundary.area
            ),
            "nearest_node_distance_m": {
                "median": float(np.median(snap_distances)),
                "maximum": float(np.max(snap_distances)),
                "within_250m_fraction": float(np.mean(snap_distances <= 250.0)),
            },
        },
        "population": {
            "source_layer_count": len(population),
            "selected_year": 2024,
            "selected_year_count": len(population_year),
            "duplicate_coordinate_count": int(
                population_year.duplicated(["easting", "northing"]).sum()
            ),
            "negative_resident_count": int((population_year.residents < 0).sum()),
            "matched_grid_cell_count": int(aligned.residents.notna().sum()),
            "missing_grid_cell_count": missing_population,
            "matched_resident_mass": float(aligned.residents.sum()),
        },
        "capabilities": {
            "environment.local_files@1": True,
            "grid.regular@1": True,
            "environment.osm@1": False,
        },
        "unresolved_defects": unresolved,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-directory", type=Path, default=Path("data/Lausanne"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_lausanne_diagnostic(args.data_directory)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
