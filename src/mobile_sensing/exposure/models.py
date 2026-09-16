"""Typed in-memory records for exact sensing exposure allocation."""

from __future__ import annotations

from dataclasses import dataclass

from mobile_sensing.contracts import SparseExposureRow, VehicleKey


@dataclass(frozen=True, slots=True)
class EdgeGridPiece:
    edge_id: str
    piece_index: int
    cell_id: str | None
    start_fraction: float
    end_fraction: float

    def __post_init__(self) -> None:
        if not self.edge_id or self.piece_index < 0:
            raise ValueError("edge-grid piece requires a valid edge identity and index")
        if not 0.0 <= self.start_fraction < self.end_fraction <= 1.0:
            raise ValueError("edge-grid piece fractions must be positive and bounded")


@dataclass(frozen=True, slots=True)
class VehicleExposureDiagnostic:
    replication_id: str
    vehicle: VehicleKey
    active_moving_time_s: float
    in_grid_duration_s: float
    outside_grid_duration_s: float
    conservation_residual_s: float
    stationary_time_s: float = 0.0
    excluded_depot_time_s: float = 0.0
    excluded_offduty_time_s: float = 0.0


@dataclass(frozen=True, slots=True)
class AllocationResult:
    rows: tuple[SparseExposureRow, ...]
    diagnostics: tuple[VehicleExposureDiagnostic, ...]
