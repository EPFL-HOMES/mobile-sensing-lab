"""Typed portfolio sampling, summary, and frontier contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Self

from pydantic import field_validator, model_validator

from mobile_sensing.contracts import (
    ArtifactRef,
    ContractModel,
    NonNegativeFloat,
    NonNegativeInt,
    OpaqueId,
    PositiveInt,
    SchemaVersion,
)


class CellTimeWeight(ContractModel):
    cell_id: OpaqueId
    time_bin_id: OpaqueId
    raw_weight: NonNegativeFloat


class CellWeight(ContractModel):
    cell_id: OpaqueId
    raw_weight: NonNegativeFloat


class UtilityWeightResource(ContractModel):
    """Resolved evaluation weights; source acquisition remains outside M08A."""

    schema_version: SchemaVersion = "2.0"
    weights_id: OpaqueId
    kind: Literal[
        "uniform_spatial_duration_temporal", "resolved_cell_time", "spatial_duration_temporal"
    ]
    missing_policy: Literal["error", "zero"] = "error"
    values: tuple[CellTimeWeight, ...] = ()
    spatial_values: tuple[CellWeight, ...] = ()
    provenance: str

    @field_validator("values")
    @classmethod
    def canonical_values(cls, value: tuple[CellTimeWeight, ...]) -> tuple[CellTimeWeight, ...]:
        keys = tuple((item.cell_id, item.time_bin_id) for item in value)
        if len(keys) != len(set(keys)):
            raise ValueError("utility weights require unique cell/time keys")
        return tuple(sorted(value, key=lambda item: (item.cell_id, item.time_bin_id)))

    @model_validator(mode="after")
    def validate_kind(self) -> Self:
        if self.provenance == "":
            raise ValueError("utility weight provenance must be nonempty")
        if self.kind == "spatial_duration_temporal":
            if self.values or not self.spatial_values or self.missing_policy != "error":
                raise ValueError("Factored weights require complete spatial values only")
            ids = [value.cell_id for value in self.spatial_values]
            if ids != sorted(set(ids)):
                raise ValueError("Spatial values must have sorted unique cell IDs")
            return self
        if self.spatial_values:
            raise ValueError("Spatial values require spatial_duration_temporal weights")
        if self.kind == "uniform_spatial_duration_temporal":
            if self.values or self.missing_policy != "error":
                raise ValueError("uniform weights do not accept values or a missing policy")
        elif not self.values:
            raise ValueError("resolved cell/time weights require at least one value")
        return self


class PortfolioResourceLimits(ContractModel):
    """Execution guards; excluded from scientific identity."""

    max_count_portfolios: PositiveInt = 10_000
    max_budget_levels: PositiveInt = 10_000
    max_portfolio_round_records: PositiveInt = 1_000_000
    max_estimated_matrix_bytes: PositiveInt = 4 * 1024**3
    max_working_bytes: PositiveInt = 1024**3


class PortfolioPreview(ContractModel):
    schema_version: SchemaVersion = "2.0"
    exposure_id: OpaqueId
    replications_R: PositiveInt
    sampling_rounds_J: PositiveInt
    catalog_size_by_fleet: dict[OpaqueId, PositiveInt]
    resolved_count_levels: dict[OpaqueId, tuple[NonNegativeInt, ...]]
    resolved_budget_levels_minor: tuple[NonNegativeInt, ...]
    raw_count_portfolios: NonNegativeInt
    feasible_count_portfolios: NonNegativeInt | None
    planned_portfolio_round_records: NonNegativeInt | None
    exposure_nonzero_rows: NonNegativeInt
    estimated_sample_matrix_rows: NonNegativeInt | None
    estimated_sample_matrix_bytes: NonNegativeInt | None
    estimated_working_bytes: NonNegativeInt
    blocking_reasons: tuple[str, ...]

    @property
    def blocked(self) -> bool:
        return bool(self.blocking_reasons)


@dataclass(frozen=True, slots=True)
class PortfolioEvaluationResult:
    reference: ArtifactRef
    preview: PortfolioPreview
    count_portfolios: int
    sampling_rounds_J: int
    unique_sample_matrices: int
    portfolio_samples: int


@dataclass(frozen=True, slots=True)
class PortfolioAnalysisResult:
    """Published M08B summaries linked losslessly to one M08A sample artifact."""

    reference: ArtifactRef
    sample_reference: ArtifactRef
    replications_R: int
    sampling_rounds_J: int
    count_portfolios: int
    budget_levels: int
    frontier_memberships: int
    sensing_statistic_rows: int
