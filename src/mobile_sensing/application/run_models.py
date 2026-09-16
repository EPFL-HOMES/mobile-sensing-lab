"""Named run, analysis and migration contracts for the public workbench."""

import json
from pydantic import Field, field_validator
from mobile_sensing.contracts import ArtifactRef, ContractModel
from mobile_sensing.application.project_models import ProjectConfig, PortfolioEditor
from mobile_sensing.jobs.models import ApiModel


class RunOptions(ContractModel):
    workers: int = Field(default=1, ge=1, le=32)
    memory_limit_bytes: int = Field(default=4 * 1024**3, ge=64 * 1024**2)
    job_timeout_s: float = Field(default=7200, gt=0)


class StudioRunRequest(ApiModel):
    name: str = Field(default="Simulation run", min_length=1, max_length=200)
    config: ProjectConfig
    options: RunOptions = Field(default_factory=RunOptions)

    @field_validator("config", "options", mode="before")
    @classmethod
    def decode(cls, value, info):
        model = ProjectConfig if info.field_name == "config" else RunOptions
        return value if isinstance(value, model) else model.model_validate_json(json.dumps(value))


class StudioAnalysisRequest(ApiModel):
    name: str = Field(default="Portfolio analysis", min_length=1, max_length=200)
    config: PortfolioEditor
    options: RunOptions = Field(default_factory=RunOptions)

    @field_validator("config", "options", mode="before")
    @classmethod
    def decode(cls, value, info):
        model = PortfolioEditor if info.field_name == "config" else RunOptions
        return value if isinstance(value, model) else model.model_validate_json(json.dumps(value))


class RunView(ContractModel):
    run_id: str
    artifact: ArtifactRef
    name: str
    source_revision_id: str | None
    config: ProjectConfig
    simulation: ArtifactRef
    exposure: ArtifactRef
    resolution: ArtifactRef
    replications: int
    vehicle_counts: dict[str, int]
    task_counts: dict[str, tuple[int, ...]]
    assumptions: tuple[str, ...]
    mobility_reused: bool
    realization_source_run_id: str | None = None
    elapsed_seconds: dict[str, float]


class AnalysisView(ContractModel):
    analysis_id: str
    artifact: ArtifactRef
    name: str
    source_run_id: str
    source_revision_id: str | None
    samples: ArtifactRef
    frontier: ArtifactRef
    config: PortfolioEditor
    replications: int
    sampling_runs: int
    count_portfolios: int
    elapsed_seconds: dict[str, float]


class UtilityCurveRequest(ApiModel):
    kind: str = "exponential"
    saturation_minutes: float = Field(default=15, gt=0)


class UtilityCurve(ContractModel):
    exposure_minutes: tuple[float, ...]
    utility: tuple[float, ...]
    interpretation: str


class MigrationView(ContractModel):
    config: ProjectConfig
    notices: tuple[str, ...]
