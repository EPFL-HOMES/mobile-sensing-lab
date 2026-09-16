"""Structured validation issue contract shared by non-HTTP entry points."""

from __future__ import annotations

from enum import StrEnum

from mobile_sensing.contracts.base import ContractModel, NonNegativeInt, OpaqueId


class IssueSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class ValidationIssue(ContractModel):
    severity: IssueSeverity
    code: str
    field_path: str
    message: str
    corrective_action: str
    dataset_id: OpaqueId | None = None
    table: str | None = None
    source_row: NonNegativeInt | None = None
    task_id: OpaqueId | None = None
