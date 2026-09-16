"""Fixed generated vehicle catalogs and replication-specific availability."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from mobile_sensing.contracts import (
    CatalogIdentity,
    GeneratedSupplyConfig,
    LocationRef,
    ResolutionStatus,
    VehicleAvailability,
    VehicleKey,
    VehicleSpec,
    scientific_hash,
    stable_id,
)
from mobile_sensing.simulation.rng import SemanticRngStreams


SUPPLY_GENERATOR_VERSION = "fixed-catalog-supply@1"


def build_generated_catalog(
    config: GeneratedSupplyConfig,
    *,
    fleet_id: str,
    initial_locations: Sequence[LocationRef],
    depot_location_ids: Sequence[str | None] | None = None,
    area_assignments: Mapping[str, Sequence[str]] | None = None,
) -> tuple[CatalogIdentity, tuple[VehicleSpec, ...]]:
    """Build the physical catalog once; replication IDs are deliberately absent."""

    if len(initial_locations) != config.catalog_size:
        raise ValueError("initial_locations must contain exactly catalog_size resolved locations")
    if any(item.resolution_status != ResolutionStatus.RESOLVED for item in initial_locations):
        raise ValueError("generated initial locations must all be resolved")
    if depot_location_ids is not None and len(depot_location_ids) != config.catalog_size:
        raise ValueError("depot_location_ids must contain exactly catalog_size entries")
    if depot_location_ids is not None:
        paired_locations = tuple(
            sorted(
                zip(initial_locations, depot_location_ids, strict=True),
                key=lambda item: (item[0].location_id, item[1] or ""),
            )
        )
    else:
        paired_locations = tuple(
            (location, None)
            for location in sorted(initial_locations, key=lambda item: item.location_id)
        )
    if config.availability.kind == "simultaneous":
        envelope_start = config.availability.start_s
        envelope_end = config.availability.end_s
    else:
        envelope_start = config.availability.earliest_start_s
        envelope_end = config.availability.latest_start_s + config.availability.duration_s
    specs = []
    assignments = area_assignments or {}
    generated_vehicle_ids = {
        f"{config.catalog_id_namespace}_{index + 1:06d}" for index in range(config.catalog_size)
    }
    unknown_assignment_keys = sorted(set(assignments) - generated_vehicle_ids)
    if unknown_assignment_keys:
        raise ValueError(
            f"area assignments reference unknown generated vehicles: {unknown_assignment_keys}"
        )
    for index, (initial, depot_location_id) in enumerate(paired_locations):
        vehicle_id = f"{config.catalog_id_namespace}_{index + 1:06d}"
        specs.append(
            VehicleSpec(
                key=VehicleKey(fleet_id=fleet_id, vehicle_id=vehicle_id),
                availability_start_s=envelope_start,
                availability_end_s=envelope_end,
                initial_location_id=initial.location_id,
                depot_location_id=depot_location_id,
                capacity_mode=config.capacity.mode,
                capacity=config.capacity_value,
                quantity_unit=getattr(config.capacity, "unit", None),
                assigned_area_ids=tuple(sorted(assignments.get(vehicle_id, ()))),
                identity_provenance="generated",
            )
        )
    vehicles = tuple(sorted(specs, key=lambda item: (item.key.fleet_id, item.key.vehicle_id)))
    physical_metadata_hash = scientific_hash(
        [
            {
                "key": item.key,
                "initial_location_id": item.initial_location_id,
                "depot_location_id": item.depot_location_id,
                "capacity_mode": item.capacity_mode,
                "capacity": item.capacity,
                "quantity_unit": item.quantity_unit,
                "assigned_area_ids": item.assigned_area_ids,
                "identity_provenance": item.identity_provenance,
            }
            for item in vehicles
        ]
    )
    keys = tuple(item.key for item in vehicles)
    catalog_hash = scientific_hash(
        {
            "vehicle_keys": [[item.fleet_id, item.vehicle_id] for item in keys],
            "physical_metadata_hash": physical_metadata_hash,
        }
    )
    return (
        CatalogIdentity(
            catalog_id=stable_id("catalog", catalog_hash),
            vehicle_keys=keys,
            physical_metadata_hash=physical_metadata_hash,
            catalog_hash=catalog_hash,
        ),
        vehicles,
    )


def realize_availability(
    config: GeneratedSupplyConfig,
    catalog: Sequence[VehicleSpec],
    *,
    replication_id: str,
    rng: SemanticRngStreams,
) -> tuple[VehicleAvailability, ...]:
    """Realize windows without adding, removing, or renaming catalog vehicles."""

    if len(catalog) != config.catalog_size:
        raise ValueError("catalog size does not match generated supply configuration")
    rows = []
    for vehicle in sorted(catalog, key=lambda item: (item.key.fleet_id, item.key.vehicle_id)):
        if config.availability.kind == "simultaneous":
            start = config.availability.start_s
            end = config.availability.end_s
        else:
            generator = rng.stream(
                "supply.activation",
                replication_id,
                vehicle.key.fleet_id,
                vehicle.key.vehicle_id,
            )
            start = float(
                generator.uniform(
                    config.availability.earliest_start_s,
                    config.availability.latest_start_s,
                )
            )
            end = start + config.availability.duration_s
        rows.append(
            VehicleAvailability(
                replication_id=replication_id,
                vehicle=vehicle.key,
                active=True,
                availability_start_s=start,
                availability_end_s=end,
                initial_location_id=vehicle.initial_location_id,
            )
        )
    return tuple(rows)
