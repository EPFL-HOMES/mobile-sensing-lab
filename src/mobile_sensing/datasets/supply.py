"""Transactional physical-vehicle catalog and separate area-assignment import."""

from __future__ import annotations

from collections import defaultdict
import json
from typing import Any

import pandas as pd
from pydantic import ValidationError

from mobile_sensing.contracts import (
    ArtifactDependency,
    CatalogIdentity,
    LocationRef,
    ValidationIssue,
    VehicleKey,
    VehicleSpec,
    canonical_json_text,
    scientific_hash,
    scientific_projection,
    stable_id,
)
from mobile_sensing.datasets.models import (
    AreaAssignmentMapping,
    NormalizedSupply,
    TabularSource,
    VehicleImportMapping,
)
from mobile_sensing.datasets.parsing import (
    LocationResolver,
    issue,
    parse_float,
    parse_identifier,
    parse_time,
)
from mobile_sensing.datasets.source import iter_source_chunks
from mobile_sensing.datasets.storage import publish_dataset, verified_dataset_directory


SUPPLY_NORMALIZATION_VERSION = "transactional-supply-normalization@1"


class SupplyImportError(ValueError):
    def __init__(self, issues: tuple[ValidationIssue, ...]) -> None:
        super().__init__(f"supply normalization failed with {len(issues)} error(s)")
        self.issues = issues


def _rows(source: TabularSource, columns: dict[str, str], *, chunk_size: int, max_rows: int):
    result: list[tuple[int, dict[str, Any]]] = []
    count = 0
    checked = False
    for chunk in iter_source_chunks(source, chunk_size=chunk_size):
        if not checked:
            missing = sorted(set(columns.values()) - set(chunk.columns.astype(str)))
            if missing:
                raise ValueError(f"source is missing mapped columns: {missing}")
            checked = True
        for row in chunk.to_dict(orient="records"):
            count += 1
            if count > max_rows:
                raise ValueError(f"source exceeds configured max_rows={max_rows}")
            result.append((count, row))
    if not checked:
        raise ValueError("source contains no tabular schema")
    return result


def _location(
    *,
    row: dict[str, Any],
    source_row: int,
    vehicle_id: str,
    role: str,
    source: TabularSource,
    mapping: VehicleImportMapping,
    resolver: LocationResolver,
) -> LocationRef | None:
    if mapping.location_representation == "ids":
        logical = f"{role}_location_id"
        if logical not in mapping.columns:
            return None
        raw = row[mapping.columns[logical]]
        if raw == "" or raw is None:
            return None
        return resolver.by_id(parse_identifier(raw))
    x_key, y_key = f"{role}_x", f"{role}_y"
    if x_key not in mapping.columns and y_key not in mapping.columns:
        return None
    if (x_key in mapping.columns) != (y_key in mapping.columns):
        raise ValueError(f"{role} coordinates require both x and y mappings")
    raw_x, raw_y = row[mapping.columns[x_key]], row[mapping.columns[y_key]]
    if (raw_x in (None, "")) != (raw_y in (None, "")):
        raise ValueError(f"{role} coordinates must both be present or both absent")
    if raw_x in (None, ""):
        return None
    location_id = stable_id(
        "location",
        {
            "source": source.registration.content_hash,
            "vehicle_id": vehicle_id,
            "role": role,
            "x": str(raw_x),
            "y": str(raw_y),
            "crs": mapping.source_crs,
        },
    )
    assert mapping.source_crs is not None
    return resolver.by_coordinates(
        location_id=location_id,
        x=raw_x,
        y=raw_y,
        source_crs=mapping.source_crs,
    )


def normalize_supply(
    vehicle_source: TabularSource,
    vehicle_mapping: VehicleImportMapping,
    *,
    artifact_root,
    resolver: LocationResolver,
    area_assignment_source: TabularSource | None = None,
    area_assignment_mapping: AreaAssignmentMapping | None = None,
    known_area_ids: set[str] | None = None,
    chunk_size: int = 10_000,
    max_rows: int = 1_000_000,
) -> NormalizedSupply:
    """Publish a fixed catalog only after whole-file assignment validation succeeds."""

    if (area_assignment_source is None) != (area_assignment_mapping is None):
        raise ValueError("area assignment source and mapping must be supplied together")
    raw_vehicles = _rows(
        vehicle_source, vehicle_mapping.columns, chunk_size=chunk_size, max_rows=max_rows
    )
    staged: dict[str, tuple[VehicleSpec, str, dict[str, LocationRef]]] = {}
    errors: list[ValidationIssue] = []
    duplicate_ids: set[str] = set()
    for source_row, row in raw_vehicles:
        vehicle_id: str | None = None
        try:
            vehicle_id = parse_identifier(row[vehicle_mapping.columns["vehicle_id"]])
            if vehicle_id in staged:
                duplicate_ids.add(vehicle_id)
                raise ValueError("duplicate vehicle_id")
            start = parse_time(
                row[vehicle_mapping.columns["availability_start"]], vehicle_mapping.time
            )
            end = parse_time(row[vehicle_mapping.columns["availability_end"]], vehicle_mapping.time)
            initial = _location(
                row=row,
                source_row=source_row,
                vehicle_id=vehicle_id,
                role="initial",
                source=vehicle_source,
                mapping=vehicle_mapping,
                resolver=resolver,
            )
            if initial is None:
                raise ValueError("initial location is required")
            depot = _location(
                row=row,
                source_row=source_row,
                vehicle_id=vehicle_id,
                role="depot",
                source=vehicle_source,
                mapping=vehicle_mapping,
                resolver=resolver,
            )
            capacity = (
                None
                if vehicle_mapping.capacity_mode == "none"
                else parse_float(row[vehicle_mapping.columns["capacity"]], nonnegative=True)
            )
            spec = VehicleSpec(
                key=VehicleKey(fleet_id=vehicle_mapping.fleet_id, vehicle_id=vehicle_id),
                availability_start_s=start,
                availability_end_s=end,
                initial_location_id=initial.location_id,
                depot_location_id=depot.location_id if depot is not None else None,
                capacity_mode=vehicle_mapping.capacity_mode,
                capacity=capacity,
                quantity_unit=vehicle_mapping.quantity_unit,
                assigned_area_ids=(),
                identity_provenance="uploaded",
            )
            source_ref = f"{vehicle_source.registration.dataset_id}:row:{source_row}"
            row_locations = {initial.location_id: initial}
            if depot is not None:
                row_locations[depot.location_id] = depot
            staged[vehicle_id] = (spec, source_ref, row_locations)
        except (ValueError, TypeError, KeyError, ValidationError) as exc:
            errors.append(
                issue(
                    "INVALID_VEHICLE_ROW",
                    str(exc),
                    dataset_id=vehicle_source.registration.dataset_id,
                    source_row=source_row,
                    field="row",
                )
            )
    for duplicate in duplicate_ids:
        staged.pop(duplicate, None)
    if not staged and not errors:
        errors.append(
            issue(
                "EMPTY_VEHICLE_CATALOG",
                "vehicle catalog must contain at least one valid physical vehicle",
                dataset_id=vehicle_source.registration.dataset_id,
                source_row=None,
                field="vehicle_id",
            )
        )

    assignments: dict[str, set[str]] = defaultdict(set)
    assignment_rows: list[dict[str, object]] = []
    if area_assignment_source is not None and area_assignment_mapping is not None:
        if known_area_ids is None:
            raise ValueError("area assignments require an independently supplied area-ID set")
        for source_row, row in _rows(
            area_assignment_source,
            area_assignment_mapping.columns,
            chunk_size=chunk_size,
            max_rows=max_rows,
        ):
            try:
                vehicle_id = parse_identifier(row[area_assignment_mapping.columns["vehicle_id"]])
                area_id = parse_identifier(row[area_assignment_mapping.columns["area_id"]])
                fleet_id = (
                    area_assignment_mapping.fixed_fleet_id
                    if area_assignment_mapping.fixed_fleet_id is not None
                    else parse_identifier(row[area_assignment_mapping.columns["fleet_id"]])
                )
                if fleet_id != vehicle_mapping.fleet_id:
                    raise ValueError("area assignment fleet does not match the vehicle catalog")
                if vehicle_id not in staged:
                    raise ValueError("area assignment references an unknown vehicle")
                if area_id not in known_area_ids:
                    raise ValueError("area assignment references an unknown area")
                if area_id in assignments[vehicle_id]:
                    raise ValueError("duplicate vehicle-area assignment")
                assignments[vehicle_id].add(area_id)
                assignment_rows.append(
                    {
                        "fleet_id": fleet_id,
                        "vehicle_id": vehicle_id,
                        "area_id": area_id,
                        "source_record_ref": (
                            f"{area_assignment_source.registration.dataset_id}:row:{source_row}"
                        ),
                    }
                )
            except (ValueError, TypeError, KeyError) as exc:
                errors.append(
                    issue(
                        "INVALID_AREA_ASSIGNMENT",
                        str(exc),
                        dataset_id=area_assignment_source.registration.dataset_id,
                        source_row=source_row,
                        field="row",
                    )
                )
    if errors:
        raise SupplyImportError(tuple(errors))

    vehicles = tuple(
        staged[vehicle_id][0].model_copy(
            update={"assigned_area_ids": tuple(sorted(assignments[vehicle_id]))}
        )
        for vehicle_id in sorted(staged)
    )
    locations_by_id = {
        location_id: location
        for vehicle_id in sorted(staged)
        for location_id, location in staged[vehicle_id][2].items()
    }
    locations = tuple(locations_by_id[key] for key in sorted(locations_by_id))
    physical_metadata = [
        {
            "key": vehicle.key,
            "initial_location_id": vehicle.initial_location_id,
            "depot_location_id": vehicle.depot_location_id,
            "capacity_mode": vehicle.capacity_mode,
            "capacity": vehicle.capacity,
            "quantity_unit": vehicle.quantity_unit,
            "assigned_area_ids": vehicle.assigned_area_ids,
            "identity_provenance": vehicle.identity_provenance,
        }
        for vehicle in vehicles
    ]
    physical_hash = scientific_hash(physical_metadata)
    vehicle_keys = tuple(vehicle.key for vehicle in vehicles)
    catalog = CatalogIdentity(
        catalog_id=stable_id(
            "catalog", {"vehicle_keys": vehicle_keys, "physical_metadata_hash": physical_hash}
        ),
        vehicle_keys=vehicle_keys,
        physical_metadata_hash=physical_hash,
        catalog_hash=scientific_hash(
            {
                "vehicle_keys": [[item.fleet_id, item.vehicle_id] for item in vehicle_keys],
                "physical_metadata_hash": physical_hash,
            }
        ),
    )
    mapping_identity = {
        "vehicle_mapping": vehicle_mapping,
        "area_assignment_mapping": area_assignment_mapping,
    }
    mapping_id = stable_id("mapping", mapping_identity)
    vehicle_rows = []
    for vehicle in vehicles:
        row = vehicle.model_dump(mode="json")
        row["fleet_id"] = vehicle.key.fleet_id
        row["vehicle_id"] = vehicle.key.vehicle_id
        row.pop("key")
        row["assigned_area_ids"] = canonical_json_text(vehicle.assigned_area_ids)
        row["source_record_ref"] = staged[vehicle.key.vehicle_id][1]
        vehicle_rows.append(row)
    frames = {
        "area_assignments": pd.DataFrame(
            sorted(
                assignment_rows,
                key=lambda item: (
                    str(item["fleet_id"]),
                    str(item["vehicle_id"]),
                    str(item["area_id"]),
                ),
            ),
            columns=("fleet_id", "vehicle_id", "area_id", "source_record_ref"),
        ),
        "catalog_identity": pd.DataFrame([catalog.model_dump(mode="json")])
        .assign(vehicle_keys_json=canonical_json_text(catalog.vehicle_keys))
        .drop(columns="vehicle_keys"),
        "import_mapping": pd.DataFrame(
            [{"mapping_id": mapping_id, "mapping_json": canonical_json_text(mapping_identity)}],
            columns=("mapping_id", "mapping_json"),
        ),
        "locations": pd.DataFrame(
            [
                {
                    **item.model_dump(mode="json"),
                    "source_record_ref": next(
                        staged[vehicle_id][1]
                        for vehicle_id in sorted(staged)
                        if item.location_id in staged[vehicle_id][2]
                    ),
                }
                for item in locations
            ],
            columns=(
                "location_id",
                "original_x",
                "original_y",
                "original_crs",
                "node_id",
                "snapped_x",
                "snapped_y",
                "snap_distance_m",
                "resolution_status",
                "source_record_ref",
            ),
        ),
        "validation_issues": pd.DataFrame(
            columns=(
                "severity",
                "code",
                "field_path",
                "message",
                "corrective_action",
                "dataset_id",
                "table",
                "source_row",
                "task_id",
            )
        ),
        "vehicle_catalog": pd.DataFrame(
            vehicle_rows,
            columns=(
                "availability_start_s",
                "availability_end_s",
                "initial_location_id",
                "depot_location_id",
                "capacity_mode",
                "capacity",
                "quantity_unit",
                "assigned_area_ids",
                "identity_provenance",
                "fleet_id",
                "vehicle_id",
                "source_record_ref",
            ),
        ),
    }
    dependencies = [
        ArtifactDependency(
            role="environment",
            artifact_id=resolver.environment.artifact_id,
            content_hash=resolver.environment.content_hash,
        ),
        ArtifactDependency(
            role="raw_vehicle_catalog",
            artifact_id=vehicle_source.registration.dataset_id,
            content_hash=vehicle_source.registration.content_hash,
        ),
    ]
    if area_assignment_source is not None:
        dependencies.append(
            ArtifactDependency(
                role="raw_area_assignments",
                artifact_id=area_assignment_source.registration.dataset_id,
                content_hash=area_assignment_source.registration.content_hash,
            )
        )
    resolved_config = {
        "mapping": scientific_projection(mapping_identity),
        "mapping_id": mapping_id,
        "catalog": scientific_projection(catalog),
        "normalized_output_hash": scientific_hash(
            {
                "vehicles": vehicles,
                "locations": locations,
                "area_assignments": assignment_rows,
            }
        ),
    }
    reference, directory = publish_dataset(
        artifact_root=artifact_root,
        resolved_config=resolved_config,
        dependencies=tuple(sorted(dependencies, key=lambda item: item.role)),
        algorithm_versions={"supply_normalization": SUPPLY_NORMALIZATION_VERSION},
        frames=frames,
    )
    return NormalizedSupply(
        reference=reference,
        directory=directory,
        mapping_id=mapping_id,
        catalog=catalog,
        locations=locations,
        vehicles=vehicles,
        issues=(),
    )


def read_saved_supply_mappings(
    reference, *, artifact_root
) -> tuple[VehicleImportMapping, AreaAssignmentMapping | None]:
    """Verify and reconstruct the vehicle and optional area-assignment mappings."""

    directory, _ = verified_dataset_directory(reference, artifact_root=artifact_root)
    table = pd.read_parquet(directory / "import_mapping.parquet")
    if len(table) != 1:
        raise ValueError("normalized supply artifact must contain exactly one saved mapping")
    payload = json.loads(str(table.iloc[0]["mapping_json"]))
    vehicle = VehicleImportMapping.model_validate(payload["vehicle_mapping"])
    area_payload = payload["area_assignment_mapping"]
    area = None if area_payload is None else AreaAssignmentMapping.model_validate(area_payload)
    return vehicle, area
