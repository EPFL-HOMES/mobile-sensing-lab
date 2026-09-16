"""Deterministic demand-balanced service areas for a frozen vehicle catalog."""

from dataclasses import dataclass

import numpy as np

from mobile_sensing.contracts import stable_id


AUTO_SERVICE_AREA_VERSION = "demand-balanced-recursive-bisection@1"


@dataclass(frozen=True)
class AutoServiceAreaResult:
    location_area_ids: dict[str, tuple[str, ...]]
    vehicle_area_ids: dict[str, tuple[str, ...]]
    report: dict


def _split(indices, area_count, coordinates, masses, identifiers):
    if area_count == 1:
        return [np.asarray(indices, dtype=int)]
    if len(indices) < area_count:
        raise ValueError("Auto service areas require at least one positive-demand cell per area")
    current = np.asarray(indices, dtype=int)
    spread = np.ptp(coordinates[current], axis=0)
    axis = int(np.argmax(spread))
    ordered = np.asarray(
        sorted(
            current, key=lambda i: (coordinates[i, axis], coordinates[i, 1 - axis], identifiers[i])
        ),
        dtype=int,
    )
    left_count = area_count // 2
    right_count = area_count - left_count
    candidates = range(left_count, len(ordered) - right_count + 1)
    target = float(masses[ordered].sum()) * left_count / area_count
    cumulative = np.cumsum(masses[ordered])
    cut = min(candidates, key=lambda value: (abs(float(cumulative[value - 1]) - target), value))
    return [
        *_split(ordered[:cut], left_count, coordinates, masses, identifiers),
        *_split(ordered[cut:], right_count, coordinates, masses, identifiers),
    ]


def automatic_service_areas(*, fleet_id, area_count, location_rows, vehicle_ids):
    """Partition positive expected demand and allocate every physical vehicle exactly once.

    ``location_rows`` contains ``(location_id, x, y, probability)``. Probabilities
    must already represent the configured whole-grid feature mixture after routing
    eligibility. Zero-demand resolved locations may be appended; they are assigned
    to their nearest positive-demand area centroid but do not affect balancing.
    """

    rows = sorted(location_rows, key=lambda row: row[0])
    vehicles = sorted(vehicle_ids)
    if area_count < 1:
        raise ValueError("Auto service-area count must be positive")
    if area_count > len(vehicles):
        raise ValueError("Auto service areas require at least one physical vehicle per area")
    positive = [row for row in rows if row[3] > 0]
    if len(positive) < area_count:
        raise ValueError("Auto service areas require at least one positive-demand cell per area")
    masses = np.asarray([row[3] for row in positive], dtype=float)
    if not np.isfinite(masses).all() or masses.sum() <= 0:
        raise ValueError("Auto service areas require finite positive expected demand mass")
    masses /= masses.sum()
    coordinates = np.asarray([[row[1], row[2]] for row in positive], dtype=float)
    identifiers = [row[0] for row in positive]
    groups = _split(np.arange(len(positive)), area_count, coordinates, masses, identifiers)
    summaries = []
    for group in groups:
        mass = float(masses[group].sum())
        centroid = np.average(coordinates[group], axis=0, weights=masses[group])
        summaries.append((group, mass, float(centroid[0]), float(centroid[1])))
    summaries.sort(key=lambda value: (value[2], value[3], identifiers[int(value[0][0])]))
    area_ids = [
        stable_id(
            "auto_service_area",
            {"version": AUTO_SERVICE_AREA_VERSION, "fleet": fleet_id, "rank": rank},
        )
        for rank in range(area_count)
    ]
    positive_membership = {}
    centroids = []
    area_masses = []
    positive_counts = []
    for area_id, (group, mass, x, y) in zip(area_ids, summaries, strict=True):
        centroids.append((x, y))
        area_masses.append(mass)
        positive_counts.append(len(group))
        for index in group:
            positive_membership[identifiers[int(index)]] = area_id
    location_membership = {}
    for location_id, x, y, _ in rows:
        area_id = positive_membership.get(location_id)
        if area_id is None:
            area_id = area_ids[
                min(
                    range(area_count),
                    key=lambda index: (
                        (x - centroids[index][0]) ** 2 + (y - centroids[index][1]) ** 2,
                        area_ids[index],
                    ),
                )
            ]
        location_membership[location_id] = (area_id,)
    remaining = len(vehicles) - area_count
    raw = np.asarray(area_masses) * remaining
    counts = np.ones(area_count, dtype=int) + np.floor(raw).astype(int)
    for index in sorted(
        range(area_count), key=lambda i: (-(raw[i] - np.floor(raw[i])), area_ids[i])
    )[: len(vehicles) - int(counts.sum())]:
        counts[index] += 1
    vehicle_membership = {}
    cursor = 0
    for area_id, count in zip(area_ids, counts, strict=True):
        for vehicle_id in vehicles[cursor : cursor + int(count)]:
            vehicle_membership[vehicle_id] = (area_id,)
        cursor += int(count)
    report_rows = []
    for index, area_id in enumerate(area_ids):
        report_rows.append(
            {
                "area_id": area_id,
                "expected_origin_demand_share": area_masses[index],
                "physical_vehicle_count": int(counts[index]),
                "positive_demand_cell_count": positive_counts[index],
                "resolved_location_count": sum(
                    area_id in values for values in location_membership.values()
                ),
                "centroid_x": centroids[index][0],
                "centroid_y": centroids[index][1],
            }
        )
    return AutoServiceAreaResult(
        location_area_ids=location_membership,
        vehicle_area_ids=vehicle_membership,
        report={
            "algorithm": AUTO_SERVICE_AREA_VERSION,
            "area_count": area_count,
            "allocation_rule": "one vehicle per area, then largest remainder by expected origin-demand mass",
            "areas": report_rows,
        },
    )
