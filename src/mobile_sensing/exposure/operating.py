"""Operating-state exposure from disjoint retained activities, with depot exclusion."""

import bisect
import math
from collections import defaultdict
from dataclasses import replace

import numpy as np
from shapely.geometry import Point
from shapely.strtree import STRtree

from mobile_sensing.artifacts import verify_partitioned_artifact, partition_file
import pandas as pd
from mobile_sensing.contracts import LocationRef, SparseExposureRow, VehicleKey, VehicleSpec
from mobile_sensing.exposure.models import AllocationResult, VehicleExposureDiagnostic
from mobile_sensing.exposure.allocation import ExposureAllocationError


def read_table(root, reference, name):
    artifact = verify_partitioned_artifact(
        artifact_root=root, collection="datasets", reference=reference
    )
    table = next(t for t in artifact.manifest.tables if t.name == name)
    return pd.read_parquet(partition_file(artifact.directory / table.relative_path, 0))


OPERATING_ALLOCATION_VERSION = "operating-activity-exposure@1"


def stationary_context(root, source, grid, retained_locations=None):
    locations = {
        row.location_id: LocationRef.model_validate_json(row.location_json)
        for row in read_table(root, source, "resolved_locations").itertuples()
    }
    locations.update(retained_locations or {})
    specs = [
        VehicleSpec.model_validate_json(row.spec_json)
        for row in read_table(root, source, "physical_catalog").itertuples()
    ]
    depots = {
        (s.key.fleet_id, s.key.vehicle_id): locations[s.depot_location_id].node_id
        for s in specs
        if s.depot_location_id is not None
    }
    ordered = grid.sort_values("cell_id")
    geometries = list(ordered.sensing_geometry)
    cell_ids = list(ordered.cell_id)
    tree = STRtree(geometries)
    cells = {}
    for key, value in locations.items():
        if value.snapped_x is None or value.snapped_y is None:
            raise ExposureAllocationError(f"Stationary location {key} lacks resolved coordinates")
        point = Point(value.snapped_x, value.snapped_y)
        hits = tree.query(point)
        # Shared boundaries have one stable owner; no duplicate residence time.
        owners = [str(cell_ids[i]) for i in hits if geometries[i].covers(point)]
        cells[key] = min(owners) if owners else None
    return locations, cells, depots


def add_stationary_exposure(
    movement,
    activities,
    *,
    time_axis,
    locations,
    location_cells,
    depot_nodes,
    operating_fleets,
    idle_break_seconds=None,
):
    """Add service/wait/idle intervals without reading movements a second time."""
    active = frozenset(operating_fleets)
    breaks = idle_break_seconds or {}
    ordered = activities.sort_values(
        ["replication_id", "fleet_id", "vehicle_id", "start_s", "end_s"], kind="stable"
    )
    if (
        not np.isfinite(ordered[["start_s", "end_s"]].to_numpy()).all()
        or (ordered.end_s <= ordered.start_s).any()
    ):
        raise ExposureAllocationError("Activity intervals require positive finite durations")
    for _, group in ordered.groupby(["replication_id", "fleet_id", "vehicle_id"], sort=False):
        if np.any(group.start_s.to_numpy()[1:] < group.end_s.to_numpy()[:-1] - 1e-7):
            raise ExposureAllocationError("Overlapping activities would double count sensing")
    values = defaultdict(float)
    for row in movement.rows:
        values[
            (
                row.replication_id,
                row.vehicle.fleet_id,
                row.vehicle.vehicle_id,
                row.cell_id,
                row.time_bin_id,
            )
        ] += row.duration_s
    diagnostics = {
        (d.replication_id, d.vehicle.fleet_id, d.vehicle.vehicle_id): d
        for d in movement.diagnostics
    }
    totals = defaultdict(lambda: defaultdict(float))
    edges = [time_axis.bins[0].start_s, *[b.end_s for b in time_axis.bins]]
    selected = ordered[(ordered.fleet_id.isin(active)) & (ordered.activity_kind != "movement")]
    for row in selected.itertuples():
        if row.activity_kind not in {"service", "wait", "idle"}:
            raise ExposureAllocationError(f"Unsupported stationary activity {row.activity_kind}")
        key = (str(row.replication_id), str(row.fleet_id), str(row.vehicle_id))
        start, end = max(row.start_s, edges[0]), min(row.end_s, edges[-1])
        if end <= start:
            continue
        duration = end - start
        if row.location_id not in locations:
            raise ExposureAllocationError(f"Missing retained stationary location {row.location_id}")
        node = locations[row.location_id].node_id
        if node == depot_nodes.get((row.fleet_id, row.vehicle_id)) and node is not None:
            totals[key]["depot"] += duration
            continue
        threshold = breaks.get(row.fleet_id)
        if (
            row.activity_kind == "idle"
            and threshold is not None
            and row.end_s - row.start_s >= threshold
        ):
            totals[key]["offduty"] += duration
            continue
        totals[key]["stationary"] += duration
        cell = location_cells[row.location_id]
        if cell is None:
            totals[key]["outside"] += duration
            continue
        totals[key]["inside"] += duration
        first = max(0, bisect.bisect_right(edges, start) - 1)
        for index in range(first, len(time_axis.bins)):
            b = time_axis.bins[index]
            if b.start_s >= end:
                break
            overlap = min(end, b.end_s) - max(start, b.start_s)
            if overlap > 0:
                values[(*key, cell, b.time_bin_id)] += overlap
    for key, added in totals.items():
        previous = diagnostics.get(
            key,
            VehicleExposureDiagnostic(
                replication_id=key[0],
                vehicle=VehicleKey(fleet_id=key[1], vehicle_id=key[2]),
                active_moving_time_s=0,
                in_grid_duration_s=0,
                outside_grid_duration_s=0,
                conservation_residual_s=0,
            ),
        )
        diagnostics[key] = replace(
            previous,
            stationary_time_s=added["stationary"],
            excluded_depot_time_s=added["depot"],
            excluded_offduty_time_s=added["offduty"],
            in_grid_duration_s=previous.in_grid_duration_s + added["inside"],
            outside_grid_duration_s=previous.outside_grid_duration_s + added["outside"],
            conservation_residual_s=previous.conservation_residual_s
            + added["stationary"]
            - added["inside"]
            - added["outside"],
        )
    by_bin = defaultdict(float)
    for (r, f, v, cell, b), duration in values.items():
        by_bin[(r, f, v, b)] += duration
    widths = {b.time_bin_id: b.end_s - b.start_s for b in time_axis.bins}
    if any(duration > widths[key[-1]] + 1e-6 for key, duration in by_bin.items()):
        raise ExposureAllocationError("Operating exposure exceeds the per-vehicle reporting bin")
    for d in diagnostics.values():
        if not math.isclose(
            d.active_moving_time_s + d.stationary_time_s,
            d.in_grid_duration_s + d.outside_grid_duration_s,
            rel_tol=1e-10,
            abs_tol=1e-6,
        ):
            raise ExposureAllocationError("Operating exposure is not conserved")
    return AllocationResult(
        tuple(
            SparseExposureRow(
                replication_id=k[0],
                vehicle=VehicleKey(fleet_id=k[1], vehicle_id=k[2]),
                cell_id=k[3],
                time_bin_id=k[4],
                duration_s=value,
            )
            for k, value in sorted(values.items())
            if value > 0
        ),
        tuple(diagnostics[k] for k in sorted(diagnostics)),
    )
