"""Reproducible offline Lausanne input preparation and example computation."""

import argparse
import hashlib
import json
import time
import zipfile
import itertools
from decimal import Decimal
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely import force_2d

from mobile_sensing.application.environment_editor import build_environment
from mobile_sensing.application.project_models import (
    ProjectConfig,
    FleetEditor,
    DemandEditor,
    SupplyEditor,
    DispatchEditor,
    SimulationEditor,
    PortfolioEditor,
    PortfolioFleetEditor,
    TemporalInterval,
    ShiftGroup,
    SpatialFeatureWeight,
    NumericRange,
)
from mobile_sensing.application.studio_models import EnvironmentEditor, FeatureSelection
from mobile_sensing.application.services import HeadlessApplication
from mobile_sensing.application.run_models import RunOptions
from mobile_sensing.application.run_pipeline import run_project, run_analysis
from mobile_sensing.datasets.inputs import snapshot_input, InputRegistration
from mobile_sensing.contracts import canonical_json_text, ArtifactRef


EXAMPLE_NAME = "[Example] Lausanne — Bus, Postal and Taxi Weekday"
ROUTE_IDS = ("92-1-V-j26-1", "92-3-S-j26-1", "92-7-P-j26-1")
STATION_LONGITUDE = 6.6290923032
STATION_LATITUDE = 46.5167918355
STATION_SOURCE = "https://data.sbb.ch/explore/dataset/linie/api/"


class BuildCancellation:
    def __init__(self, root):
        self.root = Path(root)

    def raise_if_cancelled(self):
        if self.is_cancelled():
            raise InterruptedError("Example build cancelled")

    def is_cancelled(self):
        return (self.root / "cancel-example-build").exists()


class BuildProgress:
    def __init__(self, root):
        self.root, self.last, self.phase = Path(root), 0.0, ""

    def update(self, *, phase, completed, total=None, **kwargs):
        now = time.monotonic()
        if phase != self.phase or now - self.last >= 5 or completed == total:
            value = {"phase": phase, "completed": completed, "total": total}
            print(json.dumps(value), flush=True)
            (self.root / "build-progress.json").write_text(json.dumps(value))
            self.last, self.phase = now, phase


def digest_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024**2), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path, value):
    Path(path).write_text(canonical_json_text(value) + "\n")


def prepare_example(root, data, *, cancellation, progress):
    root, data = Path(root), Path(data)
    config_path = root / "example-config.json"
    if config_path.is_file():
        return ProjectConfig.model_validate_json(config_path.read_bytes())
    started = time.perf_counter()
    derived = root / "example_sources"
    derived.mkdir(parents=True, exist_ok=True)
    sources = {
        str(path.relative_to(data)): {"sha256": digest_file(path), "bytes": path.stat().st_size}
        for path in sorted(data.rglob("*"))
        if path.is_file()
    }
    progress.update(phase="example.derive_full_region", completed=0, total=1)
    boundary = gpd.read_file(data / "boundary_lausanne.gpkg").to_crs(2056)
    if len(boundary) != 28:
        raise ValueError("Lausanne example expects the audited 28 boundary features")
    merged = gpd.GeoDataFrame(
        {"name": ["Lausanne region"]}, geometry=[force_2d(boundary.geometry.union_all())], crs=2056
    )
    merged.to_parquet(derived / "lausanne_region.parquet", index=False)
    grid = gpd.read_file(data / "grid_100m.gpkg").to_crs(2056)
    population = pd.read_csv(data / "population_swiss.csv")
    population = population.loc[population.year == 2024].copy()
    population["cell_id"] = population.easting.astype(str) + "_" + population.northing.astype(str)
    cells = grid.easting.astype(str) + "_" + grid.northing.astype(str)
    if population.cell_id.duplicated().any():
        raise ValueError("Population source contains duplicate 2024 cells")
    selected = population.loc[population.cell_id.isin(cells)].sort_values("cell_id")
    selected.to_csv(derived / "lausanne_population_2024.csv", index=False)
    progress.update(phase="example.filter_complete_bus_lines", completed=0, total=1)
    gtfs = data / "gtfs"
    trips = pd.read_csv(gtfs / "trips.csv", dtype=str, keep_default_na=False)
    trips = trips.loc[trips.route_id.isin(ROUTE_IDS)]
    if set(trips.route_id) != set(ROUTE_IDS):
        raise ValueError("GTFS source lacks one of the five fixed route IDs")
    trip_ids = set(trips.trip_id)
    chunks = []
    for chunk in pd.read_csv(
        gtfs / "stop_times.csv", dtype=str, keep_default_na=False, chunksize=100000
    ):
        cancellation.raise_if_cancelled()
        chunks.append(chunk.loc[chunk.trip_id.isin(trip_ids)])
    times = pd.concat(chunks, ignore_index=True)
    stops = pd.read_csv(gtfs / "stops.csv", dtype=str, keep_default_na=False)
    stops = stops.loc[stops.stop_id.isin(times.stop_id)]
    routes = pd.read_csv(gtfs / "buses.csv", dtype=str, keep_default_na=False)
    routes = routes.loc[routes.route_id.isin(ROUTE_IDS)]
    tables = {"trips": trips, "stop_times": times, "stops": stops, "routes": routes}
    for name in ("calendar", "calendar_dates", "agency"):
        table = pd.read_csv(gtfs / f"{name}.csv", dtype=str, keep_default_na=False)
        if name.startswith("calendar"):
            table = table.loc[table.service_id.isin(trips.service_id)]
        elif "agency_id" in routes:
            table = table.loc[table.agency_id.isin(routes.agency_id)]
        tables[name] = table
    archive = derived / "lausanne_five_lines_gtfs.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as out:
        for name, table in sorted(tables.items()):
            info = zipfile.ZipInfo(f"{name}.txt", date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            out.writestr(info, table.to_csv(index=False).encode())
    points = gpd.GeoSeries(
        gpd.points_from_xy(pd.to_numeric(stops.stop_lon), pd.to_numeric(stops.stop_lat)), crs=4326
    ).to_crs(2056)
    outside = stops.loc[~points.covered_by(merged.geometry.iloc[0]).to_numpy(), "stop_id"].tolist()
    if outside:
        raise ValueError(f"Selected source stops outside full region: {outside}")

    def register(path, name, role, crs=None):
        return snapshot_input(
            root, InputRegistration(path=str(path), name=name, role=role, source_crs=crs)
        )["input_id"]

    boundary_id = register(
        derived / "lausanne_region.parquet", "Lausanne region · 28 merged boundaries", "boundary"
    )
    network_id = register(
        data / "lausanne_roads_encoded.gpkg", "Lausanne supplied road network", "network"
    )
    grid_id = register(data / "grid_100m.gpkg", "Lausanne full 100 m grid", "grid")
    population_id = register(
        derived / "lausanne_population_2024.csv",
        "Lausanne population · 2024",
        "population",
        "EPSG:2056",
    )
    gtfs_id = register(archive, "Lausanne GTFS · lines 1, 3, 7, 9, 13", "gtfs")
    editor = EnvironmentEditor(
        boundary_input=boundary_id,
        network_input=network_id,
        grid_input=grid_id,
        working_crs="EPSG:2056",
        timezone="Europe/Zurich",
        speed_kph=30.0,
        topology_policy="conservative_repair@1",
        features=(
            FeatureSelection(
                input_id=population_id, name="population", year=2024, unit="residents"
            ),
        ),
        osm_features=("commercial", "public_services"),
    )
    prepared = build_environment(
        root,
        editor,
        application=HeadlessApplication(root),
        cancellation=cancellation,
        progress=progress,
    )
    fleets = demonstration_fleets(gtfs_id)
    config = ProjectConfig(
        environment=editor,
        prepared_environment=prepared,
        fleets=fleets,
        simulation=SimulationEditor(replications=10, temporal_resolution_minutes=60.0),
    )
    _write_json(config_path, config)
    _write_json(
        root / "input-provenance.json",
        {
            "sources": sources,
            "derived_rule": "Union all 28 polygons; retain full grid; exact 2024 population lattice join with missing cells zero; filter three full GTFS route IDs across the complete calendar without dropping stop rows",
            "boundary_features": len(boundary),
            "grid_cells": len(grid),
            "population_source_rows": len(population),
            "matched_population_rows": len(selected),
            "matched_residents": int(selected.residents.sum()),
            "selected_gtfs_tables": {name: len(table) for name, table in tables.items()},
            "route_ids": ROUTE_IDS,
            "outside_source_stop_ids": outside,
            "elapsed_seconds": time.perf_counter() - started,
        },
    )
    return config


def demonstration_fleets(gtfs_id):
    """One shared editable definition for source builds and the Lausanne tutorial."""
    return (
        FleetEditor(
            fleet_id="bus",
            name="Bus · lines 1, 3 and 7",
            demand=DemandEditor(
                source="import",
                task_type="ordered",
                template="gtfs",
                input_id=gtfs_id,
                route_ids=ROUTE_IDS,
            ),
            supply=SupplyEditor(
                source="timetable",
                fleet_size=None,
                operating_start=None,
                operating_end=None,
                initial_location="input",
            ),
            dispatch=DispatchEditor(mode="scheduled"),
        ),
        FleetEditor(
            fleet_id="postal",
            name="Postal",
            demand=DemandEditor(
                task_volume=2000.0,
                volume_mode="fixed",
                start_time="06:00",
                end_time="21:00",
                spatial_feature="population",
                spatial_weights=(
                    SpatialFeatureWeight(feature="population", weight=0.8),
                    SpatialFeatureWeight(feature="public_services", weight=0.2),
                ),
                location_condition="depot_roundtrip",
                release_mode="at_start",
                service_seconds=120.0,
                quantity=1.0,
            ),
            supply=SupplyEditor(
                fleet_size=20,
                operating_start="06:00",
                operating_end="21:00",
                activation="uniform_bounded",
                latest_start="12:00",
                work_hours=8.0,
                initial_location="depot",
                synthetic_depot=True,
                depot_longitude=STATION_LONGITUDE,
                depot_latitude=STATION_LATITUDE,
                spatial_feature="population",
                post_service="return_after_plan",
                capacity_mode="consumable",
                capacity=100.0,
                depot_min_stay_minutes=15.0,
                service_area_mode="auto",
                auto_service_area_count=4,
            ),
            dispatch=DispatchEditor(
                mode="one_shot", max_cost_pairs=5_000_000, planning_timeout_seconds=600.0
            ),
        ),
        FleetEditor(
            fleet_id="taxi",
            name="Taxi",
            demand=DemandEditor(
                task_type="od",
                volume_mode="expected",
                task_volume=800.0,
                generation_timing="online",
                temporal_mode="shares",
                time_profile=tuple(
                    TemporalInterval(start_time=start, end_time=end, value=share)
                    for start, end, share in (
                        ("00:00", "06:00", 0.05),
                        ("06:00", "09:00", 0.18),
                        ("09:00", "12:00", 0.13),
                        ("12:00", "16:00", 0.20),
                        ("16:00", "19:00", 0.24),
                        ("19:00", "21:00", 0.12),
                        ("21:00", "24:00", 0.08),
                    )
                ),
                start_time="00:00",
                end_time="24:00",
                spatial_feature="population",
                spatial_weights=(
                    SpatialFeatureWeight(feature="population", weight=0.5),
                    SpatialFeatureWeight(feature="commercial", weight=0.3),
                    SpatialFeatureWeight(feature="public_services", weight=0.2),
                ),
                destination_feature="population",
                destination_spatial_weights=(
                    SpatialFeatureWeight(feature="population", weight=0.5),
                    SpatialFeatureWeight(feature="commercial", weight=0.3),
                    SpatialFeatureWeight(feature="public_services", weight=0.2),
                ),
                pickup_seconds=60.0,
                service_seconds=30.0,
            ),
            supply=SupplyEditor(
                fleet_size=80,
                operating_start="00:00",
                operating_end="24:00",
                activation="uniform_bounded",
                latest_start="16:00",
                work_hours=8.0,
                spatial_feature="population",
                post_service="random_cruise",
                capacity_mode="occupancy",
                capacity=1.0,
                shift_groups=(
                    ShiftGroup(name="Night", count=8, start_time="00:00", latest_start="00:00"),
                    ShiftGroup(name="Morning", count=24, start_time="05:00", latest_start="06:00"),
                    ShiftGroup(name="Daytime", count=24, start_time="09:00", latest_start="11:00"),
                    ShiftGroup(
                        name="Afternoon", count=24, start_time="15:00", latest_start="16:00"
                    ),
                ),
            ),
            dispatch=DispatchEditor(mode="sequential", max_pickup_minutes=15.0),
        ),
    )


def compute_example(root, config, *, cancellation, progress):
    root = Path(root)
    options = RunOptions(memory_limit_bytes=8 * 1024**3, job_timeout_s=14400)
    resolution = root / "example-resolution.json"
    run = run_project(
        root,
        config,
        name="Lausanne · 14 January 2026 · Full day",
        source_revision_id=None,
        options=options,
        cancellation=cancellation,
        progress=progress,
        resolution_reference=(
            ArtifactRef.model_validate_json(resolution.read_bytes())
            if resolution.is_file()
            else None
        ),
    )
    _write_json(Path(root) / "example-run.json", run)
    worst_case = run_analysis(
        root,
        demonstration_portfolio(run, risk_metric="p05", spatial_weight="population"),
        name="Worst-case utility · 5-minute saturation",
        source_revision_id=None,
        options=options,
        cancellation=cancellation,
        progress=progress,
    )
    standard_deviation = run_analysis(
        root,
        demonstration_portfolio(run, risk_metric="std", spatial_weight="population"),
        name="Standard-deviation utility · 5-minute saturation",
        source_revision_id=None,
        options=options,
        cancellation=cancellation,
        progress=progress,
    )
    _write_json(Path(root) / "example-analysis.json", worst_case)
    _write_json(Path(root) / "example-analysis-std.json", standard_deviation)
    return run, worst_case, standard_deviation


def demonstration_portfolio(
    run,
    *,
    risk_metric="p05",
    budgets=(0.0, 20.0, 40.0, 60.0, 80.0, 100.0),
    saturation_minutes=5.0,
    spatial_weight="uniform",
):
    return PortfolioEditor(
        source_run_id=run.run_id,
        spatial_weight=spatial_weight,
        sampling_runs=100,
        cost_unit="sensor",
        budgets=budgets,
        saturation_minutes=saturation_minutes,
        utility_temporal_resolution_minutes=1440.0,
        risk_metric=risk_metric,
        fleets=tuple(
            PortfolioFleetEditor(
                fleet_id=fleet,
                counts=tuple(sorted(set(range(0, count + 1, 5)) | {count})),
                count_range=NumericRange(minimum=0, maximum=count, step=5),
            )
            for fleet, count in sorted(run.vehicle_counts.items())
        ),
    )


def feasible_portfolio_count(editor):
    """Audit the explicit retained count grid against the largest exact budget."""
    maximum = Decimal(str(max(editor.budgets)))
    return sum(
        sum(Decimal(str(fleet.unit_cost)) * count for fleet, count in zip(editor.fleets, counts))
        <= maximum
        for counts in itertools.product(*(fleet.counts for fleet in editor.fleets))
    )


def compute_batch(root, config, run, *, cancellation, progress, options):
    compared = config.model_copy(
        update={
            "fleets": tuple(
                (
                    fleet.model_copy(
                        update={
                            "dispatch": fleet.dispatch.model_copy(
                                update={"mode": "batch", "batch_minutes": 2.0}
                            )
                        }
                    )
                    if fleet.fleet_id == "taxi"
                    else fleet
                )
                for fleet in config.fleets
            )
        }
    )
    return run_project(
        root,
        compared,
        name="Lausanne · same requests · 2-minute Batch",
        source_revision_id=None,
        options=options,
        cancellation=cancellation,
        progress=progress,
        replay_from=run,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--stage", choices=("prepare", "compute"), default="prepare")
    args = parser.parse_args()
    args.root.mkdir(parents=True, exist_ok=True)
    cancellation, progress = BuildCancellation(args.root), BuildProgress(args.root)
    config = prepare_example(args.root, args.data, cancellation=cancellation, progress=progress)
    if args.stage == "compute":
        compute_example(args.root, config, cancellation=cancellation, progress=progress)


if __name__ == "__main__":
    main()
