"""Exact sparse sensing exposure allocation and immutable access."""

from mobile_sensing.exposure.allocation import (
    CONSERVATION_ABS_TOLERANCE_S,
    CONSERVATION_REL_TOLERANCE,
    EXPOSURE_ALLOCATION_VERSION,
    ExposureAllocationError,
    allocate_movement_exposure,
    coarsen_sparse_exposure,
)
from mobile_sensing.exposure.geometry import (
    EDGE_GRID_ALGORITHM_VERSION,
    GEOMETRY_TOLERANCE_M,
    ROAD_GRID_DOMAIN_VERSION,
    EdgeGridPreparationError,
    build_edge_grid_pieces,
    road_intersecting_grid_cells,
)
from mobile_sensing.exposure.models import (
    AllocationResult,
    EdgeGridPiece,
    VehicleExposureDiagnostic,
)
from mobile_sensing.exposure.storage import (
    DEFAULT_EXPOSURE_CHUNK_ROWS,
    DEFAULT_MATRIX_NONZERO_LIMIT,
    EXPOSURE_STORAGE_VERSION,
    ExposureArtifactReader,
    ExposureSensingQueryService,
    build_time_axis,
    publish_exposure_artifact,
)

__all__ = [
    "CONSERVATION_ABS_TOLERANCE_S",
    "CONSERVATION_REL_TOLERANCE",
    "DEFAULT_EXPOSURE_CHUNK_ROWS",
    "DEFAULT_MATRIX_NONZERO_LIMIT",
    "EDGE_GRID_ALGORITHM_VERSION",
    "EXPOSURE_ALLOCATION_VERSION",
    "EXPOSURE_STORAGE_VERSION",
    "GEOMETRY_TOLERANCE_M",
    "ROAD_GRID_DOMAIN_VERSION",
    "AllocationResult",
    "EdgeGridPiece",
    "EdgeGridPreparationError",
    "ExposureAllocationError",
    "ExposureArtifactReader",
    "ExposureSensingQueryService",
    "VehicleExposureDiagnostic",
    "allocate_movement_exposure",
    "build_edge_grid_pieces",
    "road_intersecting_grid_cells",
    "build_time_axis",
    "coarsen_sparse_exposure",
    "publish_exposure_artifact",
]
