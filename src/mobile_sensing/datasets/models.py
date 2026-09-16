"""Strict M03 import mappings and normalized handoff records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal, Self, TypeAlias
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator
from pyproj import CRS

from mobile_sensing.contracts import (
    ArtifactRef,
    CatalogIdentity,
    ContractModel,
    LocationRef,
    NonNegativeFloat,
    OpaqueId,
    Task,
    UtcDateTime,
    ValidationIssue,
    VehicleSpec,
)


class RegisteredTabularSource(ContractModel):
    dataset_id: OpaqueId
    content_hash: str
    original_filename: str
    size_bytes: int = Field(ge=0)
    format: Literal["csv", "parquet"]
    provenance: str

    @field_validator("content_hash")
    @classmethod
    def validate_hash(cls, value: str) -> str:
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("content_hash must be a lowercase SHA-256 digest")
        return value

    @field_validator("original_filename", "provenance")
    @classmethod
    def validate_text(cls, value: str) -> str:
        if value == "":
            raise ValueError("source metadata strings must be nonempty")
        return value


@dataclass(frozen=True, slots=True)
class TabularSource:
    registration: RegisteredTabularSource
    path: Path


class ElapsedTimeMapping(ContractModel):
    kind: Literal["elapsed"]
    unit: Literal["seconds", "minutes", "hours"]


class TimestampTimeMapping(ContractModel):
    kind: Literal["timestamp"]
    origin_utc: UtcDateTime


class ServiceClockTimeMapping(ContractModel):
    kind: Literal["service_clock"]
    service_date: str
    timezone: str
    origin_utc: UtcDateTime

    @field_validator("service_date")
    @classmethod
    def validate_date(cls, value: str) -> str:
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("service_date must use YYYY-MM-DD") from exc
        return value

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be an IANA timezone") from exc
        return value


TimeMapping: TypeAlias = Annotated[
    ElapsedTimeMapping | TimestampTimeMapping | ServiceClockTimeMapping,
    Field(discriminator="kind"),
]


_DEMAND_FIELDS = {
    "task_id",
    "release_time",
    "step_index",
    "scheduled_time",
    "location_id",
    "x",
    "y",
    "origin_location_id",
    "origin_x",
    "origin_y",
    "destination_location_id",
    "destination_x",
    "destination_y",
    "service_duration",
    "pickup_service_duration",
    "dropoff_service_duration",
    "quantity",
    "quantity_delta",
}


class DemandImportMapping(ContractModel):
    schema_version: Literal["2.0"]
    adapter: Literal["demand.upload_location@1", "demand.upload_od@1", "demand.upload_ordered@1"]
    structure: Literal["location", "od", "ordered"]
    data_semantics: Literal["observed_tasks"]
    fleet_id: OpaqueId
    columns: dict[str, str]
    location_representation: Literal["coordinates", "ids"]
    source_crs: str | None = None
    time: TimeMapping
    duration_unit: Literal["seconds", "minutes", "hours"]
    service_duration_default_s: NonNegativeFloat | None = None
    quantity_mode: Literal["none", "consumable", "occupancy", "signed_delta"]
    quantity_unit: str | None = None
    allow_generated_task_ids: bool = False
    error_policy: Literal["strict", "quarantine_invalid_tasks"] = "strict"

    @model_validator(mode="after")
    def validate_layout(self) -> Self:
        expected_adapter = f"demand.upload_{self.structure}@1"
        if self.adapter != expected_adapter:
            raise ValueError("adapter must match the declared task structure")
        if any(key not in _DEMAND_FIELDS for key in self.columns):
            raise ValueError("columns contains an unsupported logical field")
        if any(value == "" for value in self.columns.values()):
            raise ValueError("source column names must be nonempty")
        if len(set(self.columns.values())) != len(self.columns):
            raise ValueError("one source column cannot represent multiple logical fields")
        required = {"release_time"}
        if not self.allow_generated_task_ids:
            required.add("task_id")
        if self.structure == "ordered":
            required.add("step_index")
        if self.location_representation == "coordinates":
            if self.source_crs in (None, ""):
                raise ValueError("coordinate mappings require an explicit source_crs")
            try:
                CRS.from_user_input(self.source_crs)
            except Exception as exc:
                raise ValueError("source_crs is not recognized") from exc
            required |= (
                {"origin_x", "origin_y", "destination_x", "destination_y"}
                if self.structure == "od"
                else {"x", "y"}
            )
        else:
            if self.source_crs is not None:
                raise ValueError("ID-based locations must not declare source_crs")
            required |= (
                {"origin_location_id", "destination_location_id"}
                if self.structure == "od"
                else {"location_id"}
            )
        missing = sorted(required - self.columns.keys())
        if missing:
            raise ValueError(f"mapping is missing required logical fields: {missing}")
        duration_fields = (
            {"pickup_service_duration", "dropoff_service_duration"}
            if self.structure == "od"
            else {"service_duration"}
        )
        if not duration_fields.issubset(self.columns) and self.service_duration_default_s is None:
            raise ValueError("missing service-duration columns require an explicit default")
        quantity_field = "quantity_delta" if self.quantity_mode == "signed_delta" else "quantity"
        if self.quantity_mode == "none":
            if "quantity" in self.columns or "quantity_delta" in self.columns:
                raise ValueError("none quantity mode cannot map quantity fields")
            if self.quantity_unit is not None:
                raise ValueError("none quantity mode cannot declare a quantity unit")
        elif quantity_field not in self.columns or self.quantity_unit in (None, ""):
            raise ValueError("quantity modes require their value column and explicit unit")
        if self.quantity_mode == "signed_delta" and self.structure != "ordered":
            raise ValueError("signed_delta is supported only for ordered tasks")
        if self.quantity_mode == "occupancy" and self.structure != "od":
            raise ValueError("occupancy quantities require OD tasks")
        if self.structure == "ordered" and self.quantity_mode not in {"none", "signed_delta"}:
            raise ValueError("ordered tasks require explicit signed deltas or no quantity")
        return self


class RateImportMapping(ContractModel):
    schema_version: Literal["2.0"]
    adapter: Literal["demand.upload_sparse_od_rate@1"]
    data_semantics: Literal["rate_model"]
    fleet_id: OpaqueId
    columns: dict[str, str]
    time: TimeMapping
    rate_unit: Literal["tasks_per_second", "tasks_per_minute", "tasks_per_hour"]
    error_policy: Literal["strict"] = "strict"

    @model_validator(mode="after")
    def validate_columns(self) -> Self:
        required = {
            "interval_start",
            "interval_end",
            "origin_location_id",
            "destination_location_id",
            "rate",
        }
        if set(self.columns) != required or any(value == "" for value in self.columns.values()):
            raise ValueError(
                f"rate mapping requires exactly these logical fields: {sorted(required)}"
            )
        if len(set(self.columns.values())) != len(self.columns):
            raise ValueError("one source column cannot represent multiple logical fields")
        return self


class VehicleImportMapping(ContractModel):
    schema_version: Literal["2.0"]
    adapter: Literal["supply.upload_vehicle_catalog@1"]
    fleet_id: OpaqueId
    columns: dict[str, str]
    location_representation: Literal["coordinates", "ids"]
    source_crs: str | None = None
    time: TimeMapping
    capacity_mode: Literal["none", "consumable", "occupancy"]
    quantity_unit: str | None = None

    @model_validator(mode="after")
    def validate_columns(self) -> Self:
        allowed = {
            "vehicle_id",
            "availability_start",
            "availability_end",
            "initial_location_id",
            "initial_x",
            "initial_y",
            "depot_location_id",
            "depot_x",
            "depot_y",
            "capacity",
        }
        if any(key not in allowed for key in self.columns):
            raise ValueError("columns contains an unsupported vehicle logical field")
        required = {"vehicle_id", "availability_start", "availability_end"}
        if self.location_representation == "ids":
            required.add("initial_location_id")
            if self.source_crs is not None:
                raise ValueError("ID-based locations must not declare source_crs")
        else:
            required |= {"initial_x", "initial_y"}
            if self.source_crs in (None, ""):
                raise ValueError("coordinate mappings require an explicit source_crs")
            try:
                CRS.from_user_input(self.source_crs)
            except Exception as exc:
                raise ValueError("source_crs is not recognized") from exc
        if self.capacity_mode == "none":
            if "capacity" in self.columns or self.quantity_unit is not None:
                raise ValueError("none capacity mode cannot map capacity or declare a unit")
        elif "capacity" not in self.columns or self.quantity_unit in (None, ""):
            raise ValueError("finite capacity modes require a capacity column and unit")
        missing = sorted(required - self.columns.keys())
        if missing:
            raise ValueError(f"vehicle mapping is missing logical fields: {missing}")
        if len(set(self.columns.values())) != len(self.columns):
            raise ValueError("one source column cannot represent multiple logical fields")
        return self


class AreaAssignmentMapping(ContractModel):
    schema_version: Literal["2.0"]
    adapter: Literal["supply.upload_area_assignments@1"]
    columns: dict[str, str]
    fixed_fleet_id: OpaqueId | None = None

    @model_validator(mode="after")
    def validate_columns(self) -> Self:
        required = {"vehicle_id", "area_id"}
        optional = {"fleet_id"}
        if not required.issubset(self.columns) or set(self.columns) - required - optional:
            raise ValueError("area assignment mapping requires vehicle_id and area_id")
        if ("fleet_id" in self.columns) == (self.fixed_fleet_id is not None):
            raise ValueError("provide exactly one fleet-ID column or fixed_fleet_id")
        return self


@dataclass(frozen=True, slots=True)
class NormalizedDemand:
    reference: ArtifactRef
    directory: Path
    mapping_id: str
    data_semantics: Literal["observed_tasks", "rate_model"]
    locations: tuple[LocationRef, ...]
    tasks: tuple[Task, ...]
    rates: tuple[dict[str, object], ...]
    issues: tuple[ValidationIssue, ...]


@dataclass(frozen=True, slots=True)
class NormalizedSupply:
    reference: ArtifactRef
    directory: Path
    mapping_id: str
    catalog: CatalogIdentity
    locations: tuple[LocationRef, ...]
    vehicles: tuple[VehicleSpec, ...]
    issues: tuple[ValidationIssue, ...]
