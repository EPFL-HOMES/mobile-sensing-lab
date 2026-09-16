"""Verified reconstruction of runtime records from immutable dataset artifacts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
import numpy as np

from mobile_sensing.contracts import (
    ArtifactManifest,
    ArtifactRef,
    AssignmentPlan,
    AssignmentRow,
    LocationRef,
    Task,
    TaskStep,
    VehicleKey,
    VehicleSpec,
    canonical_json_text,
    stable_id,
)
from mobile_sensing.datasets.storage import verified_dataset_directory


DATASET_RUNTIME_LOADER_VERSION = "dataset-runtime-loader@1"


@dataclass(frozen=True, slots=True)
class DatasetRuntimeContent:
    """Canonical runtime records reconstructed from verified Parquet tables."""

    reference: ArtifactRef
    manifest: ArtifactManifest
    locations: tuple[LocationRef, ...]
    tasks: tuple[Task, ...]
    vehicle_specs: tuple[VehicleSpec, ...]
    assignment_plan: AssignmentPlan | None


def _optional(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list, tuple, np.ndarray)):
        return value
    try:
        if bool(pd.isna(value)):
            return None
    except (TypeError, ValueError):
        pass
    return value


def _sequence(value: Any) -> tuple[str, ...]:
    value = _optional(value)
    if value is None:
        return ()
    if isinstance(value, str):
        decoded = json.loads(value)
        return tuple(str(item) for item in decoded)
    return tuple(str(item) for item in value)


def _model(model, payload: dict[str, Any]):
    """Use the JSON boundary so strict contracts decode enums identically to CLI input."""

    return model.model_validate_json(canonical_json_text(payload))


def _locations(directory: Path) -> tuple[LocationRef, ...]:
    path = directory / "locations.parquet"
    if not path.is_file():
        return ()
    fields = tuple(LocationRef.model_fields)
    records = []
    for row in pd.read_parquet(path).to_dict("records"):
        payload = {name: _optional(row.get(name)) for name in fields}
        records.append(_model(LocationRef, payload))
    return tuple(sorted(records, key=lambda item: item.location_id))


def _tasks(directory: Path) -> tuple[Task, ...]:
    task_path = directory / "tasks.parquet"
    step_path = directory / "task_steps.parquet"
    if not task_path.is_file() or not step_path.is_file():
        return ()
    steps_by_task: dict[str, list[TaskStep]] = {}
    for row in pd.read_parquet(step_path).to_dict("records"):
        task_id = str(row["task_id"])
        steps_by_task.setdefault(task_id, []).append(
            _model(
                TaskStep,
                {
                    "step_index": int(row["step_index"]),
                    "location_id": str(row["location_id"]),
                    "scheduled_time_s": _optional(row.get("scheduled_time_s")),
                    "service_duration_s": float(row["service_duration_s"]),
                    "quantity_delta": _optional(row.get("quantity_delta")),
                    "source_record_refs": _sequence(row.get("source_record_refs")),
                },
            )
        )
    records = []
    for row in pd.read_parquet(task_path).to_dict("records"):
        task_id = str(row["task_id"])
        records.append(
            _model(
                Task,
                {
                    "task_id": task_id,
                    "fleet_id": str(row["fleet_id"]),
                    "release_s": float(row["release_s"]),
                    "steps": [
                        item.model_dump(mode="json")
                        for item in sorted(
                            steps_by_task.get(task_id, ()), key=lambda item: item.step_index
                        )
                    ],
                    "kind": str(row["kind"]),
                    "required_capacity": _optional(row.get("required_capacity")),
                    "source_record_refs": _sequence(row.get("source_record_refs_json")),
                    "source_policy": _optional(row.get("source_policy")),
                },
            )
        )
    return tuple(sorted(records, key=lambda item: (item.fleet_id, item.task_id)))


def _vehicles(directory: Path) -> tuple[VehicleSpec, ...]:
    path = directory / "vehicle_catalog.parquet"
    if not path.is_file():
        return ()
    records = []
    for row in pd.read_parquet(path).to_dict("records"):
        key = row.get("key")
        if key is None:
            key = {"fleet_id": str(row["fleet_id"]), "vehicle_id": str(row["vehicle_id"])}
        payload = {
            "key": key,
            "availability_start_s": float(row["availability_start_s"]),
            "availability_end_s": float(row["availability_end_s"]),
            "initial_location_id": str(row["initial_location_id"]),
            "depot_location_id": _optional(row.get("depot_location_id")),
            "capacity_mode": str(row["capacity_mode"]),
            "capacity": _optional(row.get("capacity")),
            "quantity_unit": _optional(row.get("quantity_unit")),
            "assigned_area_ids": _sequence(row.get("assigned_area_ids")),
            "identity_provenance": str(row["identity_provenance"]),
        }
        records.append(_model(VehicleSpec, payload))
    return tuple(sorted(records, key=lambda item: (item.key.fleet_id, item.key.vehicle_id)))


def _assignments(directory: Path) -> AssignmentPlan | None:
    path = directory / "assignments.parquet"
    if not path.is_file():
        return None
    rows = tuple(
        AssignmentRow(
            vehicle=VehicleKey(fleet_id=str(row["fleet_id"]), vehicle_id=str(row["vehicle_id"])),
            order_index=int(row["order_index"]),
            task_id=str(row["task_id"]),
        )
        for row in sorted(
            pd.read_parquet(path).to_dict("records"),
            key=lambda item: (
                str(item["fleet_id"]),
                str(item["vehicle_id"]),
                int(item["order_index"]),
            ),
        )
    )
    return AssignmentPlan(
        assignment_plan_id=stable_id(
            "assignment_plan", [row.model_dump(mode="json") for row in rows]
        ),
        rows=rows,
    )


def load_dataset_runtime_content(
    reference: ArtifactRef, *, artifact_root: str | Path
) -> DatasetRuntimeContent:
    """Verify one dataset and reconstruct the scientific runtime records it owns."""

    directory, manifest = verified_dataset_directory(reference, artifact_root=Path(artifact_root))
    return DatasetRuntimeContent(
        reference=reference,
        manifest=manifest,
        locations=_locations(directory),
        tasks=_tasks(directory),
        vehicle_specs=_vehicles(directory),
        assignment_plan=_assignments(directory),
    )
