from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import LineString, box

from mobile_sensing.artifacts import partition_file, read_partitioned_table
from mobile_sensing.contracts import (
    ArtifactDependency,
    ArtifactRef,
    CatalogIdentity,
    ExposureConfig,
    GridAxis,
    JointReplicationIdentity,
    MovementInterval,
    SeedManifest,
    SensingMatrixQuery,
    StationaryInterval,
    VehicleKey,
    VehicleState,
    VehicleStatus,
    scientific_hash,
    stable_id,
)
from mobile_sensing.exposure import (
    ExposureArtifactReader,
    ExposureSensingQueryService,
    allocate_movement_exposure,
    build_edge_grid_pieces,
    build_time_axis,
    coarsen_sparse_exposure,
    publish_exposure_artifact,
    road_intersecting_grid_cells,
)
from mobile_sensing.simulation import (
    ExecutionLifecycleStatus,
    ExecutionOutcome,
    IdleActivityInterval,
    KernelResult,
    TaskLifecycleStatus,
    TaskOutcome,
    VehicleOutcome,
    publish_simulation_results,
)
from mobile_sensing.simulation.storage import ACTIVITY_SCHEMA, MOVEMENT_SCHEMA


FIXTURE = Path(__file__).parent / "fixtures" / "m06" / "canonical_exposure.json"
CRS = "EPSG:2056"


def _cells() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {
            "cell_id": ["a", "b"],
            "sensing_geometry": [box(0, 0, 10, 10), box(10, 0, 20, 10)],
        },
        geometry="sensing_geometry",
        crs=CRS,
    )


def _edges(records: list[tuple[str, LineString]]) -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {
            "edge_id": [edge_id for edge_id, _ in records],
            "length_m": [geometry.length for _, geometry in records],
            "geometry": [geometry for _, geometry in records],
        },
        geometry="geometry",
        crs=CRS,
    )


def _movement_frame(rows: list[dict]) -> pd.DataFrame:
    columns = [
        "replication_id",
        "fleet_id",
        "vehicle_id",
        "movement_index",
        "start_s",
        "end_s",
        "movement_kind",
        "edge_id",
        "edge_start_fraction",
        "edge_end_fraction",
    ]
    return pd.DataFrame(rows, columns=columns)


def _row(
    vehicle_id: str,
    *,
    start_s: float,
    end_s: float,
    start_fraction: float = 0.0,
    end_fraction: float = 1.0,
    movement_kind: str = "service_pickup",
) -> dict:
    return {
        "replication_id": "r1",
        "fleet_id": "fleet",
        "vehicle_id": vehicle_id,
        "movement_index": 0,
        "start_s": start_s,
        "end_s": end_s,
        "movement_kind": movement_kind,
        "edge_id": "cross",
        "edge_start_fraction": start_fraction,
        "edge_end_fraction": end_fraction,
    }


def _projection(rows) -> list[tuple[str, str, str, str, str, float]]:
    return [
        (
            row.replication_id,
            row.vehicle.fleet_id,
            row.vehicle.vehicle_id,
            row.cell_id,
            row.time_bin_id,
            row.duration_s,
        )
        for row in rows
    ]


def test_ordered_edge_grid_pieces_cover_crossings_repeats_and_shared_boundaries() -> None:
    edges = _edges(
        [
            ("cross", LineString([(-10, 5), (30, 5)])),
            ("boundary", LineString([(10, 0), (10, 10)])),
            ("repeat", LineString([(-5, 2), (5, 2), (-5, 8), (5, 8)])),
        ]
    )
    pieces = build_edge_grid_pieces(edges, _cells())
    cross = [row for row in pieces if row.edge_id == "cross"]
    assert [(row.cell_id, row.start_fraction, row.end_fraction) for row in cross] == [
        (None, 0.0, 0.25),
        ("a", 0.25, 0.5),
        ("b", 0.5, 0.75),
        (None, 0.75, 1.0),
    ]
    boundary = [row for row in pieces if row.edge_id == "boundary"]
    assert [(row.cell_id, row.start_fraction, row.end_fraction) for row in boundary] == [
        ("a", 0.0, 1.0)
    ]
    repeat = [row for row in pieces if row.edge_id == "repeat"]
    assert sum(row.cell_id == "a" for row in repeat) == 2
    assert [row.cell_id for row in repeat] == [None, "a", None, "a"]
    shuffled = build_edge_grid_pieces(
        edges.sample(frac=1.0, random_state=9).reset_index(drop=True),
        _cells().iloc[::-1].reset_index(drop=True),
    )
    assert shuffled == pieces


def test_road_grid_domain_requires_positive_length_and_is_canonical() -> None:
    cells = gpd.GeoDataFrame(
        {
            "cell_id": ["c", "b", "a"],
            "sensing_geometry": [
                box(20, 0, 30, 10),
                box(10, 0, 20, 10),
                box(0, 0, 10, 10),
            ],
        },
        geometry="sensing_geometry",
        crs=CRS,
    )
    # The road crosses a and only touches b at its endpoint; c is disjoint.
    selected = road_intersecting_grid_cells(
        _edges([("road", LineString([(-5, 5), (10, 5)]))]), cells, batch_size=1
    )
    assert tuple(selected.cell_id) == ("a",)
    # A road along a shared boundary has positive-length intersections with both cells.
    boundary = road_intersecting_grid_cells(
        _edges([("boundary", LineString([(10, 0), (10, 10)]))]), cells
    )
    assert tuple(boundary.cell_id) == ("a", "b")


def test_published_exposure_axis_excludes_roadless_grid_cells(tmp_path: Path) -> None:
    _, simulation, _, environment, road, grid, config = _artifact_setup(tmp_path)
    expanded = gpd.GeoDataFrame(
        {
            "cell_id": ["a", "roadless"],
            "sensing_geometry": [grid.sensing_geometry.iloc[0], box(20, 0, 30, 10)],
        },
        geometry="sensing_geometry",
        crs=CRS,
    )
    exposure = publish_exposure_artifact(
        artifact_root=tmp_path,
        simulation=simulation,
        environment=environment,
        road_edges=road,
        grid_cells=expanded,
        working_crs=CRS,
        config=config,
    )
    axes = ExposureArtifactReader(tmp_path).axes(exposure)
    assert axes["cell_ids"] == ("a",)
    resolved = axes["artifact"].manifest.scientific_identity.resolved_config
    assert resolved["grid_domain"] == {
        "algorithm": "positive-length-road-grid@1",
        "definition": "prepared cells with a positive-length sensing-road intersection",
        "prepared_cell_count": 2,
        "eligible_cell_count": 1,
    }


def test_exact_allocation_conserves_full_partial_and_outside_movements() -> None:
    pieces = build_edge_grid_pieces(_edges([("cross", LineString([(-10, 5), (30, 5)]))]), _cells())
    time_axis, _ = build_time_axis([0, 10, 20, 30, 40])
    grid_axis = GridAxis(grid_axis_id="grid", cell_ids=("a", "b"), working_crs=CRS)
    movements = _movement_frame(
        [
            _row("full", start_s=0, end_s=40),
            _row(
                "partial",
                start_s=5,
                end_s=25,
                start_fraction=0.125,
                end_fraction=0.625,
            ),
            _row("masked", start_s=30, end_s=35, movement_kind="reposition"),
        ]
    )
    result = allocate_movement_exposure(
        movements,
        pieces,
        grid_axis=grid_axis,
        time_axis=time_axis,
        active_movement_kinds=("service_pickup",),
    )
    durations = {
        (row.vehicle.vehicle_id, row.cell_id, row.time_bin_id): row.duration_s
        for row in result.rows
    }
    bins = [row.time_bin_id for row in time_axis.bins]
    assert durations == {
        ("full", "a", bins[1]): 10.0,
        ("full", "b", bins[2]): 10.0,
        ("partial", "a", bins[1]): 10.0,
        ("partial", "b", bins[2]): 5.0,
    }
    diagnostics = {row.vehicle.vehicle_id: row for row in result.diagnostics}
    assert (
        diagnostics["full"].active_moving_time_s,
        diagnostics["full"].in_grid_duration_s,
        diagnostics["full"].outside_grid_duration_s,
    ) == (40.0, 20.0, 20.0)
    assert (
        diagnostics["partial"].active_moving_time_s,
        diagnostics["partial"].in_grid_duration_s,
        diagnostics["partial"].outside_grid_duration_s,
    ) == (20.0, 15.0, 5.0)
    assert diagnostics["masked"].active_moving_time_s == 0.0
    assert all(abs(row.conservation_residual_s) <= 1e-12 for row in result.diagnostics)
    for vehicle_id in ("full", "partial", "masked"):
        for time_bin_id in bins:
            assert (
                math.fsum(
                    value
                    for (vehicle, _, bin_id), value in durations.items()
                    if vehicle == vehicle_id and bin_id == time_bin_id
                )
                <= 10.0
            )
    empty = allocate_movement_exposure(
        _movement_frame([]),
        pieces,
        grid_axis=grid_axis,
        time_axis=time_axis,
        active_movement_kinds=("service_pickup",),
    )
    assert empty.rows == () and empty.diagnostics == ()


def test_aligned_coarsening_matches_direct_allocation_and_unaligned_bins_reallocate() -> None:
    movement = _movement_frame([_row("v", start_s=0, end_s=40)])
    fine_grid = _cells()
    edge = _edges([("cross", LineString([(-10, 5), (30, 5)]))])
    fine_axis = GridAxis(grid_axis_id="fine", cell_ids=("a", "b"), working_crs=CRS)
    fine_time, _ = build_time_axis([0, 10, 20, 30, 40])
    fine = allocate_movement_exposure(
        movement,
        build_edge_grid_pieces(edge, fine_grid),
        grid_axis=fine_axis,
        time_axis=fine_time,
        active_movement_kinds=("service_pickup",),
    )

    coarse_grid = gpd.GeoDataFrame(
        {"cell_id": ["ab"], "sensing_geometry": [box(0, 0, 20, 10)]},
        geometry="sensing_geometry",
        crs=CRS,
    )
    coarse_axis = GridAxis(grid_axis_id="coarse", cell_ids=("ab",), working_crs=CRS)
    coarse_time, _ = build_time_axis([0, 20, 40])
    direct = allocate_movement_exposure(
        movement,
        build_edge_grid_pieces(edge, coarse_grid),
        grid_axis=coarse_axis,
        time_axis=coarse_time,
        active_movement_kinds=("service_pickup",),
    )
    coarsened = coarsen_sparse_exposure(
        fine.rows,
        cell_mapping={"a": "ab", "b": "ab"},
        time_bin_mapping={
            fine_time.bins[0].time_bin_id: coarse_time.bins[0].time_bin_id,
            fine_time.bins[1].time_bin_id: coarse_time.bins[0].time_bin_id,
            fine_time.bins[2].time_bin_id: coarse_time.bins[1].time_bin_id,
            fine_time.bins[3].time_bin_id: coarse_time.bins[1].time_bin_id,
        },
    )
    assert _projection(coarsened) == _projection(direct.rows)

    arbitrary_time, _ = build_time_axis([0, 15, 40])
    arbitrary = allocate_movement_exposure(
        movement,
        build_edge_grid_pieces(edge, fine_grid),
        grid_axis=fine_axis,
        time_axis=arbitrary_time,
        active_movement_kinds=("service_pickup",),
    )
    assert sorted(row.duration_s for row in arbitrary.rows) == [5.0, 5.0, 10.0]


def _catalog() -> CatalogIdentity:
    keys = (
        VehicleKey(fleet_id="fleet_a", vehicle_id="v1"),
        VehicleKey(fleet_id="fleet_b", vehicle_id="v2"),
    )
    metadata_hash = scientific_hash({"fixture": "m06-vehicles"})
    catalog_hash = scientific_hash(
        {
            "vehicle_keys": [[key.fleet_id, key.vehicle_id] for key in keys],
            "physical_metadata_hash": metadata_hash,
        }
    )
    return CatalogIdentity(
        catalog_id=stable_id("catalog", catalog_hash),
        vehicle_keys=keys,
        physical_metadata_hash=metadata_hash,
        catalog_hash=catalog_hash,
    )


def _state(key: VehicleKey) -> VehicleState:
    return VehicleState(
        key=key,
        status=VehicleStatus.IDLE,
        current_execution_id=None,
        committed_location_id="node",
        available_at_s=20.0,
        remaining_capacity=None,
        execution_generation=1,
    )


def _kernel_result(replication_id: str, mover: VehicleKey | None) -> KernelResult:
    catalog = _catalog()
    executions = ()
    tasks = ()
    idle = []
    if mover is not None:
        execution_id = f"execution_{replication_id}"
        task_id = f"task_{replication_id}"
        executions = (
            ExecutionOutcome(
                execution_id=execution_id,
                vehicle=mover,
                fleet_id=mover.fleet_id,
                task_id=task_id,
                execution_generation=1,
                assigned_at_s=0.0,
                planned_end_s=12.0,
                realized_end_s=12.0,
                status=ExecutionLifecycleStatus.COMPLETED,
                realized_intervals=(
                    MovementInterval(
                        kind="movement",
                        start_s=0.0,
                        end_s=10.0,
                        movement_kind="service_pickup",
                        edge_id="inside",
                        edge_start_fraction=0.0,
                        edge_end_fraction=1.0,
                    ),
                    StationaryInterval(
                        kind="service",
                        start_s=10.0,
                        end_s=12.0,
                        location_id="node",
                        step_index=1,
                    ),
                ),
            ),
        )
        tasks = (
            TaskOutcome(
                fleet_id=mover.fleet_id,
                task_id=task_id,
                kind="service",
                source_policy=None,
                release_s=0.0,
                status=TaskLifecycleStatus.COMPLETED,
                assigned_at_s=0.0,
                terminal_time_s=12.0,
                vehicle_id=mover.vehicle_id,
                execution_id=execution_id,
            ),
        )
    for key in catalog.vehicle_keys:
        if key == mover:
            idle.append(
                IdleActivityInterval(vehicle=key, start_s=12.0, end_s=20.0, location_id="node")
            )
        else:
            idle.append(
                IdleActivityInterval(vehicle=key, start_s=0.0, end_s=20.0, location_id="node")
            )
    return KernelResult(
        replication_id=replication_id,
        simulation_start_s=0.0,
        end_s=20.0,
        task_outcomes=tasks,
        vehicle_outcomes=tuple(
            VehicleOutcome(vehicle=key, state=_state(key), entered_at_s=0.0, exited_at_s=None)
            for key in catalog.vehicle_keys
        ),
        execution_outcomes=executions,
        idle_activity_intervals=tuple(idle),
        lifecycle_events=(),
        processed_heap_events=0,
        maximum_queue_size=0,
    )


def _artifact_setup(tmp_path: Path):
    catalog = _catalog()
    results = (
        _kernel_result("r1", catalog.vehicle_keys[0]),
        _kernel_result("r2", catalog.vehicle_keys[1]),
        _kernel_result("r3", None),
    )
    replications = tuple(
        JointReplicationIdentity(
            replication_id=replication_id,
            joint_scenario_id=f"joint_{replication_id}",
            scenario_realization_hash=scientific_hash({"replication": replication_id}),
            catalog_hash=catalog.catalog_hash,
        )
        for replication_id in ("r1", "r2", "r3")
    )
    environment = ArtifactRef(
        artifact_id="environment_m06",
        artifact_kind="environment",
        content_hash=scientific_hash({"environment": "m06"}),
    )
    simulation = publish_simulation_results(
        artifact_root=tmp_path,
        catalog=catalog,
        replications=replications,
        results=results,
        seed_manifests={
            replication_id: SeedManifest(master_seed=index, streams=())
            for index, replication_id in enumerate(("r1", "r2", "r3"), start=1)
        },
        resolved_config={"fixture": "m06"},
        dependencies=(
            ArtifactDependency(
                role="environment",
                artifact_id=environment.artifact_id,
                content_hash=environment.content_hash,
            ),
        ),
    )
    road = _edges([("inside", LineString([(0, 5), (10, 5)]))])
    grid = gpd.GeoDataFrame(
        {"cell_id": ["a"], "sensing_geometry": [box(0, 0, 10, 10)]},
        geometry="sensing_geometry",
        crs=CRS,
    )
    config = ExposureConfig(
        schema_version="2.0",
        simulation_id=simulation.artifact_id,
        sensing_geometry_id="grid_m06",
        bin_edges_s=(0.0, 10.0, 20.0),
        active_movement_kinds=("service_pickup",),
    )
    exposure = publish_exposure_artifact(
        artifact_root=tmp_path,
        simulation=simulation,
        environment=environment,
        road_edges=road,
        grid_cells=grid,
        working_crs=CRS,
        config=config,
    )
    return catalog, simulation, exposure, environment, road, grid, config


def test_canonical_artifacts_zero_partitions_queries_and_handoff(tmp_path: Path) -> None:
    catalog, simulation, exposure, _, _, _, _ = _artifact_setup(tmp_path)
    expected = json.loads(FIXTURE.read_text())
    simulation_reader = __import__(
        "mobile_sensing.simulation", fromlist=["SimulationArtifactReader"]
    ).SimulationArtifactReader(tmp_path, simulation)
    simulation_counts = {
        table.name: table.row_count for table in simulation_reader.artifact.manifest.tables
    }
    assert simulation_counts == expected["simulation_table_row_counts"]
    movements = read_partitioned_table(
        simulation_reader.artifact,
        table_name="movements",
        schema=MOVEMENT_SCHEMA,
    ).to_pylist()
    assert [
        {
            "replication_id": row["replication_id"],
            "fleet_id": row["fleet_id"],
            "vehicle_id": row["vehicle_id"],
            "start_s": row["start_s"],
            "end_s": row["end_s"],
            "edge_id": row["edge_id"],
            "movement_kind": row["movement_kind"],
        }
        for row in movements
    ] == expected["movements"]
    activities = read_partitioned_table(
        simulation_reader.artifact,
        table_name="activity_intervals",
        schema=ACTIVITY_SCHEMA,
    ).to_pylist()
    assert [
        {
            "replication_id": row["replication_id"],
            "fleet_id": row["fleet_id"],
            "vehicle_id": row["vehicle_id"],
            "activity_kind": row["activity_kind"],
            "duration_s": row["end_s"] - row["start_s"],
        }
        for row in activities
    ] == expected["activities"]

    reader = ExposureArtifactReader(tmp_path, chunk_rows=1)
    axes = reader.axes(exposure)
    exposure_counts = {table.name: table.row_count for table in axes["artifact"].manifest.tables}
    assert exposure_counts == expected["exposure_table_row_counts"]
    metadata, chunks = reader.read_sparse_chunks(
        exposure,
        replication_ids=("r1", "r2", "r3"),
        vehicle_keys=catalog.vehicle_keys,
    )
    chunks = list(chunks)
    assert len(chunks) == 2
    rows = [row for chunk in chunks for row in chunk.rows]
    assert metadata.complete
    bin_index = {value: index for index, value in enumerate(axes["time_bin_ids"])}
    projection = [
        {
            "replication_id": row.replication_id,
            "fleet_id": row.vehicle.fleet_id,
            "vehicle_id": row.vehicle.vehicle_id,
            "cell_id": row.cell_id,
            "time_bin_index": bin_index[row.time_bin_id],
            "duration_s": row.duration_s,
        }
        for row in rows
    ]
    assert projection == expected["exposure_rows"]

    empty_metadata, empty_chunks = reader.read_sparse_chunks(
        exposure,
        replication_ids=("r3",),
        vehicle_keys=catalog.vehicle_keys,
    )
    assert empty_metadata.complete and list(empty_chunks) == []
    exposure_manifest = next(
        table for table in axes["artifact"].manifest.tables if table.name == "exposure"
    )
    r3_index = tuple(exposure_manifest.partition_axes[0].values).index("r3")
    assert exposure_manifest.partitions[r3_index].row_count == 0
    assert not partition_file(
        axes["artifact"].directory / exposure_manifest.relative_path, r3_index
    ).exists()

    service = ExposureSensingQueryService(tmp_path)
    all_std = service.query(
        exposure,
        SensingMatrixQuery(
            replication_ids=("r1", "r2"),
            vehicle_keys=(),
            cell_ids=(),
            time_bin_ids=(),
            statistic="sample_std",
        ),
    )
    assert all_std.values == ()
    assert all_std.replication_count == 2
    assert all_std.expected_shape == (1, 2)
    assert all_std.unit == "s"
    vehicle_std = service.query(
        exposure,
        SensingMatrixQuery(
            replication_ids=("r1", "r2"),
            vehicle_keys=(catalog.vehicle_keys[0],),
            cell_ids=(),
            time_bin_ids=(),
            statistic="sample_std",
        ),
    )
    assert len(vehicle_std.values) == 1
    assert vehicle_std.values[0].value == pytest.approx(math.sqrt(50.0))
    fleet_raw = service.query(
        exposure,
        SensingMatrixQuery(
            replication_ids=("r1", "r2", "r3"),
            vehicle_keys=(catalog.vehicle_keys[0],),
            cell_ids=(),
            time_bin_ids=(),
            statistic="raw",
        ),
    )
    assert [(row.replication_id, row.value) for row in fleet_raw.values] == [("r1", 10.0)]
    with pytest.raises(ValueError, match="nonzero-row limit"):
        ExposureSensingQueryService(tmp_path, max_nonzero_rows=1).query(
            exposure,
            SensingMatrixQuery(
                replication_ids=("r1", "r2"),
                vehicle_keys=(),
                cell_ids=(),
                time_bin_ids=(),
                statistic="raw",
            ),
        )


def test_reallocation_is_immutable_cacheable_and_missing_is_not_empty(tmp_path: Path) -> None:
    _, simulation, exposure, environment, road, grid, config = _artifact_setup(tmp_path)
    simulation_manifest = tmp_path / "simulations" / simulation.artifact_id / "manifest.json"
    before = hashlib.sha256(simulation_manifest.read_bytes()).hexdigest()
    repeated = publish_exposure_artifact(
        artifact_root=tmp_path,
        simulation=simulation,
        environment=environment,
        road_edges=road,
        grid_cells=grid,
        working_crs=CRS,
        config=config,
    )
    assert repeated == exposure
    reallocated = publish_exposure_artifact(
        artifact_root=tmp_path,
        simulation=simulation,
        environment=environment,
        road_edges=road,
        grid_cells=grid,
        working_crs=CRS,
        config=config.model_copy(update={"bin_edges_s": (0.0, 5.0, 20.0)}),
    )
    assert reallocated != exposure
    assert hashlib.sha256(simulation_manifest.read_bytes()).hexdigest() == before

    reader = ExposureArtifactReader(tmp_path)
    axes = reader.axes(exposure)
    table = next(item for item in axes["artifact"].manifest.tables if item.name == "exposure")
    r1_index = tuple(table.partition_axes[0].values).index("r1")
    partition_file(axes["artifact"].directory / table.relative_path, r1_index).unlink()
    with pytest.raises(ValueError, match="partition is missing"):
        reader.axes(exposure)
