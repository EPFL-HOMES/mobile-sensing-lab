"""Persistent M09 job, project, event, and query contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


JobKind = Literal[
    "import",
    "environment",
    "studio_environment",
    "region_search",
    "studio_resolve",
    "studio_run",
    "studio_analysis",
    "example_import",
    "gtfs_reconstruction",
    "scenario_validation",
    "simulation",
    "exposure",
    "portfolio",
    "export",
    "project_files",
    "project_export",
    "project_import",
    "test_probe",
]
JobStatus = Literal[
    "queued", "initializing", "running", "finalizing", "completed", "failed", "cancelled"
]
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


class ApiModel(BaseModel):
    """JSON-facing model: strict fields with ordinary JSON array coercion."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class JobSubmission(ApiModel):
    job_id: str
    resource_id: str
    status_url: str
    events_url: str
    cache_hit: bool = False


class JobSnapshot(ApiModel):
    job_id: str
    kind: JobKind
    project_id: str | None
    resource_id: str
    request_hash: str
    status: JobStatus
    phase: str
    attempt: int
    cancel_requested: bool
    counters: dict[str, int | None]
    result: dict[str, Any] | None
    error_code: str | None
    error_message: str | None
    cache_source_job_id: str | None
    created_at_utc: datetime
    updated_at_utc: datetime


class JobEvent(ApiModel):
    event_id: int
    job_id: str
    type: Literal["status", "progress", "warning", "completed", "failed", "cancelled"]
    timestamp_utc: datetime
    phase: str
    payload: dict[str, Any]


class ProjectCreate(ApiModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4_000)


class ProjectRecord(ProjectCreate):
    project_id: str
    current_revision_id: str | None
    created_at_utc: datetime
    updated_at_utc: datetime
    status: Literal["new", "configured", "running", "results"] = "new"


class RevisionCreate(ApiModel):
    base_revision_id: str | None = None
    payload: dict[str, Any]


class RevisionRecord(ApiModel):
    project_id: str
    revision_id: str
    base_revision_id: str | None
    payload: dict[str, Any]
    created_at_utc: datetime


class ResourceRecord(ApiModel):
    resource_id: str
    kind: str
    content_hash: str
    artifact_kind: str | None
    job_id: str | None
    metadata: dict[str, Any]
    created_at_utc: datetime


class Page(ApiModel):
    items: list[dict[str, Any]]
    returned_count: int
    total_matching_count: int
    next_cursor: str | None
    is_complete: bool


class JobStoreLimits(ApiModel):
    max_event_payload_bytes: int = Field(default=64 * 1024, ge=1024, le=1024 * 1024)
    max_events_per_job: int = Field(default=2_000, ge=10, le=100_000)
    max_job_payload_bytes: int = Field(default=4 * 1024 * 1024, ge=1024)
    max_upload_bytes: int = Field(default=256 * 1024 * 1024, ge=1024)
    default_page_size: int = Field(default=100, ge=1, le=1_000)
    max_page_size: int = Field(default=1_000, ge=1, le=10_000)
    max_map_features: int = Field(default=50_000, ge=1)
    max_map_bytes: int = Field(default=32 * 1024 * 1024, ge=1024)
    max_matrix_rows: int = Field(default=100_000, ge=1)
    max_matrix_bytes: int = Field(default=20 * 1024 * 1024, ge=256)
