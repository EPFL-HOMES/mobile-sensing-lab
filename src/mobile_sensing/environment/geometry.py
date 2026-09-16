"""Metric geometry normalization with no topology fabrication."""

from __future__ import annotations

import math

import shapely
from shapely import force_2d
from shapely.geometry import LineString, MultiLineString


class DisconnectedLineError(ValueError):
    """Raised when component linework cannot normalize to one oriented line."""


def normalize_oriented_line(geometry) -> LineString:
    """Return one 2-D line while preserving the source component orientation."""

    if geometry is None or geometry.is_empty:
        raise ValueError("road geometry must be nonempty")
    geometry = force_2d(geometry)
    if isinstance(geometry, LineString):
        line = geometry
        source_start = geometry.coords[0]
    elif isinstance(geometry, MultiLineString):
        parts = list(geometry.geoms)
        if not parts:
            raise ValueError("road geometry must contain coordinates")
        source_start = parts[0].coords[0]
        merged = shapely.line_merge(geometry)
        if not isinstance(merged, LineString):
            raise DisconnectedLineError(
                "road MultiLineString cannot be represented as one oriented LineString"
            )
        line = merged
    else:
        raise ValueError(f"road geometry must be lineal, not {geometry.geom_type}")
    if len(line.coords) < 2 or not math.isfinite(line.length) or line.length <= 0:
        raise ValueError("road geometry must have positive finite metric length")
    start_distance = math.dist(line.coords[0], source_start)
    end_distance = math.dist(line.coords[-1], source_start)
    if end_distance < start_distance:
        line = LineString(tuple(reversed(line.coords)))
    return line


def reverse_line(line: LineString) -> LineString:
    return LineString(tuple(reversed(line.coords)))
