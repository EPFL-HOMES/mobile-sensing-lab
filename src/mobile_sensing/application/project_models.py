"""Public version-three authoring contracts; runtime units are resolved in Python."""

from datetime import date
import json
from typing import Literal
from pydantic import Field, model_validator, field_validator
from mobile_sensing.contracts import ArtifactRef, ContractModel
from mobile_sensing.application.studio_models import EnvironmentEditor, EnvironmentResult
from mobile_sensing.jobs.models import ApiModel


class TemporalInterval(ContractModel):
    start_time: str
    end_time: str
    value: float = Field(ge=0, allow_inf_nan=False)


class ShiftGroup(ContractModel):
    name: str = Field(default="Shift", min_length=1)
    count: int = Field(ge=1, le=100000)
    start_time: str = "08:00"
    latest_start: str = "08:00"
    day_offset: int = Field(default=0, ge=-2, le=0)
    work_hours: float = Field(default=8, gt=0, le=48)


class NumericRange(ContractModel):
    minimum: float = Field(ge=0, allow_inf_nan=False)
    maximum: float = Field(ge=0, allow_inf_nan=False)
    step: float = Field(gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def ordered(self):
        if self.maximum < self.minimum:
            raise ValueError("Range maximum must not be below minimum")
        return self


class SpatialFeatureWeight(ContractModel):
    feature: str = Field(min_length=1)
    weight: float = Field(default=1, ge=0, allow_inf_nan=False)


class DemandEditor(ContractModel):
    source: Literal["import", "generator"] = "generator"
    task_type: Literal["location", "od", "ordered"] = "location"
    input_id: str | None = None
    template: Literal["table", "gtfs"] = "table"
    content: Literal["instances", "counts", "rates"] = "instances"
    count_semantics: Literal["task_count", "service_quantity"] | None = None
    columns: dict[str, str] = Field(default_factory=dict)
    coordinate_kind: Literal["cell_id", "coordinates", "location_id"] = "cell_id"
    time_unit: Literal["clock", "seconds", "minutes", "hours", "datetime"] = "clock"
    rate_unit: Literal["per_hour", "per_day"] = "per_hour"
    generation_timing: Literal["offline", "online"] = "offline"
    volume_mode: Literal["fixed", "expected"] = "fixed"
    task_volume: float = Field(default=100, ge=0)
    start_time: str = "08:00"
    end_time: str = "18:00"
    release_mode: Literal["uniform", "at_start"] = "uniform"
    temporal_mode: Literal["window", "shares", "rates"] = "window"
    time_profile: tuple[TemporalInterval, ...] = Field(default=(), max_length=1000)
    time_profile_input: str | None = None
    spatial_feature: str = "uniform"
    spatial_weights: tuple[SpatialFeatureWeight, ...] = ()
    destination_feature: str = "uniform"
    destination_spatial_weights: tuple[SpatialFeatureWeight, ...] = ()
    location_condition: Literal["all_resolved", "depot_roundtrip"] = "all_resolved"
    od_distribution_input: str | None = None
    service_seconds: float = Field(default=60, ge=0)
    pickup_seconds: float = Field(default=0, ge=0)
    quantity: float = Field(default=1, gt=0)
    route_ids: tuple[str, ...] = ()
    imported_assignment: Literal["execute", "history_only"] = "execute"

    @model_validator(mode="after")
    def semantics(self):
        for label, values in (
            ("origin", self.spatial_weights),
            ("destination", self.destination_spatial_weights),
        ):
            if values and sum(value.weight for value in values) <= 0:
                raise ValueError(
                    f"{label.title()} spatial-feature weights require positive total mass"
                )
        if self.temporal_mode != "window":
            if self.source != "generator" or self.release_mode != "uniform":
                raise ValueError(
                    "A temporal profile requires generated tasks with interval releases"
                )
            if bool(self.time_profile) == bool(self.time_profile_input):
                raise ValueError("Supply exactly one temporal profile table or inline profile")
            if self.temporal_mode == "rates" and self.volume_mode != "expected":
                raise ValueError("Direct temporal rates require stochastic expected task volume")
        if self.location_condition == "depot_roundtrip" and (
            self.source != "generator" or self.task_type != "location"
        ):
            raise ValueError("Depot round-trip conditioning applies to generated location tasks")
        if self.source == "import" and self.generation_timing != "offline":
            raise ValueError("Offline/online selection applies only to generators")
        if self.source == "generator" and self.task_type == "ordered":
            raise ValueError("Ordered-stop tasks require an imported timetable")
        if (
            self.source == "generator"
            and self.volume_mode == "fixed"
            and not self.task_volume.is_integer()
        ):
            raise ValueError("Fixed task total must be an integer")
        if self.content == "counts" and self.count_semantics is None:
            raise ValueError("Declare whether counts represent tasks or service quantity")
        if self.template == "gtfs" and (self.source != "import" or self.task_type != "ordered"):
            raise ValueError("GTFS imports contain ordered-stop tasks")
        return self


class SupplyEditor(ContractModel):
    source: Literal["generated", "import", "timetable"] = "generated"
    input_id: str | None = None
    columns: dict[str, str] = Field(default_factory=dict)
    fleet_size: int | None = Field(default=10, ge=1)
    timetable_catalog_complete: bool = False
    operating_start: str | None = "08:00"
    operating_end: str | None = "18:00"
    activation: Literal["simultaneous", "uniform_bounded"] = "simultaneous"
    latest_start: str = "16:00"
    work_hours: float = Field(default=8, gt=0, le=48)
    initial_location: Literal["depot", "spatial_feature", "input"] = "spatial_feature"
    spatial_feature: str = "uniform"
    spatial_weights: tuple[SpatialFeatureWeight, ...] = ()
    post_service: Literal["stationary", "random_cruise", "return_after_plan"] = "stationary"
    capacity_mode: Literal["none", "consumable", "occupancy"] = "none"
    capacity: float = Field(default=1, gt=0)
    capacity_unit: str = "units"
    depot_cell_id: str | None = None
    depot_longitude: float | None = Field(default=None, ge=-180, le=180)
    depot_latitude: float | None = Field(default=None, ge=-90, le=90)
    synthetic_depot: bool = False
    service_area_mode: Literal["none", "uploaded", "auto"] = "none"
    service_area_input: str | None = None
    area_assignment_input: str | None = None
    auto_service_area_count: int | None = Field(default=None, ge=1, le=1000)
    turnaround_seconds: float = Field(default=0, ge=0)
    depot_min_stay_minutes: float = Field(default=10, gt=0, le=1440)
    timetable_idle_break_minutes: float | None = Field(default=60, gt=0, le=1440)
    shift_groups: tuple[ShiftGroup, ...] = Field(default=(), max_length=100)

    @model_validator(mode="after")
    def consistency(self):
        if "service_area_mode" not in self.model_fields_set and (
            self.service_area_input is not None or self.area_assignment_input is not None
        ):
            object.__setattr__(self, "service_area_mode", "uploaded")
        if self.spatial_weights and sum(value.weight for value in self.spatial_weights) <= 0:
            raise ValueError("Supply spatial-feature weights require positive total mass")
        if self.shift_groups:
            if self.source != "generated":
                raise ValueError("Shift groups require a generated physical catalog")
            if sum(group.count for group in self.shift_groups) != self.fleet_size:
                raise ValueError("Shift-group counts must sum to the physical fleet size")
            if len({group.name for group in self.shift_groups}) != len(self.shift_groups):
                raise ValueError("Shift-group names must be unique")
        if self.source == "generated" and (
            self.fleet_size is None or self.operating_start is None or self.operating_end is None
        ):
            raise ValueError("Generated supply needs an explicit size and operating window")
        if self.service_area_mode == "none" and (
            self.service_area_input is not None
            or self.area_assignment_input is not None
            or self.auto_service_area_count is not None
        ):
            raise ValueError("Disabled service areas cannot declare area inputs or a count")
        if self.service_area_mode == "uploaded" and (
            self.service_area_input is None or self.area_assignment_input is None
        ):
            raise ValueError("Uploaded service areas require geometry and vehicle-area assignments")
        if self.service_area_mode == "uploaded" and self.auto_service_area_count is not None:
            raise ValueError("Uploaded service areas cannot declare an automatic area count")
        if self.service_area_mode == "auto" and (
            self.service_area_input is not None or self.area_assignment_input is not None
        ):
            raise ValueError("Auto service areas cannot use uploaded area inputs")
        if self.service_area_mode == "auto" and self.auto_service_area_count is None:
            raise ValueError("Auto service areas require an explicit area count")
        if (
            self.service_area_mode == "auto"
            and self.fleet_size is not None
            and self.auto_service_area_count > self.fleet_size
        ):
            raise ValueError("Auto service areas require at least one vehicle per area")
        if (self.depot_longitude is None) != (self.depot_latitude is None):
            raise ValueError("Depot longitude and latitude must be supplied together")
        return self


class DispatchEditor(ContractModel):
    mode: Literal["scheduled", "sequential", "batch", "one_shot"] = "sequential"
    assignment_input: str | None = None
    max_pickup_minutes: float = Field(default=15, gt=0)
    batch_minutes: float = Field(default=2, gt=0)
    solution_limit: int = Field(default=100, ge=1, le=100000)
    planning_timeout_seconds: float = Field(default=120, gt=0)
    max_cost_pairs: int = Field(default=250000, ge=1)
    allow_replenishment: bool = True
    max_replenishments_per_vehicle: int = Field(default=4, ge=1, le=100)


class FleetEditor(ContractModel):
    fleet_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    demand: DemandEditor = Field(default_factory=DemandEditor)
    supply: SupplyEditor = Field(default_factory=SupplyEditor)
    dispatch: DispatchEditor = Field(default_factory=DispatchEditor)
    routing_profile: str = "default"
    sensing_mode: Literal["movement_duration", "operating_duration"] = "operating_duration"
    sensing_movements: tuple[
        Literal["service_pickup", "service_inter_step", "cruise", "depot_return", "reposition"], ...
    ] = ("service_pickup", "service_inter_step", "cruise", "depot_return", "reposition")

    @model_validator(mode="after")
    def auto_area_demand(self):
        if self.supply.service_area_mode == "auto" and (
            self.demand.source != "generator" or self.demand.od_distribution_input is not None
        ):
            raise ValueError(
                "Auto service areas require generated demand with an explicit origin spatial-feature distribution"
            )
        return self


class SimulationEditor(ContractModel):
    time_mode: Literal["relative", "weekday", "calendar"] = "calendar"
    timezone: str | None = None
    service_date: date = date(2026, 1, 14)
    weekday: int = Field(default=2, ge=0, le=6)
    calendar_period_start: date | None = None
    calendar_period_end: date | None = None
    warmup_hours: float = Field(default=0, ge=0, le=48)
    start_time: str = "00:00"
    end_time: str = "24:00"
    replications: int = Field(default=2, ge=1, le=10000)
    temporal_resolution_minutes: float = Field(default=15, gt=0)
    seed: int = Field(default=20260114, ge=0, lt=2**64)


class PortfolioFleetEditor(ContractModel):
    fleet_id: str
    unit_cost: float = Field(default=1, ge=0)
    counts: tuple[int, ...] = (0,)
    count_range: NumericRange | None = None


class PortfolioEditor(ContractModel):
    source_run_id: str | None = None
    sampling_runs: int = Field(default=100, ge=1)
    seed: int = Field(default=20260115, ge=0, lt=2**64)
    cost_unit: str = "abstract cost units"
    budgets: tuple[float, ...] = (10, 20, 30, 40)
    budget_range: NumericRange | None = None
    fleets: tuple[PortfolioFleetEditor, ...] = ()
    utility: Literal["exponential", "linear_capped", "binary"] = "exponential"
    saturation_minutes: float = Field(default=5, gt=0)
    utility_temporal_resolution_minutes: float | None = Field(default=None, gt=0)
    spatial_weight: str = "uniform"
    # Preserve historical results; new workflows explicitly choose their metric.
    risk_metric: Literal["std", "p05"] = "std"


class ProjectConfig(ContractModel):
    schema_version: Literal["3.0", "3.1", "3.2", "3.3", "3.4"] = "3.4"
    environment: EnvironmentEditor = Field(default_factory=EnvironmentEditor)
    prepared_environment: EnvironmentResult | None = None
    fleets: tuple[FleetEditor, ...] = ()
    simulation: SimulationEditor = Field(default_factory=SimulationEditor)
    portfolio: PortfolioEditor = Field(
        default_factory=lambda: PortfolioEditor(
            risk_metric="p05", utility_temporal_resolution_minutes=1440
        )
    )
    source_revision: str | None = None
    linked_run_ids: tuple[str, ...] = ()
    linked_analysis_ids: tuple[str, ...] = ()
    example_bundle_id: str | None = None
    read_only: bool = False
    saved_views: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def historical_defaults(self):
        if self.schema_version in {"3.0", "3.1"}:
            fleets = []
            for fleet in self.fleets:
                updates = {}
                if "sensing_mode" not in fleet.model_fields_set:
                    updates["sensing_mode"] = "movement_duration"
                if "allow_replenishment" not in fleet.dispatch.model_fields_set:
                    updates["dispatch"] = fleet.dispatch.model_copy(
                        update={"allow_replenishment": False}
                    )
                if "timetable_idle_break_minutes" not in fleet.supply.model_fields_set:
                    updates["supply"] = fleet.supply.model_copy(
                        update={"timetable_idle_break_minutes": None}
                    )
                fleets.append(fleet.model_copy(update=updates))
            object.__setattr__(self, "fleets", tuple(fleets))
        if self.schema_version in {"3.0", "3.1", "3.2"} and (
            "utility_temporal_resolution_minutes" not in self.portfolio.model_fields_set
        ):
            object.__setattr__(
                self,
                "portfolio",
                self.portfolio.model_copy(
                    update={
                        "utility_temporal_resolution_minutes": self.simulation.temporal_resolution_minutes
                    }
                ),
            )
        return self

    @model_validator(mode="after")
    def unique_fleets(self):
        ids = [fleet.fleet_id for fleet in self.fleets]
        if len(ids) != len(set(ids)):
            raise ValueError("Fleet IDs must be unique")
        return self


class FleetResolution(ContractModel):
    artifact: ArtifactRef
    fleet_id: str
    vehicle_count: int
    task_counts: tuple[int, ...]
    assumptions: tuple[str, ...]
    diagnostics: dict = Field(default_factory=dict)


class ProjectConfigRequest(ApiModel):
    config: ProjectConfig

    @field_validator("config", mode="before")
    @classmethod
    def decode_config(cls, value):
        return (
            value
            if isinstance(value, ProjectConfig)
            else ProjectConfig.model_validate_json(json.dumps(value))
        )


class ProjectResolutionResult(ContractModel):
    artifact: ArtifactRef | None = None
    validation_level: Literal["configuration", "resolved"] = "resolved"
    vehicle_counts: dict[str, int]
    task_counts: dict[str, tuple[int, ...]]
    assumptions: tuple[str, ...]
    reports: dict
