"""Piecewise-constant Poisson demand with offline/online identity."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import Literal

import numpy as np

from mobile_sensing.contracts import (
    ContractModel,
    GeneratorDemandConfig,
    NonNegativeFloat,
    OpaqueId,
    Task,
    TaskStep,
    stable_id,
)
from mobile_sensing.simulation.rng import SemanticRngStreams


DEMAND_GENERATOR_VERSION = "piecewise-constant-poisson@1"


class LocationWeight(ContractModel):
    location_id: OpaqueId
    weight: NonNegativeFloat


class OdWeight(ContractModel):
    origin_location_id: OpaqueId
    destination_location_id: OpaqueId
    weight: NonNegativeFloat


def _normalized(weights: Sequence[float]) -> np.ndarray:
    values = np.asarray(weights, dtype=np.float64)
    if values.ndim != 1 or len(values) == 0 or not np.isfinite(values).all():
        raise ValueError("weights must be a nonempty finite vector")
    if (values < 0).any() or values.sum(dtype=np.float64) <= 0:
        raise ValueError("weights must be nonnegative with positive total mass")
    return values / values.sum(dtype=np.float64)


def _quantity_deltas(
    *,
    structure: str,
    capacity_mode: Literal["none", "consumable", "occupancy"],
    quantity: float | None,
) -> tuple[float | None, ...]:
    if capacity_mode == "none":
        if quantity is not None:
            raise ValueError("quantity requires a finite-capacity fleet")
        return (None,) if structure == "location" else (None, None)
    if quantity is None:
        return (None,) if structure == "location" else (None, None)
    if capacity_mode == "occupancy":
        if structure != "od":
            raise ValueError("occupancy demand requires OD structure")
        return (-quantity, quantity)
    return (-quantity,) if structure == "location" else (None, -quantity)


def iter_piecewise_poisson(
    config: GeneratorDemandConfig,
    *,
    fleet_id: str,
    replication_id: str,
    capacity_mode: Literal["none", "consumable", "occupancy"],
    rng: SemanticRngStreams,
    location_weights: Sequence[LocationWeight] = (),
    od_weights: Sequence[OdWeight] = (),
) -> Iterator[Task]:
    """Materialize one interval at a time; interval streams make chunk timing irrelevant."""

    if config.adapter != "demand.poisson_piecewise_constant@1":
        raise ValueError("unsupported demand generator")
    if config.structure == "location":
        if od_weights or not location_weights:
            raise ValueError("location generation requires only location weights")
        ordered_locations = tuple(sorted(location_weights, key=lambda item: item.location_id))
        if len({item.location_id for item in ordered_locations}) != len(ordered_locations):
            raise ValueError("location weights require unique location IDs")
        probabilities = _normalized([item.weight for item in ordered_locations])
    else:
        if location_weights or not od_weights:
            raise ValueError("OD generation requires only OD weights")
        ordered_od = tuple(
            sorted(
                od_weights,
                key=lambda item: (item.origin_location_id, item.destination_location_id),
            )
        )
        if len(
            {(item.origin_location_id, item.destination_location_id) for item in ordered_od}
        ) != len(ordered_od):
            raise ValueError("OD weights require unique origin-destination keys")
        probabilities = _normalized([item.weight for item in ordered_od])
    quantity = config.parameters.quantity
    deltas = _quantity_deltas(
        structure=config.structure, capacity_mode=capacity_mode, quantity=quantity
    )
    for interval_index, interval in enumerate(config.parameters.intervals):
        interval_label = stable_id(
            "interval",
            {
                "index": interval_index,
                "start_s": interval.start_s,
                "end_s": interval.end_s,
                "rate_tasks_per_s": interval.rate_tasks_per_s,
            },
        )
        arrival_rng = rng.stream("demand.arrivals", replication_id, fleet_id, interval_label)
        expected = interval.rate_tasks_per_s * (interval.end_s - interval.start_s)
        count = int(arrival_rng.poisson(expected))
        if count == 0:
            continue
        releases = np.sort(
            arrival_rng.uniform(interval.start_s, interval.end_s, size=count), kind="stable"
        )
        choice_rng = rng.stream(
            "demand.locations" if config.structure == "location" else "demand.od",
            replication_id,
            fleet_id,
            interval_label,
        )
        choices = choice_rng.choice(len(probabilities), size=count, p=probabilities)
        for rank, (release, choice) in enumerate(zip(releases, choices, strict=True)):
            task_id = stable_id(
                "task",
                {
                    "generator": DEMAND_GENERATOR_VERSION,
                    "replication_id": replication_id,
                    "fleet_id": fleet_id,
                    "interval": interval_label,
                    "rank": rank,
                },
            )
            if config.structure == "location":
                item = ordered_locations[int(choice)]
                steps = (
                    TaskStep(
                        step_index=1,
                        location_id=item.location_id,
                        service_duration_s=config.parameters.service_duration_s,
                        quantity_delta=deltas[0],
                    ),
                )
            else:
                item = ordered_od[int(choice)]
                steps = (
                    TaskStep(
                        step_index=1,
                        location_id=item.origin_location_id,
                        service_duration_s=0.0,
                        quantity_delta=deltas[0],
                    ),
                    TaskStep(
                        step_index=2,
                        location_id=item.destination_location_id,
                        service_duration_s=config.parameters.service_duration_s,
                        quantity_delta=deltas[1],
                    ),
                )
            yield Task(
                task_id=task_id,
                fleet_id=fleet_id,
                release_s=float(release),
                steps=steps,
                required_capacity=quantity,
                source_policy=DEMAND_GENERATOR_VERSION,
            )


def generate_piecewise_poisson(*args, **kwargs) -> tuple[Task, ...]:
    """Offline form of :func:`iter_piecewise_poisson` with identical records."""

    return tuple(iter_piecewise_poisson(*args, **kwargs))
