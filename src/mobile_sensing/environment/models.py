"""Immutable metadata and local dataset definitions for prepared environments."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Mapping

from mobile_sensing.contracts import (
    ContractModel,
    EnvironmentArtifactRef,
    NonNegativeFloat,
    NonNegativeInt,
    OpaqueId,
    PositiveFloat,
    SchemaVersion,
    Sha256,
)

if TYPE_CHECKING:
    import geopandas as gpd

    from mobile_sensing.environment.network import PreparedRoutingService
    from mobile_sensing.environment.snapping import NodeSnapper


DatasetRole = Literal["boundary", "network", "grid", "population"]


@dataclass(frozen=True, slots=True)
class LocalDatasetResource:
    """A local file registered under a portable dataset identity."""

    dataset_id: str
    role: DatasetRole
    path: Path
    source_crs: str
    layer: str | None = None
    attribution: tuple[str, ...] = ("Local project data",)
    population_cell_size_m: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path).resolve())
        if not self.dataset_id or not self.source_crs:
            raise ValueError("local resource requires nonempty dataset_id and source_crs")
        if not self.attribution or any(not item for item in self.attribution):
            raise ValueError("local resource requires nonempty attribution")
        if self.population_cell_size_m is not None and self.population_cell_size_m <= 0:
            raise ValueError("population_cell_size_m must be positive")


@dataclass(frozen=True, slots=True)
class LocalRegionSelection:
    """A named selection from a registered boundary layer."""

    dataset_id: str
    municipality_names: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if not self.dataset_id:
            raise ValueError("region selection requires a dataset_id")
        if self.municipality_names is not None:
            names = tuple(sorted(self.municipality_names))
            if not names or any(not name for name in names) or len(set(names)) != len(names):
                raise ValueError("municipality names must be nonempty and unique")
            object.__setattr__(self, "municipality_names", names)


class EnvironmentPreparationMetadata(ContractModel):
    schema_version: SchemaVersion
    environment: EnvironmentArtifactRef
    working_crs: str
    network_hash: Sha256
    mobility_hash: Sha256
    sensing_hash: Sha256
    grid_axis_hash: Sha256
    profile_hashes: dict[OpaqueId, Sha256]
    profile_source_counts: dict[OpaqueId, dict[str, NonNegativeInt]]
    network_repair_hash: Sha256
    network_repair_policy: Literal["strict@1", "conservative_repair@1", "quarantine_invalid@1"]
    network_readiness_grade: Literal["strict_ready", "scenario_ready_with_quarantine", "not_ready"]
    repaired_source_rows: NonNegativeInt
    quarantined_source_rows: NonNegativeInt
    split_source_node_count: NonNegativeInt
    snapping_max_distance_m: PositiveFloat
    sensing_boundary_source_rows: NonNegativeInt
    sensing_boundary_selected_rows: NonNegativeInt
    routing_boundary_source_rows: NonNegativeInt
    routing_boundary_selected_rows: NonNegativeInt
    road_source_rows: NonNegativeInt
    road_selected_rows: NonNegativeInt
    road_node_count: NonNegativeInt
    directed_edge_count: NonNegativeInt
    grid_source_rows: NonNegativeInt
    grid_selected_rows: NonNegativeInt
    population_source_rows: NonNegativeInt
    population_matched_rows: NonNegativeInt
    population_unmatched_rows: NonNegativeInt
    population_input_mass: NonNegativeFloat
    population_matched_mass: NonNegativeFloat
    assumptions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PreparedEnvironment:
    """Read-only aggregate reconstructed from an immutable local artifact."""

    reference: EnvironmentArtifactRef
    directory: Path
    metadata: EnvironmentPreparationMetadata
    boundary: "gpd.GeoDataFrame"
    grid_cells: "gpd.GeoDataFrame"
    nodes: "gpd.GeoDataFrame"
    road_edges: "gpd.GeoDataFrame"
    routing_weights: object
    population_features: object
    repair_actions: object
    edge_lineage: object
    node_lineage: object
    quarantined_edges: "gpd.GeoDataFrame"
    repair_issues: object
    repair_summary: object
    repaired_roads: "gpd.GeoDataFrame"
    routing: "PreparedRoutingService"
    snapping: "NodeSnapper"

    @property
    def factored_hashes(self) -> Mapping[str, str]:
        return {
            "mobility": self.metadata.mobility_hash,
            "sensing": self.metadata.sensing_hash,
        }

    @property
    def grid_spatial_index(self):
        """Lazily construct the metric grid index supplied by GeoPandas."""

        return self.grid_cells.sindex
