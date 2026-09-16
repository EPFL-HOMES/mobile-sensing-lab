"""Versioned human authoring contracts resolved into the common scientific contracts."""

from typing import Any, Literal
import json
from pydantic import Field, field_validator
from mobile_sensing.contracts import ContractModel, ArtifactRef, EnvironmentArtifactRef
from mobile_sensing.jobs.models import ApiModel


class FeatureSelection(ContractModel):
    input_id: str
    name: str = Field(min_length=1)
    value_column: str = "residents"
    unit: str = "count"
    year: int | None = None
    source_cell_size_m: float = Field(default=100, gt=0)
    x_column: str = "easting"
    y_column: str = "northing"
    coordinate_anchor: Literal["point", "lower_left"] = "point"


class EnvironmentEditor(ContractModel):
    boundary_input: str | None = None
    region_query: str = ""
    drawn_boundary: dict[str, Any] | None = None
    network_input: str | None = None
    grid_input: str | None = None
    speed_input: str | None = None
    working_crs: str = "auto"
    timezone: str = "UTC"
    snap_distance_m: float = Field(default=250, gt=0)
    grid_size_m: float = Field(default=100, ge=10)
    routing_buffer_m: float = Field(default=1000, ge=0, le=50000)
    speed_source: Literal["constant", "file", "osm"] = "constant"
    speed_kph: float = Field(default=30, gt=0, le=200)
    speed_column: str = "speed_kph"
    road_columns: dict[str, str] = Field(default_factory=dict)
    features: tuple[FeatureSelection, ...] = ()
    osm_features: tuple[
        Literal[
            "transportation",
            "residential",
            "commercial",
            "industrial",
            "public_services",
            "leisure",
        ],
        ...,
    ] = ()
    topology_policy: Literal["strict@1", "conservative_repair@1", "quarantine_invalid@1"] = (
        "quarantine_invalid@1"
    )


class EnvironmentResult(ContractModel):
    artifact: EnvironmentArtifactRef
    features: ArtifactRef
    feature_names: tuple[str, ...]
    working_crs: str
    timezone: str
    assumptions: tuple[str, ...] = ()


class EnvironmentEditorRequest(ApiModel):
    config: EnvironmentEditor

    @field_validator("config", mode="before")
    @classmethod
    def json_config(cls, value):
        return (
            value
            if isinstance(value, EnvironmentEditor)
            else EnvironmentEditor.model_validate_json(json.dumps(value))
        )


class WorkspaceInfo(ApiModel):
    directory: str
    example_project_id: str | None = None
    example_job_id: str | None = None
    example_project_ids: tuple[str, ...] = ()
    example_job_ids: tuple[str, ...] = ()


class RegionSearchRequest(ApiModel):
    query: str = Field(min_length=2, max_length=250)


class RegionSearchResult(ApiModel):
    query: str
    name: str
    boundary: dict[str, Any]
    source: str
