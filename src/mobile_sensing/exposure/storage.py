"""Immutable exposure publication, complete sparse reads, and matrix queries."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterator, Sequence
from pathlib import Path

import geopandas as gpd
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.dataset as ds

from mobile_sensing.artifacts import (
    PartitionedTableData,
    partition_file,
    publish_partitioned_artifact,
    read_partitioned_table,
    verify_partitioned_artifact,
)
from mobile_sensing.contracts import (
    ArtifactDependency,
    ArtifactRef,
    ExposureConfig,
    ExposureReadMetadata,
    GridAxis,
    PartitionAxis,
    ScientificIdentity,
    SensingMatrixQuery,
    SensingMatrixSlice,
    SensingMatrixValue,
    SparseExposureChunk,
    SparseExposureRow,
    TimeAxis,
    TimeBin,
    VehicleKey,
    scientific_hash,
    scientific_projection,
    stable_id,
)
from mobile_sensing.exposure.allocation import (
    EXPOSURE_ALLOCATION_VERSION,
    allocate_movement_exposure,
)
from mobile_sensing.exposure.geometry import (
    EDGE_GRID_ALGORITHM_VERSION,
    ROAD_GRID_DOMAIN_VERSION,
    build_edge_grid_pieces,
    road_intersecting_grid_cells,
)
from mobile_sensing.simulation.storage import (
    SIMULATION_STORAGE_VERSION,
    SimulationArtifactReader,
)


EXPOSURE_STORAGE_VERSION = "exposure-parquet@1"
DEFAULT_EXPOSURE_CHUNK_ROWS = 10_000
DEFAULT_MATRIX_NONZERO_LIMIT = 100_000

EDGE_GRID_SCHEMA = pa.schema(
    [
        ("edge_id", pa.string(), False),
        ("piece_index", pa.int64(), False),
        ("cell_id", pa.string(), True),
        ("start_fraction", pa.float64(), False),
        ("end_fraction", pa.float64(), False),
    ]
)

EXPOSURE_SCHEMA = pa.schema(
    [
        ("replication_id", pa.string(), False),
        ("fleet_id", pa.string(), False),
        ("vehicle_id", pa.string(), False),
        ("cell_id", pa.string(), False),
        ("time_bin_id", pa.string(), False),
        ("duration_s", pa.float64(), False),
    ]
)

GRID_AXIS_SCHEMA = pa.schema(
    [
        ("cell_id", pa.string(), False),
        ("canonical_index", pa.int64(), False),
        ("grid_axis_id", pa.string(), False),
        ("grid_axis_hash", pa.string(), False),
        ("working_crs", pa.string(), False),
    ]
)

TIME_BIN_SCHEMA = pa.schema(
    [
        ("time_bin_id", pa.string(), False),
        ("canonical_index", pa.int64(), False),
        ("start_s", pa.float64(), False),
        ("end_s", pa.float64(), False),
        ("time_axis_id", pa.string(), False),
        ("time_axis_hash", pa.string(), False),
    ]
)

EXPOSURE_REPLICATION_STATUS_SCHEMA = pa.schema(
    [
        ("replication_id", pa.string(), False),
        ("complete", pa.bool_(), False),
        ("catalog_hash", pa.string(), False),
        ("exposure_row_count", pa.int64(), False),
        ("sensor_active_moving_time_s", pa.float64(), False),
        ("in_grid_duration_s", pa.float64(), False),
        ("outside_grid_duration_s", pa.float64(), False),
        ("conservation_residual_s", pa.float64(), False),
    ]
)

OPERATING_REPLICATION_STATUS_SCHEMA = EXPOSURE_REPLICATION_STATUS_SCHEMA
for _field in (
    "sensor_active_stationary_time_s",
    "sensor_active_time_s",
    "excluded_depot_time_s",
    "excluded_offduty_time_s",
):
    OPERATING_REPLICATION_STATUS_SCHEMA = OPERATING_REPLICATION_STATUS_SCHEMA.append(
        pa.field(_field, pa.float64(), nullable=False)
    )

EXPOSURE_VEHICLE_CATALOG_SCHEMA = pa.schema(
    [
        ("fleet_id", pa.string(), False),
        ("vehicle_id", pa.string(), False),
        ("catalog_index", pa.int64(), False),
        ("catalog_hash", pa.string(), False),
    ]
)


def build_time_axis(bin_edges_s: Sequence[float]) -> tuple[TimeAxis, str]:
    edges = tuple(float(value) for value in bin_edges_s)
    if (
        len(edges) < 2
        or any(not math.isfinite(value) for value in edges)
        or any(right <= left for left, right in zip(edges, edges[1:]))
    ):
        raise ValueError("time axis requires finite strictly increasing bin edges")
    payload = {"algorithm": "explicit-time-axis@1", "bin_edges_s": list(edges)}
    axis_id = stable_id("time_axis", payload)
    bins = tuple(
        TimeBin(
            time_bin_id=stable_id("time_bin", {"time_axis_id": axis_id, "canonical_index": index}),
            canonical_index=index,
            start_s=start,
            end_s=end,
        )
        for index, (start, end) in enumerate(zip(edges, edges[1:]))
    )
    axis = TimeAxis(
        time_axis_id=axis_id,
        observation_start_s=edges[0],
        end_s=edges[-1],
        bins=bins,
    )
    return axis, scientific_hash(axis)


def _grid_axis(grid_cells: gpd.GeoDataFrame, *, working_crs: str) -> tuple[GridAxis, str]:
    if grid_cells.crs is None or grid_cells.crs != working_crs:
        raise ValueError("grid CRS must equal the declared working CRS")
    geometry_column = (
        "sensing_geometry" if "sensing_geometry" in grid_cells else grid_cells.geometry.name
    )
    ordered = grid_cells[["cell_id", geometry_column]].sort_values("cell_id", kind="stable")
    grid_axis_hash = scientific_hash(
        {
            "working_crs": working_crs,
            "cells": [
                {
                    "cell_id": str(cell_id),
                    "sensing_geometry_wkb_hex": geometry.wkb_hex,
                }
                for cell_id, geometry in zip(
                    ordered["cell_id"], ordered[geometry_column], strict=True
                )
            ],
        }
    )
    axis = GridAxis(
        grid_axis_id=stable_id("grid_axis", grid_axis_hash),
        cell_ids=tuple(str(value) for value in ordered["cell_id"]),
        working_crs=working_crs,
    )
    return axis, grid_axis_hash


def publish_exposure_artifact(
    *,
    artifact_root: Path,
    simulation: ArtifactRef,
    environment: ArtifactRef,
    road_edges: gpd.GeoDataFrame,
    grid_cells: gpd.GeoDataFrame,
    working_crs: str,
    config: ExposureConfig,
) -> ArtifactRef:
    """Allocate a new grid/bin artifact solely from retained simulation movement."""

    config = ExposureConfig.model_validate(config)
    if environment.artifact_kind != "environment":
        raise ValueError("exposure allocation requires an environment artifact dependency")
    if config.simulation_id != simulation.artifact_id:
        raise ValueError("exposure configuration references a different simulation")
    simulation_reader = SimulationArtifactReader(artifact_root, simulation)
    if config.fleet_movement_kinds and set(config.fleet_movement_kinds) != {
        key.fleet_id for key in simulation_reader.vehicle_keys
    }:
        raise ValueError("Fleet sensing categories must cover the exact physical catalog fleets")
    simulation_environment = next(
        (
            dependency
            for dependency in simulation_reader.artifact.manifest.dependencies
            if dependency.role == "environment"
        ),
        None,
    )
    if (
        simulation_environment is None
        or simulation_environment.artifact_id != environment.artifact_id
        or simulation_environment.content_hash != environment.content_hash
    ):
        raise ValueError("exposure environment must equal the simulation environment dependency")
    movements = simulation_reader.read_movements()
    time_axis, time_axis_hash = build_time_axis(config.bin_edges_s)
    used_edge_ids = tuple(sorted({str(value) for value in movements["edge_id"]}))
    available_edge_ids = {str(value) for value in road_edges["edge_id"]}
    missing_edge_ids = tuple(sorted(set(used_edge_ids) - available_edge_ids))
    if missing_edge_ids:
        raise ValueError(
            "simulation movements reference edges absent from the prepared environment: "
            f"{missing_edge_ids[:5]!r}"
        )
    sensing_grid_cells = road_intersecting_grid_cells(road_edges, grid_cells)
    used_edges = road_edges.loc[road_edges["edge_id"].astype(str).isin(used_edge_ids)]
    pieces = build_edge_grid_pieces(used_edges, sensing_grid_cells) if used_edge_ids else ()
    grid_axis, grid_axis_hash = _grid_axis(sensing_grid_cells, working_crs=working_crs)
    allocation = allocate_movement_exposure(
        movements,
        pieces,
        grid_axis=grid_axis,
        time_axis=time_axis,
        active_movement_kinds=config.active_movement_kinds,
        fleet_movement_kinds=config.fleet_movement_kinds,
    )
    if config.measurement == "operating_duration":
        if config.activity_source_ref is None:
            raise ValueError(
                "Operating exposure requires retained resolved location/catalog inputs"
            )
        source = config.activity_source_ref
        if not any(
            d.role == "scenario_validation"
            and d.artifact_id == source.artifact_id
            and d.content_hash == source.content_hash
            for d in simulation_reader.artifact.manifest.dependencies
        ):
            raise ValueError(
                "Operating locations must be the simulation's exact retained input dependency"
            )
        if not set(config.operating_fleet_ids) <= {
            v.fleet_id for v in simulation_reader.vehicle_keys
        }:
            raise ValueError("Operating exposure references a fleet outside the physical catalog")
        from mobile_sensing.exposure.operating import stationary_context, add_stationary_exposure

        locations, location_cells, depots = stationary_context(
            artifact_root,
            config.activity_source_ref,
            sensing_grid_cells,
            simulation_reader.read_stationary_locations(),
        )
        allocation = add_stationary_exposure(
            allocation,
            simulation_reader.read_activities(),
            time_axis=time_axis,
            locations=locations,
            location_cells=location_cells,
            depot_nodes=depots,
            operating_fleets=config.operating_fleet_ids,
            idle_break_seconds=config.fleet_idle_break_seconds,
        )
    replication_ids = simulation_reader.replication_ids
    replication_axis = (PartitionAxis(name="replication_id", values=replication_ids),)
    exposure_partitions = {(replication_id,): [] for replication_id in replication_ids}
    for row in allocation.rows:
        exposure_partitions[(row.replication_id,)].append(
            {
                "replication_id": row.replication_id,
                "fleet_id": row.vehicle.fleet_id,
                "vehicle_id": row.vehicle.vehicle_id,
                "cell_id": row.cell_id,
                "time_bin_id": row.time_bin_id,
                "duration_s": row.duration_s,
            }
        )
    diagnostic_by_replication: dict[str, list] = defaultdict(list)
    for row in allocation.diagnostics:
        diagnostic_by_replication[row.replication_id].append(row)
    catalog_hash = simulation_reader.artifact.manifest.scientific_identity.catalog_hash
    replication_set_hash = (
        simulation_reader.artifact.manifest.scientific_identity.replication_set_hash
    )
    assert catalog_hash is not None and replication_set_hash is not None
    status_partitions = {}
    for replication_id in replication_ids:
        diagnostics = diagnostic_by_replication[replication_id]
        exposure_count = len(exposure_partitions[(replication_id,)])
        status_partitions[(replication_id,)] = [
            {
                "replication_id": replication_id,
                "complete": True,
                "catalog_hash": catalog_hash,
                "exposure_row_count": exposure_count,
                "sensor_active_moving_time_s": math.fsum(
                    row.active_moving_time_s for row in diagnostics
                ),
                "in_grid_duration_s": math.fsum(row.in_grid_duration_s for row in diagnostics),
                "outside_grid_duration_s": math.fsum(
                    row.outside_grid_duration_s for row in diagnostics
                ),
                "conservation_residual_s": math.fsum(
                    row.conservation_residual_s for row in diagnostics
                ),
            }
        ]
    if config.measurement == "operating_duration":
        for (replication_id,), records in status_partitions.items():
            diagnostics = diagnostic_by_replication[replication_id]
            records[0].update(
                {
                    "sensor_active_stationary_time_s": math.fsum(
                        d.stationary_time_s for d in diagnostics
                    ),
                    "sensor_active_time_s": math.fsum(
                        d.active_moving_time_s + d.stationary_time_s for d in diagnostics
                    ),
                    "excluded_depot_time_s": math.fsum(
                        d.excluded_depot_time_s for d in diagnostics
                    ),
                    "excluded_offduty_time_s": math.fsum(
                        d.excluded_offduty_time_s for d in diagnostics
                    ),
                }
            )
    edge_rows = {
        (): [
            {
                "edge_id": row.edge_id,
                "piece_index": row.piece_index,
                "cell_id": row.cell_id,
                "start_fraction": row.start_fraction,
                "end_fraction": row.end_fraction,
            }
            for row in pieces
        ]
    }
    grid_rows = {
        (): [
            {
                "cell_id": cell_id,
                "canonical_index": index,
                "grid_axis_id": grid_axis.grid_axis_id,
                "grid_axis_hash": grid_axis_hash,
                "working_crs": working_crs,
            }
            for index, cell_id in enumerate(grid_axis.cell_ids)
        ]
    }
    time_rows = {
        (): [
            {
                "time_bin_id": row.time_bin_id,
                "canonical_index": row.canonical_index,
                "start_s": row.start_s,
                "end_s": row.end_s,
                "time_axis_id": time_axis.time_axis_id,
                "time_axis_hash": time_axis_hash,
            }
            for row in time_axis.bins
        ]
    }
    catalog_rows = {
        (): [
            {
                "fleet_id": key.fleet_id,
                "vehicle_id": key.vehicle_id,
                "catalog_index": index,
                "catalog_hash": catalog_hash,
            }
            for index, key in enumerate(simulation_reader.vehicle_keys)
        ]
    }
    tables = (
        PartitionedTableData(
            "edge_grid_pieces",
            EDGE_GRID_SCHEMA,
            (),
            edge_rows,
            ("edge_id", "piece_index"),
        ),
        PartitionedTableData(
            "exposure",
            EXPOSURE_SCHEMA,
            replication_axis,
            exposure_partitions,
            ("replication_id", "fleet_id", "vehicle_id", "cell_id", "time_bin_id"),
        ),
        PartitionedTableData(
            "grid_axis",
            GRID_AXIS_SCHEMA,
            (),
            grid_rows,
            ("cell_id",),
        ),
        PartitionedTableData(
            "replication_status",
            (
                OPERATING_REPLICATION_STATUS_SCHEMA
                if config.measurement == "operating_duration"
                else EXPOSURE_REPLICATION_STATUS_SCHEMA
            ),
            replication_axis,
            status_partitions,
            ("replication_id",),
        ),
        PartitionedTableData(
            "time_bins",
            TIME_BIN_SCHEMA,
            (),
            time_rows,
            ("time_bin_id",),
        ),
        PartitionedTableData(
            "vehicle_catalog",
            EXPOSURE_VEHICLE_CATALOG_SCHEMA,
            (),
            catalog_rows,
            ("fleet_id", "vehicle_id"),
        ),
    )
    dependencies = (
        ArtifactDependency(
            role="environment",
            artifact_id=environment.artifact_id,
            content_hash=environment.content_hash,
        ),
        ArtifactDependency(
            role="simulation",
            artifact_id=simulation.artifact_id,
            content_hash=simulation.content_hash,
        ),
    )
    if config.activity_source_ref is not None:
        dependencies += (
            ArtifactDependency(
                role="activity_locations",
                artifact_id=config.activity_source_ref.artifact_id,
                content_hash=config.activity_source_ref.content_hash,
            ),
        )
    resolved_config = {
        "exposure": scientific_projection(config),
        "simulation_storage_version": SIMULATION_STORAGE_VERSION,
        "grid_domain": {
            "algorithm": ROAD_GRID_DOMAIN_VERSION,
            "definition": "prepared cells with a positive-length sensing-road intersection",
            "prepared_cell_count": len(grid_cells),
            "eligible_cell_count": len(sensing_grid_cells),
        },
        "edge_grid_piece_hash": scientific_hash(edge_rows[()]),
        "sparse_exposure_hash": scientific_hash(
            [row.model_dump(mode="json") for row in allocation.rows]
        ),
    }
    identity = ScientificIdentity(
        schema_version="2.0",
        artifact_kind="exposure",
        resolved_config=resolved_config,
        resolved_config_hash=scientific_hash(resolved_config),
        dependency_hashes={item.role: item.content_hash for item in dependencies},
        algorithm_versions={
            "edge_grid": EDGE_GRID_ALGORITHM_VERSION,
            "grid_domain": ROAD_GRID_DOMAIN_VERSION,
            "allocation": (
                "operating-activity-exposure@2"
                if config.measurement == "operating_duration"
                else EXPOSURE_ALLOCATION_VERSION
            ),
            "storage": EXPOSURE_STORAGE_VERSION,
        },
        catalog_hash=catalog_hash,
        replication_set_hash=replication_set_hash,
        time_axis_hash=time_axis_hash,
        grid_axis_hash=grid_axis_hash,
    )
    return publish_partitioned_artifact(
        artifact_root=artifact_root,
        collection="exposures",
        identity=identity,
        dependencies=dependencies,
        tables=tables,
    ).reference


def replication_status_schema(artifact):
    """Select the persisted version without reinterpreting historical exposure."""
    operating = (
        artifact.manifest.scientific_identity.resolved_config["exposure"].get("measurement")
        == "operating_duration"
    )
    return OPERATING_REPLICATION_STATUS_SCHEMA if operating else EXPOSURE_REPLICATION_STATUS_SCHEMA


class ExposureArtifactReader:
    """Schema-complete sparse reader that certifies zeros only after verification."""

    def __init__(
        self, artifact_root: Path, *, chunk_rows: int = DEFAULT_EXPOSURE_CHUNK_ROWS
    ) -> None:
        if isinstance(chunk_rows, bool) or chunk_rows <= 0:
            raise ValueError("exposure chunk_rows must be a positive integer")
        self.artifact_root = Path(artifact_root)
        self.chunk_rows = chunk_rows

    def _open(self, reference: ArtifactRef):
        if reference.artifact_kind != "exposure":
            raise ValueError("exposure reader requires an exposure artifact reference")
        artifact = verify_partitioned_artifact(
            artifact_root=self.artifact_root,
            collection="exposures",
            reference=reference,
        )
        status = read_partitioned_table(
            artifact,
            table_name="replication_status",
            schema=replication_status_schema(artifact),
        ).to_pylist()
        if not status or not all(row["complete"] for row in status):
            raise ValueError("exposure artifact lacks a complete replication set")
        catalog = read_partitioned_table(
            artifact,
            table_name="vehicle_catalog",
            schema=EXPOSURE_VEHICLE_CATALOG_SCHEMA,
        ).to_pylist()
        grid = read_partitioned_table(
            artifact, table_name="grid_axis", schema=GRID_AXIS_SCHEMA
        ).to_pylist()
        bins = read_partitioned_table(
            artifact, table_name="time_bins", schema=TIME_BIN_SCHEMA
        ).to_pylist()
        return artifact, status, catalog, grid, bins

    def axes(self, exposure: ArtifactRef):
        artifact, status, catalog, grid, bins = self._open(exposure)
        return {
            "artifact": artifact,
            "replication_ids": tuple(sorted(row["replication_id"] for row in status)),
            "vehicle_keys": tuple(
                VehicleKey(fleet_id=row["fleet_id"], vehicle_id=row["vehicle_id"])
                for row in catalog
            ),
            "cell_ids": tuple(
                row["cell_id"] for row in sorted(grid, key=lambda row: row["canonical_index"])
            ),
            "time_bin_ids": tuple(
                row["time_bin_id"] for row in sorted(bins, key=lambda row: row["canonical_index"])
            ),
        }

    def read_sparse_batches(
        self,
        exposure: ArtifactRef,
        *,
        replication_ids: Sequence[str],
        vehicle_keys: Sequence[VehicleKey],
    ) -> tuple[ExposureReadMetadata, Iterator[pa.RecordBatch]]:
        """Verified bounded Arrow batches for internal numerical consumers."""
        axes = self.axes(exposure)
        artifact = axes["artifact"]
        selected_replications = tuple(sorted(set(replication_ids)))
        selected_vehicles = tuple(
            sorted(set(vehicle_keys), key=lambda key: (key.fleet_id, key.vehicle_id))
        )
        if not selected_replications or not set(selected_replications) <= set(
            axes["replication_ids"]
        ):
            raise ValueError("requested replications are empty or outside the complete set")
        available_keys = {(key.fleet_id, key.vehicle_id) for key in axes["vehicle_keys"]}
        if (
            not selected_vehicles
            or not {(key.fleet_id, key.vehicle_id) for key in selected_vehicles} <= available_keys
        ):
            raise ValueError("requested vehicles are empty or outside the catalog")
        exposure_manifest = next(
            table for table in artifact.manifest.tables if table.name == "exposure"
        )
        axis_values = tuple(exposure_manifest.partition_axes[0].values)
        partition_indices = [axis_values.index(value) for value in selected_replications]
        identity = artifact.manifest.scientific_identity
        assert identity.catalog_hash is not None
        assert identity.replication_set_hash is not None
        assert identity.grid_axis_hash is not None
        assert identity.time_axis_hash is not None
        metadata = ExposureReadMetadata(
            exposure_id=exposure.artifact_id,
            catalog_hash=identity.catalog_hash,
            replication_set_hash=identity.replication_set_hash,
            grid_axis_hash=identity.grid_axis_hash,
            time_axis_hash=identity.time_axis_hash,
            partition_manifest_hash=scientific_hash(exposure_manifest),
            replication_ids=selected_replications,
            vehicle_keys=selected_vehicles,
        )

        def batches() -> Iterator[pa.RecordBatch]:
            predicate = None
            for key in selected_vehicles:
                vehicle_predicate = (ds.field("fleet_id") == key.fleet_id) & (
                    ds.field("vehicle_id") == key.vehicle_id
                )
                predicate = (
                    vehicle_predicate if predicate is None else predicate | vehicle_predicate
                )
            table_directory = artifact.directory / exposure_manifest.relative_path
            for partition_index in partition_indices:
                if exposure_manifest.partitions[partition_index].row_count == 0:
                    continue
                dataset = ds.dataset(
                    str(partition_file(table_directory, partition_index)),
                    format="parquet",
                    schema=EXPOSURE_SCHEMA,
                )
                for batch in dataset.scanner(
                    filter=predicate,
                    batch_size=self.chunk_rows,
                ).to_batches():
                    if not batch.num_rows:
                        continue
                    if any(column.null_count for column in batch.columns):
                        raise ValueError("Sparse exposure contains null values")
                    duration = batch.column("duration_s")
                    if not pc.all(pc.and_(pc.is_finite(duration), pc.greater(duration, 0))).as_py():
                        raise ValueError("Sparse exposure requires finite positive durations")
                    for name, values in (
                        ("replication_id", selected_replications),
                        ("cell_id", axes["cell_ids"]),
                        ("time_bin_id", axes["time_bin_ids"]),
                    ):
                        if not pc.all(
                            pc.is_in(batch.column(name), value_set=pa.array(values))
                        ).as_py():
                            raise ValueError("Sparse exposure row is outside its certified axes")
                    yield batch

        return metadata, batches()

    def read_sparse_chunks(
        self,
        exposure: ArtifactRef,
        *,
        replication_ids: Sequence[str],
        vehicle_keys: Sequence[VehicleKey],
    ) -> tuple[ExposureReadMetadata, Iterator[SparseExposureChunk]]:
        metadata, batches = self.read_sparse_batches(
            exposure, replication_ids=replication_ids, vehicle_keys=vehicle_keys
        )

        def chunks():
            for chunk_index, batch in enumerate(batches):
                rows = tuple(
                    SparseExposureRow(
                        replication_id=row["replication_id"],
                        vehicle=VehicleKey(fleet_id=row["fleet_id"], vehicle_id=row["vehicle_id"]),
                        cell_id=row["cell_id"],
                        time_bin_id=row["time_bin_id"],
                        duration_s=row["duration_s"],
                    )
                    for row in batch.to_pylist()
                )
                yield SparseExposureChunk(
                    scope_hash=metadata.scope_hash, chunk_index=chunk_index, rows=rows
                )

        return metadata, chunks()


class ExposureSensingQueryService:
    """Bounded sparse vehicle, fleet, and all-vehicle realization/statistic queries."""

    def __init__(
        self, artifact_root: Path, *, max_nonzero_rows: int = DEFAULT_MATRIX_NONZERO_LIMIT
    ) -> None:
        if isinstance(max_nonzero_rows, bool) or max_nonzero_rows <= 0:
            raise ValueError("matrix nonzero-row limit must be positive")
        self.reader = ExposureArtifactReader(artifact_root)
        self.max_nonzero_rows = max_nonzero_rows

    def query(self, exposure: ArtifactRef, request: SensingMatrixQuery) -> SensingMatrixSlice:
        axes = self.reader.axes(exposure)
        requested_replications = frozenset(request.replication_ids)
        requested_vehicles = frozenset(
            (key.fleet_id, key.vehicle_id) for key in request.vehicle_keys
        )
        requested_cells = frozenset(request.cell_ids)
        requested_bins = frozenset(request.time_bin_ids)
        replications = tuple(
            value
            for value in axes["replication_ids"]
            if not requested_replications or value in requested_replications
        )
        vehicles = tuple(
            key
            for key in axes["vehicle_keys"]
            if not requested_vehicles or (key.fleet_id, key.vehicle_id) in requested_vehicles
        )
        cells = tuple(
            value for value in axes["cell_ids"] if not requested_cells or value in requested_cells
        )
        bins = tuple(
            value for value in axes["time_bin_ids"] if not requested_bins or value in requested_bins
        )
        if requested_replications and len(replications) != len(requested_replications):
            raise ValueError("matrix query replication axis is outside the exposure artifact")
        if requested_vehicles and len(vehicles) != len(requested_vehicles):
            raise ValueError("matrix query vehicle axis is outside the exposure artifact")
        if not set(cells) <= set(axes["cell_ids"]) or not set(bins) <= set(axes["time_bin_ids"]):
            raise ValueError("matrix query cell/time axes are outside the exposure artifact")
        if requested_cells and len(cells) != len(requested_cells):
            raise ValueError("matrix query cell axis is outside the exposure artifact")
        if requested_bins and len(bins) != len(requested_bins):
            raise ValueError("matrix query time axis is outside the exposure artifact")
        _, chunks = self.reader.read_sparse_chunks(
            exposure,
            replication_ids=replications,
            vehicle_keys=vehicles,
        )
        selected_cells = frozenset(cells)
        selected_bins = frozenset(bins)
        realization: dict[tuple[str, str, str], list[float]] = defaultdict(list)
        for chunk in chunks:
            for row in chunk.rows:
                if row.cell_id in selected_cells and row.time_bin_id in selected_bins:
                    realization[(row.replication_id, row.cell_id, row.time_bin_id)].append(
                        row.duration_s
                    )
                    accumulator_limit = self.max_nonzero_rows * (
                        1 if request.statistic == "raw" else len(replications)
                    )
                    if len(realization) > accumulator_limit:
                        raise ValueError(
                            "matrix query exceeds the nonzero-row limit while accumulating; "
                            "narrow the scope or export"
                        )
        summed = {key: math.fsum(values) for key, values in realization.items()}
        values = []
        if request.statistic == "raw":
            values = [
                SensingMatrixValue(
                    replication_id=key[0],
                    cell_id=key[1],
                    time_bin_id=key[2],
                    value=value,
                )
                for key, value in sorted(summed.items())
                if value > 0.0
            ]
        else:
            if request.statistic in {"sample_variance", "sample_std"} and len(replications) < 2:
                raise ValueError("sample variance/std requires at least two complete replications")
            for cell_id in cells:
                for time_bin_id in bins:
                    observations = [
                        summed.get((replication_id, cell_id, time_bin_id), 0.0)
                        for replication_id in replications
                    ]
                    mean = math.fsum(observations) / len(observations)
                    if request.statistic == "mean":
                        value = mean
                    else:
                        variance = math.fsum((item - mean) ** 2 for item in observations) / (
                            len(observations) - 1
                        )
                        value = (
                            variance
                            if request.statistic == "sample_variance"
                            else math.sqrt(variance)
                        )
                    if value > 0.0:
                        values.append(
                            SensingMatrixValue(
                                cell_id=cell_id,
                                time_bin_id=time_bin_id,
                                value=value,
                            )
                        )
        if len(values) > self.max_nonzero_rows:
            raise ValueError(
                "matrix query exceeds the nonzero-row limit; narrow the scope or export"
            )
        identity = axes["artifact"].manifest.scientific_identity
        assert identity.grid_axis_hash is not None and identity.time_axis_hash is not None
        values.sort(
            key=lambda value: (
                value.replication_id or "",
                value.cell_id,
                value.time_bin_id,
            )
        )
        return SensingMatrixSlice(
            exposure_id=exposure.artifact_id,
            grid_axis_hash=identity.grid_axis_hash,
            time_axis_hash=identity.time_axis_hash,
            statistic=request.statistic,
            replication_ids=tuple(replications),
            vehicle_keys=tuple(vehicles),
            cell_ids=tuple(cells),
            time_bin_ids=tuple(bins),
            replication_count=len(replications),
            expected_shape=(
                (len(replications), len(cells), len(bins))
                if request.statistic == "raw"
                else (len(cells), len(bins))
            ),
            unit="s^2" if request.statistic == "sample_variance" else "s",
            values=tuple(values),
        )
