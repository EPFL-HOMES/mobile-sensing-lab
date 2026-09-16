"""Version-2 scientific and execution configuration contracts."""

from __future__ import annotations

from mobile_sensing.contracts.artifacts import ArtifactRef

from datetime import datetime
from typing import Annotated, ClassVar, Literal, Self, TypeAlias
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator

from mobile_sensing.contracts.base import (
    CapabilityKey,
    ContractModel,
    FiniteFloat,
    NonNegativeFloat,
    NonNegativeInt,
    OpaqueId,
    PositiveFloat,
    PositiveInt,
    SchemaVersion,
    UInt64,
    UtcDateTime,
)


class ClockConfig(ContractModel):
    origin_utc: UtcDateTime
    display_timezone: str
    simulation_start_s: FiniteFloat
    observation_start_s: FiniteFloat
    end_s: FiniteFloat

    @field_validator("display_timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("display_timezone must be an IANA timezone") from exc
        return value

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        if not self.simulation_start_s <= self.observation_start_s < self.end_s:
            raise ValueError("clock requires simulation_start_s <= observation_start_s < end_s")
        return self


class UploadedBoundaryConfig(ContractModel):
    kind: Literal["uploaded"]
    dataset_id: OpaqueId


class LocalMunicipalitiesBoundaryConfig(ContractModel):
    kind: Literal["local_municipalities"]
    dataset_id: OpaqueId
    municipality_names: tuple[str, ...]

    @field_validator("municipality_names")
    @classmethod
    def validate_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or any(name == "" for name in value):
            raise ValueError("at least one nonempty municipality name is required")
        if len(set(value)) != len(value):
            raise ValueError("municipality_names must be unique")
        return tuple(sorted(value))


BoundaryConfig: TypeAlias = Annotated[
    UploadedBoundaryConfig | LocalMunicipalitiesBoundaryConfig,
    Field(discriminator="kind"),
]


class SuppliedNetworkConfig(ContractModel):
    kind: Literal["supplied"]
    dataset_id: OpaqueId
    layer: str | None = None
    topology_policy: Literal["strict@1", "conservative_repair@1", "quarantine_invalid@1"] = (
        "strict@1"
    )
    endpoint_tolerance_m: PositiveFloat = 0.05


NetworkSourceConfig: TypeAlias = SuppliedNetworkConfig


class EnvironmentProviderRequest(ContractModel):
    scientific_identity_excluded_fields: ClassVar[frozenset[str]] = frozenset({"cache_policy"})

    schema_version: SchemaVersion
    provider: CapabilityKey
    boundary: BoundaryConfig
    routing_extent_ref: OpaqueId | None = None
    target_crs: str
    network_mode: Literal["drive"] = "drive"
    requested_features: tuple[str, ...] = ()
    cache_policy: Literal["reuse", "refresh"] = "reuse"

    @field_validator("target_crs")
    @classmethod
    def validate_target_crs(cls, value: str) -> str:
        if value == "":
            raise ValueError("target_crs must be nonempty")
        return value

    @field_validator("requested_features")
    @classmethod
    def validate_features(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(item == "" for item in value) or len(set(value)) != len(value):
            raise ValueError("requested feature names must be nonempty and unique")
        return tuple(sorted(value))


class EdgeTravelTimeSourceConfig(ContractModel):
    kind: Literal["edge_travel_time"]
    field: str

    @field_validator("field")
    @classmethod
    def validate_field(cls, value: str) -> str:
        if value == "":
            raise ValueError("travel-time field must be nonempty")
        return value


class EdgeSpeedSourceConfig(ContractModel):
    kind: Literal["edge_speed"]
    field: str
    fallback_speed_mps: PositiveFloat | None = None

    @field_validator("field")
    @classmethod
    def validate_field(cls, value: str) -> str:
        if value == "":
            raise ValueError("speed field must be nonempty")
        return value


class ClassSpeedSourceConfig(ContractModel):
    kind: Literal["road_class_speed"]
    class_speed_mps: dict[str, PositiveFloat]
    fallback_speed_mps: PositiveFloat

    @field_validator("class_speed_mps")
    @classmethod
    def validate_class_speeds(cls, value: dict[str, float]) -> dict[str, float]:
        if not value or any(key == "" for key in value):
            raise ValueError("road-class speed source requires nonempty class speeds")
        return value


class ConstantSpeedSourceConfig(ContractModel):
    kind: Literal["constant_speed"]
    speed_mps: PositiveFloat


TravelTimeSourceConfig: TypeAlias = Annotated[
    EdgeTravelTimeSourceConfig
    | EdgeSpeedSourceConfig
    | ClassSpeedSourceConfig
    | ConstantSpeedSourceConfig,
    Field(discriminator="kind"),
]


class TravelTimeProfileConfig(ContractModel):
    profile_id: OpaqueId
    algorithm: Literal["static_shortest_travel_time"] = "static_shortest_travel_time"
    source: TravelTimeSourceConfig


class SnappingConfig(ContractModel):
    max_distance_m: PositiveFloat
    tie_break: Literal["canonical_node_id"] = "canonical_node_id"


class UploadedGridConfig(ContractModel):
    kind: Literal["uploaded"]
    dataset_id: OpaqueId


class RegularGridConfig(ContractModel):
    kind: Literal["regular"]
    cell_size_m: PositiveFloat
    origin_easting_m: FiniteFloat
    origin_northing_m: FiniteFloat


GridConfig: TypeAlias = Annotated[
    UploadedGridConfig | RegularGridConfig,
    Field(discriminator="kind"),
]


class PopulationFeaturesConfig(ContractModel):
    dataset_id: OpaqueId
    year: int
    missing_policy: Literal["error", "zero", "uniform_proxy"] = "error"


class EnvironmentBuildConfig(ContractModel):
    schema_version: SchemaVersion
    provider: CapabilityKey
    boundary: BoundaryConfig
    routing_extent_ref: OpaqueId | None = None
    network_source: NetworkSourceConfig
    network_mode: Literal["drive"] = "drive"
    working_crs: str
    travel_time_profiles: tuple[TravelTimeProfileConfig, ...]
    snapping: SnappingConfig
    grid: GridConfig
    population_features: PopulationFeaturesConfig | None = None

    @field_validator("working_crs")
    @classmethod
    def validate_crs(cls, value: str) -> str:
        if value == "":
            raise ValueError("working_crs must be nonempty")
        return value

    @field_validator("travel_time_profiles")
    @classmethod
    def validate_profiles(
        cls, value: tuple[TravelTimeProfileConfig, ...]
    ) -> tuple[TravelTimeProfileConfig, ...]:
        if not value:
            raise ValueError("at least one travel-time profile is required")
        ids = [profile.profile_id for profile in value]
        if len(set(ids)) != len(ids):
            raise ValueError("travel-time profile IDs must be unique")
        return tuple(sorted(value, key=lambda profile: profile.profile_id))


class UploadDemandParameters(ContractModel):
    dataset_id: OpaqueId
    mapping_id: OpaqueId
    error_policy: Literal["strict", "quarantine_invalid_tasks"] = "strict"


class IntensityIntervalConfig(ContractModel):
    start_s: FiniteFloat
    end_s: FiniteFloat
    rate_tasks_per_s: NonNegativeFloat

    @model_validator(mode="after")
    def validate_interval(self) -> Self:
        if self.end_s <= self.start_s:
            raise ValueError("intensity interval must have positive duration")
        return self


class PoissonGeneratorParameters(ContractModel):
    intervals: tuple[IntensityIntervalConfig, ...]
    location_weights_ref: OpaqueId | None = None
    od_weights_ref: OpaqueId | None = None
    service_duration_s: NonNegativeFloat = 0.0
    quantity: NonNegativeFloat | None = None

    @field_validator("intervals")
    @classmethod
    def validate_intervals(
        cls, value: tuple[IntensityIntervalConfig, ...]
    ) -> tuple[IntensityIntervalConfig, ...]:
        if not value:
            raise ValueError("at least one intensity interval is required")
        ordered = sorted(value, key=lambda item: item.start_s)
        if list(value) != ordered:
            raise ValueError("intensity intervals must be sorted")
        if any(right.start_s < left.end_s for left, right in zip(value, value[1:])):
            raise ValueError("intensity intervals must not overlap")
        return value

    @model_validator(mode="after")
    def validate_weight_source(self) -> Self:
        if (self.location_weights_ref is None) == (self.od_weights_ref is None):
            raise ValueError("exactly one location or OD weight reference is required")
        return self


class GTFSDemandParameters(ContractModel):
    reconstruction_id: OpaqueId


class UploadDemandConfig(ContractModel):
    source: Literal["upload"]
    structure: Literal["location", "od", "ordered"]
    adapter: CapabilityKey
    parameters: UploadDemandParameters


class GeneratorDemandConfig(ContractModel):
    source: Literal["generator"]
    structure: Literal["location", "od"]
    adapter: Literal["demand.poisson_piecewise_constant@1"]
    generation_timing: Literal["offline", "online"]
    parameters: PoissonGeneratorParameters

    @model_validator(mode="after")
    def validate_weights_for_structure(self) -> Self:
        if self.structure == "location" and self.parameters.location_weights_ref is None:
            raise ValueError("location demand requires location_weights_ref")
        if self.structure == "od" and self.parameters.od_weights_ref is None:
            raise ValueError("OD demand requires od_weights_ref")
        return self


class GTFSDemandConfig(ContractModel):
    source: Literal["gtfs"]
    structure: Literal["ordered"]
    adapter: Literal["demand.gtfs_reconstruction@1"]
    parameters: GTFSDemandParameters


DemandConfig: TypeAlias = Annotated[
    UploadDemandConfig | GeneratorDemandConfig | GTFSDemandConfig,
    Field(discriminator="source"),
]


class NoCapacityConfig(ContractModel):
    mode: Literal["none"]


class ConsumableCapacityConfig(ContractModel):
    mode: Literal["consumable"]
    unit: str
    replenishment_duration_s: PositiveFloat

    @field_validator("unit")
    @classmethod
    def validate_unit(cls, value: str) -> str:
        if value == "":
            raise ValueError("capacity unit must be nonempty")
        return value


class OccupancyCapacityConfig(ContractModel):
    mode: Literal["occupancy"]
    unit: str

    @field_validator("unit")
    @classmethod
    def validate_unit(cls, value: str) -> str:
        if value == "":
            raise ValueError("capacity unit must be nonempty")
        return value


CapacityConfig: TypeAlias = Annotated[
    NoCapacityConfig | ConsumableCapacityConfig | OccupancyCapacityConfig,
    Field(discriminator="mode"),
]


class StationaryIdleConfig(ContractModel):
    policy: Literal["stationary"]


class RandomCruiseIdleConfig(ContractModel):
    policy: Literal["random_cruise"]
    implementation: Literal["operational.random_cruise@1"] = "operational.random_cruise@1"


IdlePolicyConfig: TypeAlias = Annotated[
    StationaryIdleConfig | RandomCruiseIdleConfig,
    Field(discriminator="policy"),
]


class DepotReturnPolicyConfig(ContractModel):
    policy: Literal["return_when_blocked"]
    implementation: Literal["operational.depot_return@1"] = "operational.depot_return@1"


class SimultaneousAvailabilityConfig(ContractModel):
    kind: Literal["simultaneous"]
    start_s: FiniteFloat
    end_s: FiniteFloat

    @model_validator(mode="after")
    def validate_window(self) -> Self:
        if self.end_s <= self.start_s:
            raise ValueError("availability window must have positive duration")
        return self


class UniformBoundedAvailabilityConfig(ContractModel):
    kind: Literal["uniform_bounded"]
    earliest_start_s: FiniteFloat
    latest_start_s: FiniteFloat
    duration_s: PositiveFloat

    @model_validator(mode="after")
    def validate_start_bounds(self) -> Self:
        if self.latest_start_s < self.earliest_start_s:
            raise ValueError("latest_start_s must not precede earliest_start_s")
        return self


AvailabilityConfig: TypeAlias = Annotated[
    SimultaneousAvailabilityConfig | UniformBoundedAvailabilityConfig,
    Field(discriminator="kind"),
]


class UploadSupplyConfig(ContractModel):
    source: Literal["upload"]
    catalog_ref: OpaqueId
    availability_ref: OpaqueId
    capacity: CapacityConfig
    area_definitions_ref: OpaqueId | None = None
    area_assignments_ref: OpaqueId | None = None
    idle_policy: IdlePolicyConfig
    depot_policy: DepotReturnPolicyConfig | None = None

    @model_validator(mode="after")
    def validate_depot_policy(self) -> Self:
        if (self.area_definitions_ref is None) != (self.area_assignments_ref is None):
            raise ValueError("area definitions and assignments must be provided together")
        if self.depot_policy is not None and self.capacity.mode != "consumable":
            raise ValueError("depot policy is supported only for consumable capacity")
        return self


class GeneratedSupplyConfig(ContractModel):
    source: Literal["generated"]
    catalog_size: PositiveInt
    catalog_id_namespace: str
    availability: AvailabilityConfig
    initial_locations_ref: OpaqueId
    capacity: CapacityConfig
    capacity_value: NonNegativeFloat | None = None
    area_definitions_ref: OpaqueId | None = None
    area_assignments_ref: OpaqueId | None = None
    idle_policy: IdlePolicyConfig
    depot_policy: DepotReturnPolicyConfig | None = None

    @field_validator("catalog_id_namespace")
    @classmethod
    def validate_catalog_namespace(cls, value: str) -> str:
        if value == "":
            raise ValueError("catalog_id_namespace must be nonempty")
        return value

    @model_validator(mode="after")
    def validate_depot_policy(self) -> Self:
        if (self.area_definitions_ref is None) != (self.area_assignments_ref is None):
            raise ValueError("area definitions and assignments must be provided together")
        if self.depot_policy is not None and self.capacity.mode != "consumable":
            raise ValueError("depot policy is supported only for consumable capacity")
        if self.capacity.mode == "none" and self.capacity_value is not None:
            raise ValueError("none capacity mode cannot declare capacity_value")
        if self.capacity.mode != "none" and self.capacity_value is None:
            raise ValueError("generated finite-capacity supply requires capacity_value")
        return self


class GTFSDutiesSupplyConfig(ContractModel):
    source: Literal["gtfs_duties"]
    reconstruction_id: OpaqueId
    capacity: CapacityConfig
    area_definitions_ref: OpaqueId | None = None
    area_assignments_ref: OpaqueId | None = None
    idle_policy: IdlePolicyConfig
    depot_policy: DepotReturnPolicyConfig | None = None

    @model_validator(mode="after")
    def validate_supply(self) -> Self:
        if (self.area_definitions_ref is None) != (self.area_assignments_ref is None):
            raise ValueError("area definitions and assignments must be provided together")
        if self.depot_policy is not None and self.capacity.mode != "consumable":
            raise ValueError("depot policy is supported only for consumable capacity")
        return self


SupplyConfig: TypeAlias = Annotated[
    UploadSupplyConfig | GeneratedSupplyConfig | GTFSDutiesSupplyConfig,
    Field(discriminator="source"),
]


class PredefinedDispatchConfig(ContractModel):
    policy: Literal["predefined"]
    implementation: Literal["dispatch.predefined@1"] = "dispatch.predefined@1"
    assignment_plan_ref: OpaqueId


class NearestMatchingDispatchConfig(ContractModel):
    policy: Literal["nearest_matching"]
    implementation: Literal["dispatch.nearest_matching@1"] = "dispatch.nearest_matching@1"
    max_pickup_time_s: PositiveFloat | None = None


class BatchNearestMatchingDispatchConfig(ContractModel):
    policy: Literal["batch_nearest_matching"]
    implementation: Literal["dispatch.batch_nearest_matching@1"] = (
        "dispatch.batch_nearest_matching@1"
    )
    max_pickup_time_s: PositiveFloat | None = None
    batch_interval_s: PositiveFloat
    batch_origin_s: FiniteFloat = 0.0


class OneShotDispatchConfig(ContractModel):
    policy: Literal["one_shot"]
    implementation: Literal["dispatch.one_shot@1"] = "dispatch.one_shot@1"
    assignment_plan_ref: OpaqueId


DispatchConfig: TypeAlias = Annotated[
    PredefinedDispatchConfig
    | NearestMatchingDispatchConfig
    | BatchNearestMatchingDispatchConfig
    | OneShotDispatchConfig,
    Field(discriminator="policy"),
]


class RoutingConfig(ContractModel):
    profile_id: OpaqueId
    algorithm: Literal["static_shortest_travel_time"] = "static_shortest_travel_time"


class FleetConfig(ContractModel):
    scientific_identity_excluded_fields: ClassVar[frozenset[str]] = frozenset({"label"})

    fleet_id: OpaqueId
    label: str
    demand: DemandConfig
    supply: SupplyConfig
    dispatch: DispatchConfig
    routing: RoutingConfig

    @field_validator("label")
    @classmethod
    def validate_label(cls, value: str) -> str:
        if value == "":
            raise ValueError("fleet label must be nonempty")
        return value


class IndependentJointScenarioConfig(ContractModel):
    kind: Literal["independent_conditional_environment"]


class SharedFactorsJointScenarioConfig(ContractModel):
    kind: Literal["shared_factors"]
    model_ref: OpaqueId


JointScenarioConfig: TypeAlias = Annotated[
    IndependentJointScenarioConfig | SharedFactorsJointScenarioConfig,
    Field(discriminator="kind"),
]


class ScenarioConfig(ContractModel):
    schema_version: SchemaVersion
    environment_id: OpaqueId
    clock: ClockConfig
    fleets: tuple[FleetConfig, ...]
    replications: PositiveInt
    master_seed: UInt64
    joint_scenario_model: JointScenarioConfig

    @field_validator("fleets")
    @classmethod
    def validate_fleets(cls, value: tuple[FleetConfig, ...]) -> tuple[FleetConfig, ...]:
        if not value:
            raise ValueError("scenario requires at least one fleet")
        ids = [fleet.fleet_id for fleet in value]
        if len(set(ids)) != len(ids):
            raise ValueError("fleet IDs must be unique")
        return tuple(sorted(value, key=lambda fleet: fleet.fleet_id))


class ExposureConfig(ContractModel):
    schema_version: SchemaVersion
    simulation_id: OpaqueId
    sensing_geometry_id: OpaqueId
    bin_edges_s: tuple[FiniteFloat, ...]
    active_movement_kinds: tuple[
        Literal["service_pickup", "service_inter_step", "cruise", "depot_return", "reposition"],
        ...,
    ]
    measurement: Literal["movement_duration", "operating_duration"] = "movement_duration"
    activity_source_ref: ArtifactRef | None = None
    operating_fleet_ids: tuple[OpaqueId, ...] = ()
    fleet_idle_break_seconds: dict[OpaqueId, PositiveFloat] = Field(default_factory=dict)
    fleet_movement_kinds: dict[
        OpaqueId,
        tuple[
            Literal["service_pickup", "service_inter_step", "cruise", "depot_return", "reposition"],
            ...,
        ],
    ] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_operating_measurement(self):
        if len(set(self.operating_fleet_ids)) != len(self.operating_fleet_ids):
            raise ValueError("Operating fleet identities must be unique")
        if self.measurement == "operating_duration":
            if (
                self.activity_source_ref is None
                or self.activity_source_ref.artifact_kind != "dataset"
                or not self.operating_fleet_ids
            ):
                raise ValueError(
                    "Operating measurement requires resolved inputs and selected fleets"
                )
        elif (
            self.activity_source_ref is not None
            or self.operating_fleet_ids
            or self.fleet_idle_break_seconds
        ):
            raise ValueError("Stationary settings require operating measurement")
        if not set(self.fleet_idle_break_seconds) <= set(self.operating_fleet_ids):
            raise ValueError("Idle breaks require a selected operating fleet")
        return self

    @field_validator("fleet_movement_kinds")
    @classmethod
    def validate_fleet_movements(cls, value):
        if any(len(set(kinds)) != len(kinds) for kinds in value.values()):
            raise ValueError("Fleet movement categories must be unique")
        return {key: tuple(sorted(kinds)) for key, kinds in sorted(value.items())}

    @field_validator("bin_edges_s")
    @classmethod
    def validate_bins(cls, value: tuple[float, ...]) -> tuple[float, ...]:
        if len(value) < 2 or any(right <= left for left, right in zip(value, value[1:])):
            raise ValueError("bin_edges_s must contain at least two strictly increasing edges")
        return value

    @field_validator("active_movement_kinds")
    @classmethod
    def validate_movement_kinds(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("active_movement_kinds must be unique")
        return tuple(sorted(value))


class ExecutionOptions(ContractModel):
    scientific_identity_excluded_fields: ClassVar[frozenset[str]] = frozenset(
        {"workers", "memory_limit_bytes", "job_timeout_s", "progress_frequency_events"}
    )

    workers: PositiveInt = 1
    memory_limit_bytes: PositiveInt
    job_timeout_s: PositiveFloat | None = None
    progress_frequency_events: PositiveInt = 1000


class ExplicitFleetCountLevels(ContractModel):
    count_levels: tuple[NonNegativeInt, ...]

    @field_validator("count_levels")
    @classmethod
    def validate_levels(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if not value or tuple(sorted(set(value))) != value:
            raise ValueError("count_levels must be nonempty, sorted, and unique")
        return value


class RangeFleetCountLevels(ContractModel):
    min_count: NonNegativeInt
    max_count: NonNegativeInt
    step: PositiveInt
    include_max: bool = True

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        if self.max_count < self.min_count:
            raise ValueError("max_count must not be less than min_count")
        return self


FleetCountLevels: TypeAlias = ExplicitFleetCountLevels | RangeFleetCountLevels


class CountEnumerationConfig(ContractModel):
    fleets: dict[OpaqueId, FleetCountLevels]

    @field_validator("fleets")
    @classmethod
    def validate_count_fleets(
        cls, value: dict[str, FleetCountLevels]
    ) -> dict[str, FleetCountLevels]:
        if not value:
            raise ValueError("count enumeration requires at least one fleet")
        return value


class ExplicitBudgetLevels(ContractModel):
    levels_minor: tuple[NonNegativeInt, ...]

    @field_validator("levels_minor")
    @classmethod
    def validate_levels(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if not value or tuple(sorted(set(value))) != value:
            raise ValueError("levels_minor must be nonempty, sorted, and unique")
        return value


class RangeBudgetLevels(ContractModel):
    min_minor: NonNegativeInt
    max_minor: NonNegativeInt
    step_minor: PositiveInt
    include_max: bool = True

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        if self.max_minor < self.min_minor:
            raise ValueError("max_minor must not be less than min_minor")
        return self


BudgetLevels: TypeAlias = ExplicitBudgetLevels | RangeBudgetLevels


class PortfolioCosts(ContractModel):
    unit: str
    minor_unit_scale: PositiveInt
    by_fleet_minor: dict[OpaqueId, NonNegativeInt]

    @field_validator("unit")
    @classmethod
    def validate_unit(cls, value: str) -> str:
        if value == "":
            raise ValueError("cost unit must be nonempty")
        return value


class UtilityConfig(ContractModel):
    kind: Literal["exponential_saturation", "linear_diagnostic", "linear_capped", "binary"]
    saturation_s: PositiveFloat
    weights_ref: OpaqueId
    temporal_interval_s: PositiveFloat | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class ObjectiveComparisonResolution(ContractModel):
    mean_utility: PositiveFloat = 1e-12
    std_utility: PositiveFloat = 1e-12
    p05_utility: PositiveFloat = 1e-12


class PortfolioConfig(ContractModel):
    schema_version: SchemaVersion
    exposure_id: OpaqueId
    utility: UtilityConfig
    count_enumeration: CountEnumerationConfig
    budgets: BudgetLevels
    costs: PortfolioCosts
    sampling_rounds: PositiveInt
    sampling_seed: UInt64
    sampling_design: Literal["joint_replication_uniform_vehicle"]
    comparison_resolution: ObjectiveComparisonResolution
    sample_matrix_storage: Literal["materialized", "reconstruct"] = "materialized"
    sensing_statistics_mode: Literal["eager", "on_demand"] = "eager"
    risk_metric: Literal["std", "p05"] = "std"

    @model_validator(mode="after")
    def validate_fleet_cost_alignment(self) -> Self:
        count_fleets = set(self.count_enumeration.fleets)
        cost_fleets = set(self.costs.by_fleet_minor)
        if count_fleets != cost_fleets:
            raise ValueError("count_enumeration and costs must contain identical fleet IDs")
        return self


class ProjectRevision(ContractModel):
    scientific_identity_excluded_fields: ClassVar[frozenset[str]] = frozenset(
        {"project_id", "revision_id", "name", "description"}
    )

    schema_version: SchemaVersion
    project_id: OpaqueId
    revision_id: OpaqueId
    name: str
    description: str
    scenario: ScenarioConfig
    default_exposure: ExposureConfig
    default_portfolio: PortfolioConfig | None = None
    dataset_references: tuple[OpaqueId, ...] = ()

    @field_validator("dataset_references")
    @classmethod
    def validate_dataset_references(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("dataset_references must be unique")
        return tuple(sorted(value))

    @model_validator(mode="after")
    def validate_linked_configs(self) -> Self:
        if self.scenario.schema_version != self.schema_version:
            raise ValueError("scenario schema version must match project revision")
        if self.default_exposure.schema_version != self.schema_version:
            raise ValueError("exposure schema version must match project revision")
        if (
            self.default_portfolio is not None
            and self.default_portfolio.schema_version != self.schema_version
        ):
            raise ValueError("portfolio schema version must match project revision")
        return self


def utc_origin(value: str) -> datetime:
    """Parse a UTC origin for Python callers; external JSON remains schema validated."""

    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("origin must contain an offset")
    return parsed
