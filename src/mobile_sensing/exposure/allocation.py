"""Exact deterministic allocation of movement intervals over space and time."""

from __future__ import annotations

import bisect
import math
import numbers
from collections import defaultdict
from collections.abc import Mapping, Sequence

import pandas as pd

from mobile_sensing.contracts import GridAxis, SparseExposureRow, TimeAxis, VehicleKey
from mobile_sensing.exposure.models import (
    AllocationResult,
    EdgeGridPiece,
    VehicleExposureDiagnostic,
)


EXPOSURE_ALLOCATION_VERSION = "exact-interval-exposure@1"
CONSERVATION_ABS_TOLERANCE_S = 1e-6
CONSERVATION_REL_TOLERANCE = 1e-10

MOVEMENT_COLUMNS = {
    "replication_id",
    "fleet_id",
    "vehicle_id",
    "movement_index",
    "start_s",
    "end_s",
    "movement_kind",
    "edge_id",
    "edge_start_fraction",
    "edge_end_fraction",
}
MOVEMENT_KINDS = {
    "service_pickup",
    "service_inter_step",
    "cruise",
    "depot_return",
    "reposition",
}


class ExposureAllocationError(ValueError):
    """Raised when movement or edge-grid inputs violate conservation assumptions."""


def _validate_movements(movements: pd.DataFrame) -> pd.DataFrame:
    if not MOVEMENT_COLUMNS <= set(movements.columns):
        raise ExposureAllocationError(
            f"movement table lacks fields: {sorted(MOVEMENT_COLUMNS-set(movements.columns))}"
        )
    for column in ("replication_id", "fleet_id", "vehicle_id", "edge_id", "movement_kind"):
        if not movements[column].map(lambda value: isinstance(value, str) and value != "").all():
            raise ExposureAllocationError(f"movement {column} values must be nonempty strings")
    if not movements["movement_kind"].isin(MOVEMENT_KINDS).all():
        raise ExposureAllocationError("movement_kind contains an unsupported value")
    if (
        not movements["movement_index"]
        .map(
            lambda value: isinstance(value, numbers.Integral)
            and not isinstance(value, bool)
            and value >= 0
        )
        .all()
    ):
        raise ExposureAllocationError("movement_index values must be nonnegative integers")
    ordered = movements.sort_values(
        ["replication_id", "fleet_id", "vehicle_id", "movement_index"], kind="stable"
    ).reset_index(drop=True)
    keys = ordered[["replication_id", "fleet_id", "vehicle_id", "movement_index"]]
    if keys.duplicated().any():
        raise ExposureAllocationError("movement keys must be unique")
    numeric = ordered[["start_s", "end_s", "edge_start_fraction", "edge_end_fraction"]].astype(
        float
    )
    if not numeric.map(math.isfinite).all().all():
        raise ExposureAllocationError("movement times and fractions must be finite")
    if (ordered.end_s <= ordered.start_s).any():
        raise ExposureAllocationError("movement intervals must have positive duration")
    if (
        (ordered.edge_start_fraction < 0.0)
        | (ordered.edge_end_fraction > 1.0)
        | (ordered.edge_end_fraction <= ordered.edge_start_fraction)
    ).any():
        raise ExposureAllocationError("movement edge fractions must increase within [0,1]")
    for _, group in ordered.groupby(["replication_id", "fleet_id", "vehicle_id"], sort=False):
        if tuple(int(value) for value in group.movement_index) != tuple(range(len(group))):
            raise ExposureAllocationError(
                "movement indices must be contiguous in directed traversal order"
            )
        chronological = group.sort_values(["start_s", "end_s", "movement_index"], kind="stable")
        if tuple(chronological.movement_index) != tuple(group.movement_index):
            raise ExposureAllocationError("movement index order must be chronological")
        if any(
            float(left.end_s) > float(right.start_s)
            for left, right in zip(chronological.itertuples(), chronological.iloc[1:].itertuples())
        ):
            raise ExposureAllocationError("one vehicle has overlapping movement intervals")
    return ordered


def _piece_index(pieces: Sequence[EdgeGridPiece]) -> dict[str, tuple[EdgeGridPiece, ...]]:
    grouped: dict[str, list[EdgeGridPiece]] = defaultdict(list)
    for piece in pieces:
        grouped[piece.edge_id].append(piece)
    result = {}
    for edge_id, values in grouped.items():
        ordered = tuple(sorted(values, key=lambda item: item.piece_index))
        if tuple(item.piece_index for item in ordered) != tuple(range(len(ordered))):
            raise ExposureAllocationError("edge-grid piece indices must be contiguous from zero")
        if ordered[0].start_fraction != 0.0 or ordered[-1].end_fraction != 1.0:
            raise ExposureAllocationError("edge-grid pieces must cover the full edge")
        if any(
            left.end_fraction != right.start_fraction for left, right in zip(ordered, ordered[1:])
        ):
            raise ExposureAllocationError("edge-grid pieces must be contiguous")
        result[edge_id] = ordered
    return result


def allocate_movement_exposure(
    movements: pd.DataFrame,
    pieces: Sequence[EdgeGridPiece],
    *,
    grid_axis: GridAxis,
    time_axis: TimeAxis,
    active_movement_kinds: Sequence[str],
    fleet_movement_kinds=None,
) -> AllocationResult:
    """Apply the normative within-edge linear time map and half-open intersections."""

    ordered = _validate_movements(movements)
    indexed_pieces = _piece_index(pieces)
    cell_ids = frozenset(grid_axis.cell_ids)
    if any(piece.cell_id is not None and piece.cell_id not in cell_ids for piece in pieces):
        raise ExposureAllocationError("edge-grid piece references a cell outside the grid axis")
    active = frozenset(active_movement_kinds)
    per_fleet = {key: frozenset(value) for key, value in (fleet_movement_kinds or {}).items()}
    bin_edges = [time_axis.bins[0].start_s, *(item.end_s for item in time_axis.bins)]
    contributions: dict[tuple[str, str, str, str, str], list[float]] = defaultdict(list)
    totals: dict[tuple[str, str, str], list[float]] = defaultdict(lambda: [0.0, 0.0])
    vehicles = {
        (str(row.replication_id), str(row.fleet_id), str(row.vehicle_id))
        for row in ordered.itertuples()
    }
    for row in ordered.itertuples():
        if row.movement_kind not in per_fleet.get(str(row.fleet_id), active):
            continue
        try:
            edge_pieces = indexed_pieces[str(row.edge_id)]
        except KeyError as exc:
            raise ExposureAllocationError(
                f"movement references edge without a complete grid partition: {row.edge_id!r}"
            ) from exc
        interval_start = max(float(row.start_s), time_axis.observation_start_s)
        interval_end = min(float(row.end_s), time_axis.end_s)
        if interval_end <= interval_start:
            continue
        vehicle_key = (str(row.replication_id), str(row.fleet_id), str(row.vehicle_id))
        totals[vehicle_key][0] += interval_end - interval_start
        duration = float(row.end_s) - float(row.start_s)
        fraction_span = float(row.edge_end_fraction) - float(row.edge_start_fraction)
        for piece in edge_pieces:
            start_fraction = max(float(row.edge_start_fraction), piece.start_fraction)
            end_fraction = min(float(row.edge_end_fraction), piece.end_fraction)
            if end_fraction <= start_fraction:
                continue
            spatial_start = float(row.start_s) + duration * (
                (start_fraction - float(row.edge_start_fraction)) / fraction_span
            )
            spatial_end = float(row.start_s) + duration * (
                (end_fraction - float(row.edge_start_fraction)) / fraction_span
            )
            clipped_start = max(spatial_start, time_axis.observation_start_s)
            clipped_end = min(spatial_end, time_axis.end_s)
            if clipped_end <= clipped_start:
                continue
            if piece.cell_id is None:
                totals[vehicle_key][1] += clipped_end - clipped_start
                continue
            first_bin = max(0, bisect.bisect_right(bin_edges, clipped_start) - 1)
            for bin_index in range(first_bin, len(time_axis.bins)):
                bin_record = time_axis.bins[bin_index]
                if bin_record.start_s >= clipped_end:
                    break
                overlap = min(clipped_end, bin_record.end_s) - max(
                    clipped_start, bin_record.start_s
                )
                if overlap > 0.0:
                    contributions[
                        (
                            vehicle_key[0],
                            vehicle_key[1],
                            vehicle_key[2],
                            piece.cell_id,
                            bin_record.time_bin_id,
                        )
                    ].append(overlap)
    rows = tuple(
        SparseExposureRow(
            replication_id=key[0],
            vehicle=VehicleKey(fleet_id=key[1], vehicle_id=key[2]),
            cell_id=key[3],
            time_bin_id=key[4],
            duration_s=math.fsum(values),
        )
        for key, values in sorted(contributions.items())
        if math.fsum(values) > 0.0
    )
    by_vehicle_in_grid: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    by_vehicle_bin: dict[tuple[str, str, str, str], list[float]] = defaultdict(list)
    for row in rows:
        key = (row.replication_id, row.vehicle.fleet_id, row.vehicle.vehicle_id)
        by_vehicle_in_grid[key].append(row.duration_s)
        by_vehicle_bin[(*key, row.time_bin_id)].append(row.duration_s)
    for key, values in by_vehicle_bin.items():
        bin_record = next(item for item in time_axis.bins if item.time_bin_id == key[3])
        observed = math.fsum(values)
        bound = bin_record.end_s - bin_record.start_s
        if observed > bound and not math.isclose(
            observed,
            bound,
            rel_tol=CONSERVATION_REL_TOLERANCE,
            abs_tol=CONSERVATION_ABS_TOLERANCE_S,
        ):
            raise ExposureAllocationError("per-vehicle exposure exceeds a reporting-bin duration")
    diagnostics = []
    for key in sorted(vehicles):
        active_s, outside_s = totals[key]
        in_grid_s = math.fsum(by_vehicle_in_grid[key])
        residual = active_s - in_grid_s - outside_s
        if not math.isclose(
            active_s,
            in_grid_s + outside_s,
            rel_tol=CONSERVATION_REL_TOLERANCE,
            abs_tol=CONSERVATION_ABS_TOLERANCE_S,
        ):
            raise ExposureAllocationError("active movement duration is not spatially conserved")
        diagnostics.append(
            VehicleExposureDiagnostic(
                replication_id=key[0],
                vehicle=VehicleKey(fleet_id=key[1], vehicle_id=key[2]),
                active_moving_time_s=active_s,
                in_grid_duration_s=in_grid_s,
                outside_grid_duration_s=outside_s,
                conservation_residual_s=residual,
            )
        )
    return AllocationResult(rows=rows, diagnostics=tuple(diagnostics))


def coarsen_sparse_exposure(
    rows: Sequence[SparseExposureRow],
    *,
    cell_mapping: Mapping[str, str],
    time_bin_mapping: Mapping[str, str],
) -> tuple[SparseExposureRow, ...]:
    """Exactly coarsen already allocated rows under explicit aligned axis mappings."""

    contributions: dict[tuple[str, str, str, str, str], list[float]] = defaultdict(list)
    for row in rows:
        try:
            cell_id = cell_mapping[row.cell_id]
            time_bin_id = time_bin_mapping[row.time_bin_id]
        except KeyError as exc:
            raise ExposureAllocationError(
                "coarsening mappings must cover every nonzero row"
            ) from exc
        key = (
            row.replication_id,
            row.vehicle.fleet_id,
            row.vehicle.vehicle_id,
            cell_id,
            time_bin_id,
        )
        contributions[key].append(row.duration_s)
    return tuple(
        SparseExposureRow(
            replication_id=key[0],
            vehicle=VehicleKey(fleet_id=key[1], vehicle_id=key[2]),
            cell_id=key[3],
            time_bin_id=key[4],
            duration_s=math.fsum(values),
        )
        for key, values in sorted(contributions.items())
    )
