"""Mean fleet projections over the complete joint replication set."""

import math
from collections import defaultdict

import numpy as np
import pyarrow.parquet as pq

from mobile_sensing.api.queries import (
    ArtifactCatalog,
    QueryLimitExceeded,
    _table_paths,
    _filtered_rows,
    _cached_map,
    _retain_map,
)
from mobile_sensing.contracts import scientific_hash


def fleet_summary(
    root, exposure_id, *, fleet_id=None, vehicle_id=None, time_start_s=None, time_end_s=None, limits
):
    catalog = ArtifactCatalog(root)
    exposure = catalog.locate(exposure_id)
    if exposure.reference.artifact_kind != "exposure":
        raise ValueError("Fleet results require a complete exposure")
    dependency = next(d for d in exposure.manifest.dependencies if d.role == "simulation")
    simulation = catalog.locate(dependency.artifact_id)
    if simulation.reference.content_hash != dependency.content_hash:
        raise ValueError("Exposure and simulation source identities disagree")
    bins = _filtered_rows(exposure, "time_bins", lambda _: True, limit=10000)
    bins.sort(key=lambda row: row["start_s"])
    replications = _filtered_rows(exposure, "replication_status", lambda _: True, limit=10000)
    if not replications or not all(row["complete"] for row in replications):
        raise ValueError("Fleet means require every retained replication to be complete")
    ids = {row["replication_id"] for row in replications}
    vehicles = _filtered_rows(
        exposure,
        "vehicle_catalog",
        lambda row: (fleet_id is None or row["fleet_id"] == fleet_id)
        and (vehicle_id is None or row["vehicle_id"] == vehicle_id),
        limit=100000,
    )
    if not vehicles:
        raise ValueError("Vehicle selection is outside the frozen catalog")
    fleets = sorted({row["fleet_id"] for row in vehicles})
    if len(ids) * len(fleets) * len(bins) > 1_000_000:
        raise QueryLimitExceeded("Fleet summary axis limit exceeded; select one fleet")
    start = bins[0]["start_s"] if time_start_s is None else time_start_s
    end = bins[-1]["end_s"] if time_end_s is None else time_end_s
    if (
        not math.isfinite(start)
        or not math.isfinite(end)
        or start >= end
        or start not in {b["start_s"] for b in bins}
        or end not in {b["end_s"] for b in bins}
    ):
        raise ValueError("Select an increasing window on retained reporting-bin boundaries")
    key = scientific_hash(
        {
            "projection": "mean-fleet@2",
            "root": str(root),
            "exposure": exposure.reference,
            "simulation": simulation.reference,
            "fleet": fleet_id,
            "vehicle": vehicle_id,
            "start": start,
            "end": end,
        }
    )
    if (cached := _cached_map(key)) is not None:
        return cached
    stats = {(r, f): defaultdict(float) for r in ids for f in fleets}
    statuses = {(r, f): defaultdict(int) for r in ids for f in fleets}
    carry_before, carry_active = defaultdict(set), defaultdict(set)
    arrivals, active = np.zeros(len(bins)), np.zeros(len(bins))
    edges = np.array([bins[0]["start_s"], *[b["end_s"] for b in bins]])
    if any(bins[i]["end_s"] != bins[i + 1]["start_s"] for i in range(len(bins) - 1)):
        raise ValueError("Reporting bins must be contiguous")

    def batches(table):
        for path in _table_paths(simulation, table):
            for batch in pq.ParquetFile(path).iter_batches(batch_size=20000):
                frame = batch.to_pandas()
                frame = frame[frame.replication_id.isin(ids) & frame.fleet_id.isin(fleets)]
                if vehicle_id is not None:
                    frame = frame[frame.vehicle_id == vehicle_id]
                if not frame.empty:
                    yield frame

    for frame in batches("task_outcomes"):
        frame = frame[frame.kind == "service"]
        released = frame.release_s.to_numpy()
        indices = np.searchsorted(edges, released, side="right") - 1
        valid = (indices >= 0) & (indices < len(bins))
        arrivals += np.bincount(indices[valid], minlength=len(bins))
        for identity, group in frame.groupby(["replication_id", "fleet_id"], sort=False):
            carry_before[identity].update(group.loc[group.release_s < start, "task_id"])
            selected = group[(group.release_s >= start) & (group.release_s < end)]
            stats[identity]["released"] += len(selected)
            for status, count in selected.status.value_counts().items():
                statuses[identity][status] += int(count)

    for frame in batches("activity_intervals"):
        starts, ends = frame.start_s.to_numpy(), frame.end_s.to_numpy()
        # Bounded vector batches; no vehicle/cell/replication tensor is constructed.
        for i, b in enumerate(bins):
            active[i] += np.maximum(
                0, np.minimum(ends, b["end_s"]) - np.maximum(starts, b["start_s"])
            ).sum()
        frame = frame.assign(
            clipped=np.maximum(0, np.minimum(ends, end) - np.maximum(starts, start))
        )
        frame = frame[frame.clipped > 0]
        for identity, group in frame.groupby(["replication_id", "fleet_id"], sort=False):
            stats[identity]["active_s"] += float(group.clipped.sum())
            carry_active[identity].update(group.task_id.dropna())

    count = len(ids)
    fleet_rows = []
    for fleet in fleets:
        rows = [stats[(r, fleet)] for r in sorted(ids)]
        counts = [statuses[(r, fleet)].get("completed", 0) for r in sorted(ids)]
        rates = [
            completed / row["released"]
            for completed, row in zip(counts, rows)
            if row["released"] > 0
        ]
        names = sorted({name for r in ids for name in statuses[(r, fleet)]})
        fleet_rows.append(
            {
                "fleet_id": fleet,
                "catalog_size": sum(v["fleet_id"] == fleet for v in vehicles),
                "mean_released_tasks": math.fsum(row["released"] for row in rows) / count,
                "mean_completed_tasks": math.fsum(counts) / count,
                "mean_completion_rate": math.fsum(rates) / len(rates) if rates else None,
                "completion_rate_replications": len(rates),
                "mean_active_vehicles": math.fsum(row["active_s"] for row in rows)
                / count
                / (end - start),
                "mean_carry_in_tasks": sum(
                    len(carry_before[(r, fleet)] & carry_active[(r, fleet)]) for r in ids
                )
                / count,
                "mean_status_counts": {
                    name: sum(statuses[(r, fleet)][name] for r in ids) / count for name in names
                },
            }
        )
    result = {
        "exposure_id": exposure_id,
        "simulation_id": simulation.reference.artifact_id,
        "replications_R": count,
        "statistic": "mean",
        "time_start_s": start,
        "time_end_s": end,
        "fleets": fleet_rows,
        "time_summary": [
            {
                "time_bin_id": b["time_bin_id"],
                "start_s": b["start_s"],
                "end_s": b["end_s"],
                "mean_arrivals": float(arrivals[i] / count),
                "mean_active_vehicles": float(active[i] / count / (b["end_s"] - b["start_s"])),
            }
            for i, b in enumerate(bins)
        ],
        "task_semantics": "final_outcomes_of_release_cohort; rates_exclude_empty_cohorts",
        "complete": True,
    }
    return _retain_map(key, result)
