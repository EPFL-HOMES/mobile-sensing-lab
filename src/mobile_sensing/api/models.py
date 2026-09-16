"""Typed M09 HTTP request/response contracts."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import Field, field_validator, model_validator

from mobile_sensing.application import LocalCatalogConfig, ScenarioResourceBundle
from mobile_sensing.contracts import (
    ArtifactRef,
    EnvironmentArtifactRef,
    EnvironmentBuildConfig,
    EnvironmentProviderRequest,
    ExecutionOptions,
    ExposureConfig,
    PortfolioConfig,
    VehicleKey,
)
from mobile_sensing.datasets import (
    DemandImportMapping,
    GTFSReconstructionConfig,
    RateImportMapping,
    VehicleImportMapping,
)
from mobile_sensing.jobs.models import ApiModel
from mobile_sensing.portfolio import PortfolioResourceLimits, UtilityWeightResource


def _from_json(model, value):
    if isinstance(value, model):
        return value
    return model.model_validate_json(json.dumps(value, separators=(",", ":")))


class ErrorIssue(ApiModel):
    severity: Literal["error", "warning", "info"] = "error"
    code: str
    field_path: str = ""
    message: str
    corrective_action: str | None = None


class ErrorBody(ApiModel):
    code: str
    message: str
    issues: list[ErrorIssue] = Field(default_factory=list)
    request_id: str


class MeanFleetRow(ApiModel):
    fleet_id: str
    catalog_size: int
    mean_released_tasks: float
    mean_completed_tasks: float
    mean_completion_rate: float | None
    completion_rate_replications: int
    mean_active_vehicles: float
    mean_carry_in_tasks: float
    mean_status_counts: dict[str, float]


class MeanFleetTimeRow(ApiModel):
    time_bin_id: str
    start_s: float
    end_s: float
    mean_arrivals: float
    mean_active_vehicles: float


class MeanFleetView(ApiModel):
    exposure_id: str
    simulation_id: str
    replications_R: int
    statistic: Literal["mean"]
    time_start_s: float
    time_end_s: float
    fleets: list[MeanFleetRow]
    time_summary: list[MeanFleetTimeRow]
    task_semantics: str
    complete: bool


class PortfolioBudgetView(ApiModel):
    budget_id: str
    budget_minor: int
    cost_unit: str
    minor_unit_scale: int
    replications_R: int
    sampling_rounds_J: int
    feasible_portfolio_count: int
    frontier_portfolio_count: int | None
    frontier_enabled: bool
    disabled_reason: str | None


class PortfolioFrontierPoint(ApiModel):
    budget_id: str
    budget_minor: int
    portfolio_id: str
    replications_R: int
    sampling_rounds_J: int
    count_by_fleet: dict[str, int]
    cost_by_fleet: dict[str, float]
    total_cost: float
    total_cost_minor: int
    unspent_minor: int
    feasible: bool
    nondominated: bool | None
    tie_group_id: str | None
    mean_comparison_key: str
    std_comparison_key: str | None
    risk_comparison_key: str | None = None
    utility_mean: float
    utility_sample_variance: float | None
    utility_sample_std: float | None
    conditional_mean_se: float | None
    utility_min: float
    utility_max: float
    utility_p05: float
    utility_p50: float
    utility_p95: float
    sample_count: int


class PortfolioFrontierView(ApiModel):
    max_mean_utility: float | None = None
    max_p05_utility: float | None = None
    frontier_portfolio_count: int = 0
    analysis_id: str
    risk_metric: Literal["std", "p05"] = "std"
    budget: PortfolioBudgetView
    replications_R: int
    sampling_rounds_J: int
    variability_interpretation: str
    inference_scope: str
    points: list[PortfolioFrontierPoint]
    returned_count: int
    is_complete: Literal[True]


class EnvironmentJobRequest(ApiModel):
    catalog: LocalCatalogConfig
    request: EnvironmentProviderRequest
    config: EnvironmentBuildConfig
    catalog_relative_to: str | None = None

    @field_validator("catalog", mode="before")
    @classmethod
    def parse_catalog(cls, value):
        return _from_json(LocalCatalogConfig, value)

    @field_validator("request", mode="before")
    @classmethod
    def parse_request(cls, value):
        return _from_json(EnvironmentProviderRequest, value)

    @field_validator("config", mode="before")
    @classmethod
    def parse_config(cls, value):
        return _from_json(EnvironmentBuildConfig, value)


class ImportJobRequest(ApiModel):
    import_kind: Literal["demand", "supply"]
    source_id: str
    environment: EnvironmentArtifactRef
    mapping: DemandImportMapping | RateImportMapping | VehicleImportMapping
    routing_profile_id: str | None = None

    @field_validator("environment", mode="before")
    @classmethod
    def parse_environment(cls, value):
        return _from_json(EnvironmentArtifactRef, value)

    @field_validator("mapping", mode="before")
    @classmethod
    def parse_mapping(cls, value):
        if isinstance(value, (DemandImportMapping, RateImportMapping, VehicleImportMapping)):
            return value
        adapter = value.get("adapter") if isinstance(value, dict) else None
        if adapter == "supply.upload_vehicle_catalog@1":
            model = VehicleImportMapping
        elif adapter == "demand.upload_sparse_od_rate@1":
            model = RateImportMapping
        else:
            model = DemandImportMapping
        return _from_json(model, value)

    @model_validator(mode="after")
    def validate_import_mapping(self):
        if self.import_kind == "supply" and not isinstance(self.mapping, VehicleImportMapping):
            raise ValueError("supply import requires a vehicle mapping")
        if self.import_kind == "demand" and isinstance(self.mapping, VehicleImportMapping):
            raise ValueError("demand import requires a demand or rate mapping")
        return self


class GTFSJobRequest(ApiModel):
    source_directory: str
    environment: EnvironmentArtifactRef
    config: GTFSReconstructionConfig

    @field_validator("environment", mode="before")
    @classmethod
    def parse_environment(cls, value):
        return _from_json(EnvironmentArtifactRef, value)

    @field_validator("config", mode="before")
    @classmethod
    def parse_config(cls, value):
        return _from_json(GTFSReconstructionConfig, value)


class ScenarioJobRequest(ApiModel):
    environment: EnvironmentArtifactRef
    resources: ScenarioResourceBundle

    @field_validator("environment", mode="before")
    @classmethod
    def parse_environment(cls, value):
        return _from_json(EnvironmentArtifactRef, value)

    @field_validator("resources", mode="before")
    @classmethod
    def parse_resources(cls, value):
        return _from_json(ScenarioResourceBundle, value)


class SimulationJobRequest(ScenarioJobRequest):
    options: ExecutionOptions

    @field_validator("options", mode="before")
    @classmethod
    def parse_options(cls, value):
        return _from_json(ExecutionOptions, value)


class ExposureJobRequest(ApiModel):
    environment: EnvironmentArtifactRef
    simulation: ArtifactRef
    config: ExposureConfig

    @field_validator("environment", mode="before")
    @classmethod
    def parse_environment(cls, value):
        return _from_json(EnvironmentArtifactRef, value)

    @field_validator("simulation", mode="before")
    @classmethod
    def parse_simulation(cls, value):
        return _from_json(ArtifactRef, value)

    @field_validator("config", mode="before")
    @classmethod
    def parse_config(cls, value):
        return _from_json(ExposureConfig, value)


class PortfolioPreviewRequest(ApiModel):
    exposure: ArtifactRef
    config: PortfolioConfig
    limits: PortfolioResourceLimits | None = None

    @field_validator("exposure", mode="before")
    @classmethod
    def parse_exposure(cls, value):
        return _from_json(ArtifactRef, value)

    @field_validator("config", mode="before")
    @classmethod
    def parse_config(cls, value):
        return _from_json(PortfolioConfig, value)

    @field_validator("limits", mode="before")
    @classmethod
    def parse_limits(cls, value):
        return None if value is None else _from_json(PortfolioResourceLimits, value)


class PortfolioAnalysisJobRequest(ApiModel):
    mode: Literal["samples", "analysis"]
    config: PortfolioConfig
    exposure: ArtifactRef | None = None
    weights: UtilityWeightResource | None = None
    samples: ArtifactRef | None = None
    limits: PortfolioResourceLimits | None = None

    @field_validator("config", mode="before")
    @classmethod
    def parse_config(cls, value):
        return _from_json(PortfolioConfig, value)

    @field_validator("exposure", "samples", mode="before")
    @classmethod
    def parse_artifact(cls, value):
        return None if value is None else _from_json(ArtifactRef, value)

    @field_validator("weights", mode="before")
    @classmethod
    def parse_weights(cls, value):
        return None if value is None else _from_json(UtilityWeightResource, value)

    @field_validator("limits", mode="before")
    @classmethod
    def parse_limits(cls, value):
        return None if value is None else _from_json(PortfolioResourceLimits, value)

    @model_validator(mode="after")
    def validate_mode_inputs(self):
        if self.mode == "samples" and (self.exposure is None or self.weights is None):
            raise ValueError("samples mode requires exposure and weights")
        if self.mode == "analysis" and self.samples is None:
            raise ValueError("analysis mode requires samples")
        return self


class ExportJobRequest(ApiModel):
    resource_id: str
    table: str
    format: Literal["parquet", "csv"] = "parquet"


class MatrixQueryRequest(ApiModel):
    resource_id: str
    kind: Literal[
        "vehicle_exposure", "operational_aggregate", "portfolio_sample", "portfolio_summary"
    ]
    replication_ids: tuple[str, ...] = ()
    vehicle_keys: tuple[VehicleKey, ...] = ()
    portfolio_id: str | None = None
    round_id: int | None = Field(default=None, ge=0)
    cell_ids: tuple[str, ...] = ()
    time_bin_ids: tuple[str, ...] = ()
    statistic: Literal["realization", "mean", "variance", "std"]
    temporal_aggregation: Literal["bins", "sum"] = "bins"

    @field_validator("replication_ids", "vehicle_keys", "cell_ids", "time_bin_ids", mode="before")
    @classmethod
    def parse_json_arrays(cls, value):
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_scope(self):
        if self.kind == "vehicle_exposure" and len(self.vehicle_keys) != 1:
            raise ValueError("vehicle_exposure requires exactly one vehicle")
        if self.kind == "portfolio_sample" and (
            self.portfolio_id is None or self.round_id is None or self.statistic != "realization"
        ):
            raise ValueError(
                "portfolio_sample requires portfolio_id, round_id, and realization statistic"
            )
        if self.kind == "portfolio_summary" and (
            self.portfolio_id is None or self.statistic == "realization"
        ):
            raise ValueError("portfolio_summary requires portfolio_id and an aggregate statistic")
        return self
