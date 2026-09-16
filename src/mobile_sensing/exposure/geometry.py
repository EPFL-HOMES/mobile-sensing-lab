"""Ordered directed-edge decomposition against a nonoverlapping sensing grid."""

from __future__ import annotations

import math
from collections.abc import Iterable

import geopandas as gpd
from shapely.geometry import GeometryCollection, LineString, MultiLineString, MultiPoint, Point

from mobile_sensing.exposure.models import EdgeGridPiece


EDGE_GRID_ALGORITHM_VERSION = "ordered-edge-grid@1"
ROAD_GRID_DOMAIN_VERSION = "positive-length-road-grid@1"
GEOMETRY_TOLERANCE_M = 1e-9


class EdgeGridPreparationError(ValueError):
    """Raised when edge/grid geometry cannot satisfy the ordered-piece contract."""


def _intersection_points(geometry) -> Iterable[Point]:
    if geometry.is_empty:
        return
    if isinstance(geometry, Point):
        yield geometry
        return
    if isinstance(geometry, MultiPoint):
        yield from geometry.geoms
        return
    if isinstance(geometry, LineString):
        yield Point(geometry.coords[0])
        yield Point(geometry.coords[-1])
        return
    if isinstance(geometry, (MultiLineString, GeometryCollection)):
        for part in geometry.geoms:
            yield from _intersection_points(part)


def _validate_inputs(edges: gpd.GeoDataFrame, cells: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    if edges.empty or cells.empty or edges.crs is None or cells.crs is None:
        raise EdgeGridPreparationError("edges and grid cells must be nonempty with declared CRS")
    if edges.crs != cells.crs:
        raise EdgeGridPreparationError("edge and grid CRS must match exactly")
    if not edges.crs.is_projected or any(
        axis.unit_name.casefold() not in {"metre", "meter"}
        or not math.isclose(axis.unit_conversion_factor, 1.0, abs_tol=1e-12)
        for axis in edges.crs.axis_info
    ):
        raise EdgeGridPreparationError("edge-grid preparation requires a metric projected CRS")
    if (
        "edge_id" not in edges
        or not edges.edge_id.map(lambda value: isinstance(value, str) and value != "").all()
        or edges.edge_id.duplicated().any()
    ):
        raise EdgeGridPreparationError("directed edges require unique nonempty edge_id values")
    if not edges.geometry.geom_type.eq("LineString").all():
        raise EdgeGridPreparationError("directed edge geometry must contain only LineStrings")
    if (
        "cell_id" not in cells
        or not cells.cell_id.map(lambda value: isinstance(value, str) and value != "").all()
        or cells.cell_id.duplicated().any()
    ):
        raise EdgeGridPreparationError("grid cells require unique nonempty cell_id values")
    geometry_column = "sensing_geometry" if "sensing_geometry" in cells else cells.geometry.name
    normalized = cells[["cell_id", geometry_column]].copy()
    normalized = normalized.set_geometry(geometry_column)
    if geometry_column != "geometry":
        normalized = normalized.rename_geometry("geometry")
    if not normalized.geometry.geom_type.isin({"Polygon", "MultiPolygon"}).all():
        raise EdgeGridPreparationError("sensing grid must contain polygonal geometry")
    if not normalized.geometry.is_valid.all() or (normalized.geometry.area <= 0.0).any():
        raise EdgeGridPreparationError("sensing grid geometry must be valid with positive area")
    normalized = normalized.sort_values("cell_id", kind="stable").reset_index(drop=True)
    pairs = normalized.sindex.query(normalized.geometry, predicate="intersects")
    for left, right in zip(pairs[0], pairs[1], strict=True):
        if left >= right:
            continue
        overlap = normalized.geometry.iloc[left].intersection(normalized.geometry.iloc[right]).area
        if overlap > GEOMETRY_TOLERANCE_M**2:
            raise EdgeGridPreparationError(
                f"sensing grid interiors overlap: {normalized.cell_id.iloc[left]!r}, "
                f"{normalized.cell_id.iloc[right]!r}"
            )
    return normalized


def _edge_breakpoints(edge: LineString, candidate_geometries: Iterable) -> list[float]:
    coordinates = list(edge.coords)
    cumulative = 0.0
    distances = [0.0, float(edge.length)]
    for start, end in zip(coordinates, coordinates[1:]):
        segment = LineString((start, end))
        length = float(segment.length)
        if length <= 0.0:
            raise EdgeGridPreparationError(
                "directed edge contains a zero-length coordinate segment"
            )
        for polygon in candidate_geometries:
            crossing = segment.intersection(polygon.boundary)
            for point in _intersection_points(crossing):
                distances.append(cumulative + float(segment.project(point)))
        cumulative += length
    distances.sort()
    unique = []
    for distance in distances:
        bounded = min(max(distance, 0.0), float(edge.length))
        if not unique or not math.isclose(
            bounded, unique[-1], rel_tol=0.0, abs_tol=GEOMETRY_TOLERANCE_M
        ):
            unique.append(bounded)
    unique[0] = 0.0
    unique[-1] = float(edge.length)
    return unique


def build_edge_grid_pieces(
    edges: gpd.GeoDataFrame,
    cells: gpd.GeoDataFrame,
) -> tuple[EdgeGridPiece, ...]:
    """Partition every oriented edge into contiguous in-grid/outside pieces."""

    grid = _validate_inputs(edges, cells)
    pieces = []
    for edge_row in edges.sort_values("edge_id", kind="stable").itertuples():
        edge = edge_row.geometry
        length = float(edge.length)
        if not math.isfinite(length) or length <= 0.0:
            raise EdgeGridPreparationError("directed edge length must be positive and finite")
        declared_length = getattr(edge_row, "length_m", length)
        if not math.isclose(float(declared_length), length, rel_tol=1e-10, abs_tol=1e-6):
            raise EdgeGridPreparationError("directed edge metric length disagrees with geometry")
        candidate_indices = sorted(set(grid.sindex.query(edge, predicate="intersects")))
        candidates = grid.iloc[candidate_indices]
        breakpoints = _edge_breakpoints(edge, candidates.geometry)
        edge_pieces = []
        for start_m, end_m in zip(breakpoints, breakpoints[1:]):
            if end_m <= start_m:
                continue
            midpoint = edge.interpolate((start_m + end_m) / 2.0)
            covering = sorted(
                row.cell_id for row in candidates.itertuples() if row.geometry.covers(midpoint)
            )
            edge_pieces.append(
                EdgeGridPiece(
                    edge_id=str(edge_row.edge_id),
                    piece_index=len(edge_pieces),
                    cell_id=covering[0] if covering else None,
                    start_fraction=start_m / length,
                    end_fraction=end_m / length,
                )
            )
        if not edge_pieces:
            raise EdgeGridPreparationError("edge decomposition produced no positive pieces")
        edge_pieces[0] = EdgeGridPiece(
            edge_id=edge_pieces[0].edge_id,
            piece_index=0,
            cell_id=edge_pieces[0].cell_id,
            start_fraction=0.0,
            end_fraction=edge_pieces[0].end_fraction,
        )
        last = edge_pieces[-1]
        edge_pieces[-1] = EdgeGridPiece(
            edge_id=last.edge_id,
            piece_index=last.piece_index,
            cell_id=last.cell_id,
            start_fraction=last.start_fraction,
            end_fraction=1.0,
        )
        if any(
            left.end_fraction != right.start_fraction
            for left, right in zip(edge_pieces, edge_pieces[1:])
        ):
            raise EdgeGridPreparationError("edge-grid pieces do not form a contiguous partition")
        pieces.extend(edge_pieces)
    return tuple(pieces)


def road_intersecting_grid_cells(
    edges: gpd.GeoDataFrame,
    cells: gpd.GeoDataFrame,
    *,
    batch_size: int = 20_000,
) -> gpd.GeoDataFrame:
    """Select cells having a positive-length intersection with sensing roads."""

    if isinstance(batch_size, bool) or batch_size <= 0:
        raise ValueError("road-grid domain batch size must be positive")
    grid = _validate_inputs(edges, cells)
    eligible: set[str] = set()
    for start in range(0, len(edges), batch_size):
        edge_geometry = edges.geometry.iloc[start : start + batch_size].reset_index(drop=True)
        edge_positions, cell_positions = grid.sindex.query(edge_geometry, predicate="intersects")
        if not len(edge_positions):
            continue
        intersections = gpd.GeoSeries(
            edge_geometry.iloc[edge_positions].reset_index(drop=True), crs=edges.crs
        ).intersection(
            gpd.GeoSeries(grid.geometry.iloc[cell_positions].reset_index(drop=True), crs=grid.crs)
        )
        positive = intersections.length.to_numpy() > GEOMETRY_TOLERANCE_M
        eligible.update(grid.cell_id.iloc[cell_positions[positive]].astype(str))
    if not eligible:
        raise EdgeGridPreparationError(
            "sensing road network has no positive-length intersection with the prepared grid"
        )
    selected = cells.loc[cells.cell_id.astype(str).isin(eligible)].copy()
    return selected.sort_values("cell_id", kind="stable").reset_index(drop=True)
