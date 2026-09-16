"""Transactional normalization of observed tasks and sparse OD rates."""

from __future__ import annotations

from collections import defaultdict
import json
from typing import Any

import pandas as pd
from pydantic import ValidationError

from mobile_sensing.contracts import (
    ArtifactDependency,
    LocationRef,
    Task,
    TaskStep,
    ValidationIssue,
    canonical_json_text,
    scientific_hash,
    scientific_projection,
    stable_id,
)
from mobile_sensing.datasets.models import (
    DemandImportMapping,
    NormalizedDemand,
    RateImportMapping,
    TabularSource,
)
from mobile_sensing.datasets.parsing import (
    LocationResolver,
    issue,
    parse_duration,
    parse_float,
    parse_identifier,
    parse_time,
)
from mobile_sensing.datasets.source import iter_source_chunks
from mobile_sensing.datasets.storage import publish_dataset
from mobile_sensing.datasets.storage import verified_dataset_directory


NORMALIZATION_VERSION = "transactional-demand-normalization@1"


class DemandImportError(ValueError):
    def __init__(self, issues: tuple[ValidationIssue, ...]) -> None:
        super().__init__(f"demand normalization failed with {len(issues)} error(s)")
        self.issues = issues


def _mapped(row: dict[str, Any], mapping, logical: str) -> Any:
    return row[mapping.columns[logical]]


def _read_rows(
    source: TabularSource, mapping, *, chunk_size: int, max_rows: int
) -> list[tuple[int, dict[str, Any]]]:
    rows: list[tuple[int, dict[str, Any]]] = []
    source_row = 0
    observed_columns: set[str] | None = None
    for chunk in iter_source_chunks(source, chunk_size=chunk_size):
        if observed_columns is None:
            observed_columns = set(chunk.columns.astype(str))
            missing = sorted(set(mapping.columns.values()) - observed_columns)
            if missing:
                raise ValueError(f"source is missing mapped columns: {missing}")
        for record in chunk.to_dict(orient="records"):
            source_row += 1
            if source_row > max_rows:
                raise ValueError(f"source exceeds configured max_rows={max_rows}")
            rows.append((source_row, record))
    if observed_columns is None:
        raise ValueError("source contains no tabular schema")
    return rows


def _resolve_location(
    *,
    row: dict[str, Any],
    source_row: int,
    task_id: str,
    role: str,
    mapping: DemandImportMapping,
    source: TabularSource,
    resolver: LocationResolver,
) -> LocationRef:
    if mapping.location_representation == "ids":
        logical = f"{role}_location_id" if mapping.structure == "od" else "location_id"
        return resolver.by_id(parse_identifier(_mapped(row, mapping, logical)))
    prefix = f"{role}_" if mapping.structure == "od" else ""
    location_id = stable_id(
        "location",
        {
            "source": source.registration.content_hash,
            "task_id": task_id,
            "role": role,
            "x": str(_mapped(row, mapping, f"{prefix}x")),
            "y": str(_mapped(row, mapping, f"{prefix}y")),
            "crs": mapping.source_crs,
        },
    )
    assert mapping.source_crs is not None
    return resolver.by_coordinates(
        location_id=location_id,
        x=_mapped(row, mapping, f"{prefix}x"),
        y=_mapped(row, mapping, f"{prefix}y"),
        source_crs=mapping.source_crs,
    )


def _duration(row: dict[str, Any], mapping: DemandImportMapping, logical: str) -> float:
    if logical in mapping.columns:
        return parse_duration(_mapped(row, mapping, logical), mapping.duration_unit)
    assert mapping.service_duration_default_s is not None
    return mapping.service_duration_default_s


def _quantity(row: dict[str, Any], mapping: DemandImportMapping) -> float | None:
    if mapping.quantity_mode == "none":
        return None
    logical = "quantity_delta" if mapping.quantity_mode == "signed_delta" else "quantity"
    return parse_float(_mapped(row, mapping, logical), nonnegative=logical == "quantity")


def _task_id(
    row: dict[str, Any], source_row: int, mapping: DemandImportMapping, source: TabularSource
) -> str:
    if "task_id" in mapping.columns:
        return parse_identifier(_mapped(row, mapping, "task_id"))
    return stable_id(
        "task",
        {"source": source.registration.content_hash, "logical_source_row": source_row},
    )


def _normalize_observed(
    source: TabularSource,
    mapping: DemandImportMapping,
    resolver: LocationResolver,
    *,
    chunk_size: int,
    max_rows: int,
) -> tuple[tuple[Task, ...], tuple[LocationRef, ...], tuple[ValidationIssue, ...]]:
    raw_rows = _read_rows(source, mapping, chunk_size=chunk_size, max_rows=max_rows)
    staged: dict[str, list[dict[str, Any]]] = defaultdict(list)
    issues: list[ValidationIssue] = []
    for source_row, row in raw_rows:
        task_id: str | None = None
        try:
            task_id = _task_id(row, source_row, mapping, source)
            staged[task_id].append({"row": row, "source_row": source_row})
        except (ValueError, TypeError, KeyError) as exc:
            issues.append(
                issue(
                    "INVALID_TASK_ID",
                    str(exc),
                    dataset_id=source.registration.dataset_id,
                    source_row=source_row,
                    field="task_id",
                    task_id=task_id,
                )
            )

    tasks: list[Task] = []
    locations: dict[str, LocationRef] = {}
    for task_id in sorted(staged):
        records = staged[task_id]
        task_issues: list[ValidationIssue] = []
        if mapping.structure != "ordered" and len(records) != 1:
            for record in records:
                task_issues.append(
                    issue(
                        "DUPLICATE_TASK_ID",
                        "task_id occurs more than once for a single-row layout",
                        dataset_id=source.registration.dataset_id,
                        source_row=record["source_row"],
                        field="task_id",
                        task_id=task_id,
                    )
                )
        parsed_steps: list[TaskStep] = []
        releases: list[float] = []
        task_locations: dict[str, LocationRef] = {}
        required_capacity: float | None = None
        for record in records:
            row = record["row"]
            source_row = record["source_row"]
            source_ref = f"{source.registration.dataset_id}:row:{source_row}"
            try:
                release = parse_time(_mapped(row, mapping, "release_time"), mapping.time)
                releases.append(release)
                quantity = _quantity(row, mapping)
                if mapping.structure == "location":
                    location = _resolve_location(
                        row=row,
                        source_row=source_row,
                        task_id=task_id,
                        role="service",
                        mapping=mapping,
                        source=source,
                        resolver=resolver,
                    )
                    task_locations[location.location_id] = location
                    required_capacity = quantity
                    parsed_steps.append(
                        TaskStep(
                            step_index=1,
                            location_id=location.location_id,
                            service_duration_s=_duration(row, mapping, "service_duration"),
                            quantity_delta=-quantity if quantity is not None else None,
                            source_record_refs=(source_ref,),
                        )
                    )
                elif mapping.structure == "od":
                    origin = _resolve_location(
                        row=row,
                        source_row=source_row,
                        task_id=task_id,
                        role="origin",
                        mapping=mapping,
                        source=source,
                        resolver=resolver,
                    )
                    destination = _resolve_location(
                        row=row,
                        source_row=source_row,
                        task_id=task_id,
                        role="destination",
                        mapping=mapping,
                        source=source,
                        resolver=resolver,
                    )
                    task_locations[origin.location_id] = origin
                    task_locations[destination.location_id] = destination
                    required_capacity = quantity
                    if mapping.quantity_mode == "occupancy":
                        deltas = (-quantity, quantity)
                    elif mapping.quantity_mode == "consumable":
                        deltas = (None, -quantity)
                    else:
                        deltas = (None, None)
                    parsed_steps.extend(
                        (
                            TaskStep(
                                step_index=1,
                                location_id=origin.location_id,
                                service_duration_s=_duration(
                                    row, mapping, "pickup_service_duration"
                                ),
                                quantity_delta=deltas[0],
                                source_record_refs=(source_ref,),
                            ),
                            TaskStep(
                                step_index=2,
                                location_id=destination.location_id,
                                service_duration_s=_duration(
                                    row, mapping, "dropoff_service_duration"
                                ),
                                quantity_delta=deltas[1],
                                source_record_refs=(source_ref,),
                            ),
                        )
                    )
                else:
                    location = _resolve_location(
                        row=row,
                        source_row=source_row,
                        task_id=task_id,
                        role="step",
                        mapping=mapping,
                        source=source,
                        resolver=resolver,
                    )
                    task_locations[location.location_id] = location
                    raw_index = parse_float(_mapped(row, mapping, "step_index"), nonnegative=True)
                    if not raw_index.is_integer() or raw_index < 1:
                        raise ValueError("step_index must be a positive integer")
                    scheduled = (
                        parse_time(_mapped(row, mapping, "scheduled_time"), mapping.time)
                        if "scheduled_time" in mapping.columns
                        else None
                    )
                    if scheduled is not None and scheduled < release:
                        raise ValueError("scheduled_time must not precede release_time")
                    parsed_steps.append(
                        TaskStep(
                            step_index=int(raw_index),
                            location_id=location.location_id,
                            scheduled_time_s=scheduled,
                            service_duration_s=_duration(row, mapping, "service_duration"),
                            quantity_delta=quantity,
                            source_record_refs=(source_ref,),
                        )
                    )
            except (ValueError, TypeError, KeyError, ValidationError) as exc:
                task_issues.append(
                    issue(
                        "INVALID_TASK_ROW",
                        str(exc),
                        dataset_id=source.registration.dataset_id,
                        source_row=source_row,
                        field="row",
                        task_id=task_id,
                    )
                )
        if releases and any(value != releases[0] for value in releases[1:]):
            task_issues.append(
                issue(
                    "INCONSISTENT_TASK_RELEASE",
                    "all rows of an ordered task must have one release_time",
                    dataset_id=source.registration.dataset_id,
                    source_row=records[0]["source_row"],
                    field="release_time",
                    task_id=task_id,
                )
            )
        if mapping.structure == "ordered" and parsed_steps:
            indices = sorted(step.step_index for step in parsed_steps)
            if indices != list(range(1, len(parsed_steps) + 1)):
                task_issues.append(
                    issue(
                        "INVALID_STEP_SEQUENCE",
                        "step indices must be unique and contiguous from one",
                        dataset_id=source.registration.dataset_id,
                        source_row=records[0]["source_row"],
                        field="step_index",
                        task_id=task_id,
                    )
                )
            if mapping.quantity_mode == "signed_delta":
                cumulative = 0.0
                minimum = 0.0
                for step in sorted(parsed_steps, key=lambda item: item.step_index):
                    cumulative += step.quantity_delta or 0.0
                    minimum = min(minimum, cumulative)
                required_capacity = -minimum
        if task_issues:
            issues.extend(task_issues)
            continue
        for left, right in zip(
            sorted(parsed_steps, key=lambda item: item.step_index),
            sorted(parsed_steps, key=lambda item: item.step_index)[1:],
        ):
            try:
                resolver.require_route(
                    task_locations[left.location_id], task_locations[right.location_id]
                )
            except ValueError as exc:
                task_issues.append(
                    issue(
                        "UNREACHABLE_TASK_LEG",
                        str(exc),
                        dataset_id=source.registration.dataset_id,
                        source_row=records[0]["source_row"],
                        field="steps",
                        task_id=task_id,
                    )
                )
        if task_issues:
            issues.extend(task_issues)
            continue
        try:
            steps = tuple(sorted(parsed_steps, key=lambda item: item.step_index))
            task = Task(
                task_id=task_id,
                fleet_id=mapping.fleet_id,
                release_s=releases[0],
                steps=steps,
                required_capacity=required_capacity,
                source_record_refs=tuple(
                    f"{source.registration.dataset_id}:row:{item['source_row']}" for item in records
                ),
                source_policy=mapping.adapter,
            )
        except (ValidationError, IndexError) as exc:
            issues.append(
                issue(
                    "INVALID_TASK",
                    str(exc),
                    dataset_id=source.registration.dataset_id,
                    source_row=records[0]["source_row"],
                    field="task",
                    task_id=task_id,
                )
            )
            continue
        tasks.append(task)
        locations.update(task_locations)
    if issues and mapping.error_policy == "strict":
        raise DemandImportError(tuple(issues))
    return (
        tuple(sorted(tasks, key=lambda item: (item.release_s, item.task_id))),
        tuple(locations[key] for key in sorted(locations)),
        tuple(sorted(issues, key=lambda item: (item.source_row or 0, item.code))),
    )


def _normalize_rates(
    source: TabularSource,
    mapping: RateImportMapping,
    resolver: LocationResolver,
    *,
    chunk_size: int,
    max_rows: int,
) -> tuple[tuple[dict[str, object], ...], tuple[LocationRef, ...]]:
    raw_rows = _read_rows(source, mapping, chunk_size=chunk_size, max_rows=max_rows)
    rates: list[dict[str, object]] = []
    locations: dict[str, LocationRef] = {}
    errors: list[ValidationIssue] = []
    divisor = {"tasks_per_second": 1.0, "tasks_per_minute": 60.0, "tasks_per_hour": 3600.0}[
        mapping.rate_unit
    ]
    for source_row, row in raw_rows:
        try:
            origin_id = parse_identifier(_mapped(row, mapping, "origin_location_id"))
            destination_id = parse_identifier(_mapped(row, mapping, "destination_location_id"))
            origin = resolver.by_id(origin_id)
            destination = resolver.by_id(destination_id)
            resolver.require_route(origin, destination)
            start = parse_time(_mapped(row, mapping, "interval_start"), mapping.time)
            end = parse_time(_mapped(row, mapping, "interval_end"), mapping.time)
            rate = parse_float(_mapped(row, mapping, "rate"), nonnegative=True) / divisor
            if end <= start:
                raise ValueError("rate interval must have positive duration")
            rates.append(
                {
                    "fleet_id": mapping.fleet_id,
                    "interval_start_s": start,
                    "interval_end_s": end,
                    "origin_location_id": origin_id,
                    "destination_location_id": destination_id,
                    "rate_tasks_per_s": rate,
                    "source_record_ref": f"{source.registration.dataset_id}:row:{source_row}",
                }
            )
            locations[origin_id] = origin
            locations[destination_id] = destination
        except (ValueError, TypeError, KeyError) as exc:
            errors.append(
                issue(
                    "INVALID_RATE_ROW",
                    str(exc),
                    dataset_id=source.registration.dataset_id,
                    source_row=source_row,
                    field="row",
                )
            )
    grouped: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for rate in rates:
        grouped[(str(rate["origin_location_id"]), str(rate["destination_location_id"]))].append(
            rate
        )
    for key, group in grouped.items():
        ordered = sorted(
            group, key=lambda item: (float(item["interval_start_s"]), float(item["interval_end_s"]))
        )
        for left, right in zip(ordered, ordered[1:]):
            if float(right["interval_start_s"]) < float(left["interval_end_s"]):
                errors.append(
                    issue(
                        "OVERLAPPING_RATE_INTERVALS",
                        f"OD key {key!r} has overlapping rate intervals",
                        dataset_id=source.registration.dataset_id,
                        source_row=int(str(right["source_record_ref"]).rsplit(":", 1)[1]),
                        field="interval",
                    )
                )
    if errors:
        raise DemandImportError(tuple(errors))
    return (
        tuple(
            sorted(
                rates,
                key=lambda item: (
                    float(item["interval_start_s"]),
                    str(item["origin_location_id"]),
                    str(item["destination_location_id"]),
                ),
            )
        ),
        tuple(locations[key] for key in sorted(locations)),
    )


def _frames(
    *,
    mapping_id: str,
    mapping,
    tasks: tuple[Task, ...],
    locations: tuple[LocationRef, ...],
    rates: tuple[dict[str, object], ...],
    issues: tuple[ValidationIssue, ...],
) -> dict[str, pd.DataFrame]:
    location_refs: dict[str, set[str]] = defaultdict(set)
    for task in tasks:
        for step in task.steps:
            location_refs[step.location_id].update(step.source_record_refs)
    for rate in rates:
        source_ref = str(rate["source_record_ref"])
        location_refs[str(rate["origin_location_id"])].add(source_ref)
        location_refs[str(rate["destination_location_id"])].add(source_ref)
    task_rows = [
        {
            "task_id": task.task_id,
            "fleet_id": task.fleet_id,
            "release_s": task.release_s,
            "kind": task.kind,
            "required_capacity": task.required_capacity,
            "source_record_refs_json": canonical_json_text(task.source_record_refs),
            "source_policy": task.source_policy,
        }
        for task in tasks
    ]
    step_rows = [
        {
            "task_id": task.task_id,
            **step.model_dump(mode="json"),
            "source_record_refs": canonical_json_text(step.source_record_refs),
        }
        for task in tasks
        for step in task.steps
    ]
    location_rows = []
    for item in locations:
        row = item.model_dump(mode="json")
        row["source_record_refs_json"] = canonical_json_text(
            tuple(sorted(location_refs[item.location_id]))
        )
        location_rows.append(row)
    issue_rows = [item.model_dump(mode="json") for item in issues]
    return {
        "import_mapping": pd.DataFrame(
            [{"mapping_id": mapping_id, "mapping_json": canonical_json_text(mapping)}],
            columns=("mapping_id", "mapping_json"),
        ),
        "locations": pd.DataFrame(
            location_rows,
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
                "source_record_refs_json",
            ),
        ),
        "od_rates": pd.DataFrame(
            list(rates),
            columns=(
                "fleet_id",
                "interval_start_s",
                "interval_end_s",
                "origin_location_id",
                "destination_location_id",
                "rate_tasks_per_s",
                "source_record_ref",
            ),
        ),
        "task_steps": pd.DataFrame(
            step_rows,
            columns=(
                "task_id",
                "step_index",
                "location_id",
                "scheduled_time_s",
                "service_duration_s",
                "quantity_delta",
                "source_record_refs",
            ),
        ),
        "tasks": pd.DataFrame(
            task_rows,
            columns=(
                "task_id",
                "fleet_id",
                "release_s",
                "kind",
                "required_capacity",
                "source_record_refs_json",
                "source_policy",
            ),
        ),
        "validation_issues": pd.DataFrame(
            issue_rows,
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
            ),
        ),
    }


def normalize_demand(
    source: TabularSource,
    mapping: DemandImportMapping | RateImportMapping,
    *,
    artifact_root,
    resolver: LocationResolver,
    chunk_size: int = 10_000,
    max_rows: int = 1_000_000,
) -> NormalizedDemand:
    """Normalize one registered source and atomically publish only a valid result."""

    if isinstance(mapping, RateImportMapping):
        rates, locations = _normalize_rates(
            source, mapping, resolver, chunk_size=chunk_size, max_rows=max_rows
        )
        tasks: tuple[Task, ...] = ()
        issues: tuple[ValidationIssue, ...] = ()
        semantics = "rate_model"
    else:
        tasks, locations, issues = _normalize_observed(
            source, mapping, resolver, chunk_size=chunk_size, max_rows=max_rows
        )
        rates = ()
        semantics = "observed_tasks"
    mapping_id = stable_id("mapping", mapping)
    frames = _frames(
        mapping_id=mapping_id,
        mapping=mapping,
        tasks=tasks,
        locations=locations,
        rates=rates,
        issues=issues,
    )
    resolved_config = {
        "mapping": scientific_projection(mapping),
        "mapping_id": mapping_id,
        "data_semantics": semantics,
        "routing_profile_id": resolver.routing_profile_id,
        "normalized_output_hash": scientific_hash(
            {"tasks": tasks, "locations": locations, "rates": rates, "issues": issues}
        ),
    }
    reference, directory = publish_dataset(
        artifact_root=artifact_root,
        resolved_config=resolved_config,
        dependencies=(
            ArtifactDependency(
                role="raw_demand",
                artifact_id=source.registration.dataset_id,
                content_hash=source.registration.content_hash,
            ),
            ArtifactDependency(
                role="environment",
                artifact_id=resolver.environment.artifact_id,
                content_hash=resolver.environment.content_hash,
            ),
        ),
        algorithm_versions={"demand_normalization": NORMALIZATION_VERSION},
        frames=frames,
    )
    return NormalizedDemand(
        reference=reference,
        directory=directory,
        mapping_id=mapping_id,
        data_semantics=semantics,
        locations=locations,
        tasks=tasks,
        rates=rates,
        issues=issues,
    )


def read_saved_demand_mapping(
    reference, *, artifact_root
) -> DemandImportMapping | RateImportMapping:
    """Replay the exact immutable mapping stored with a normalized demand dataset."""

    directory, _ = verified_dataset_directory(reference, artifact_root=artifact_root)
    table = pd.read_parquet(directory / "import_mapping.parquet")
    if len(table) != 1:
        raise ValueError("normalized demand artifact must contain exactly one saved mapping")
    payload = json.loads(str(table.iloc[0]["mapping_json"]))
    if payload.get("adapter") == "demand.upload_sparse_od_rate@1":
        return RateImportMapping.model_validate(payload)
    return DemandImportMapping.model_validate(payload)
