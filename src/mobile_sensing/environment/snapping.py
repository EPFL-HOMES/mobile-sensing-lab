"""Metric nearest-node location resolution with deterministic ties."""

from __future__ import annotations

import math

import geopandas as gpd
import numpy as np
from pyproj import Transformer
from scipy.spatial import cKDTree

from mobile_sensing.contracts import LocationRef, ResolutionStatus


class NodeSnapper:
    def __init__(self, nodes: gpd.GeoDataFrame, *, max_distance_m: float) -> None:
        if nodes.empty or nodes.crs is None:
            raise ValueError("node snapper requires nonempty metric nodes")
        if max_distance_m <= 0:
            raise ValueError("max_distance_m must be positive")
        if not nodes.crs.is_projected or any(
            axis.unit_name.casefold() not in {"metre", "meter"}
            or not math.isclose(axis.unit_conversion_factor, 1.0, abs_tol=1e-12)
            for axis in nodes.crs.axis_info
        ):
            raise ValueError("node snapper requires a projected metre CRS")
        ordered = nodes.sort_values("node_id", kind="stable").reset_index(drop=True)
        self.node_ids = tuple(ordered.node_id.astype(str))
        self.coordinates = np.column_stack(
            [ordered.x_m.to_numpy(dtype=float), ordered.y_m.to_numpy(dtype=float)]
        )
        self.working_crs = ordered.crs
        self.max_distance_m = float(max_distance_m)
        self._tree = cKDTree(self.coordinates)
        self._transformers: dict[str, Transformer] = {}

    def resolve(self, *, location_id: str, x: float, y: float, source_crs: str) -> LocationRef:
        if not math.isfinite(x) or not math.isfinite(y):
            raise ValueError("location coordinates must be finite")
        try:
            transformer = self._transformers.get(source_crs)
            if transformer is None:
                transformer = Transformer.from_crs(source_crs, self.working_crs, always_xy=True)
                self._transformers[source_crs] = transformer
            projected_x, projected_y = transformer.transform(x, y)
        except Exception:
            projected_x, projected_y = math.nan, math.nan
        if not math.isfinite(projected_x) or not math.isfinite(projected_y):
            return LocationRef(
                location_id=location_id,
                original_x=x,
                original_y=y,
                original_crs=source_crs,
                node_id=None,
                snapped_x=None,
                snapped_y=None,
                snap_distance_m=None,
                resolution_status=ResolutionStatus.REJECTED_INVALID,
            )
        nearest_distance, nearest_index = self._tree.query([projected_x, projected_y], k=1)
        candidates = self._tree.query_ball_point(
            [projected_x, projected_y], r=float(nearest_distance) + 1e-9
        )
        ranked = sorted(
            (
                math.hypot(
                    projected_x - self.coordinates[index, 0],
                    projected_y - self.coordinates[index, 1],
                ),
                self.node_ids[index],
                index,
            )
            for index in candidates
        )
        distance, node_id, index = (
            ranked[0]
            if ranked
            else (
                float(nearest_distance),
                self.node_ids[int(nearest_index)],
                int(nearest_index),
            )
        )
        if distance > self.max_distance_m:
            return LocationRef(
                location_id=location_id,
                original_x=x,
                original_y=y,
                original_crs=source_crs,
                node_id=None,
                snapped_x=None,
                snapped_y=None,
                snap_distance_m=None,
                resolution_status=ResolutionStatus.REJECTED_DISTANCE,
            )
        return LocationRef(
            location_id=location_id,
            original_x=x,
            original_y=y,
            original_crs=source_crs,
            node_id=node_id,
            snapped_x=float(self.coordinates[index, 0]),
            snapped_y=float(self.coordinates[index, 1]),
            snap_distance_m=distance,
            resolution_status=ResolutionStatus.RESOLVED,
        )
