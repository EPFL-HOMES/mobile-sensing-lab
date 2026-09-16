"""Bounded offline Lausanne vertical workflow for M07 acceptance and research setup."""

from __future__ import annotations

import json
import time
import tracemalloc
from collections import defaultdict
from pathlib import Path
from typing import Self

import geopandas as gpd
from pydantic import field_validator, model_validator
from shapely.geometry import Point

from mobile_sensing.application.models import FleetRuntimeResource, ScenarioResourceBundle
from mobile_sensing.application.services import HeadlessApplication
from mobile_sensing.contracts import (
    AssignmentPlan,
    AssignmentRow,
    ContractModel,
    EnvironmentBuildConfig,
    EnvironmentProviderRequest,
    ExecutionOptions,
    ExposureConfig,
    ScenarioConfig,
    canonical_json_text,
    scientific_hash,
    stable_id,
)
from mobile_sensing.datasets import (
    GTFSReconstructionConfig,
    discover_gtfs_directory,
    gtfs_service_origin,
)
from mobile_sensing.environment import (
    LocalDatasetCatalog,
    LocalDatasetResource,
    LocalRegionSelection,
    PreparedEnvironment,
    PreparedEnvironmentReader,
)
from mobile_sensing.exposure import ExposureArtifactReader
from mobile_sensing.simulation import LocationWeight, OdWeight, SimulationArtifactReader


LAUSANNE_SMOKE_VERSION = "lausanne-bounded-smoke@1"


class LausanneSmokeConfig(ContractModel):
    """Explicit bounded parameters; no full-city simulation is implied."""

    schema_version: str = "2.0"
    service_date: str = "2026-01-14"
    route_ids: tuple[str, ...] = ("92-13-H-j26-1",)
    study_municipalities: tuple[str, ...] = (
        "Lausanne (suburban)",
        "Lausanne (urban)",
    )
    routing_profile_id: str = "road_static_30kph"
    agency_timezone: str = "Europe/Berlin"
    road_speed_mps: float = 8.333333333333334
    grid_cell_size_m: float = 100.0
    observation_duration_s: float = 7_200.0
    reporting_bin_s: float = 900.0
    replications: int = 2
    master_seed: int = 2_026_011_4
    max_gtfs_tasks: int = 4
    generated_rate_tasks_per_s: float = 1.0 / 900.0
    generated_service_duration_s: float = 30.0
    allow_uniform_population_proxy: bool = False
    memory_limit_bytes: int = 512 * 1024 * 1024

    @field_validator("route_ids", "study_municipalities")
    @classmethod
    def canonical_values(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or tuple(sorted(set(value))) != value:
            raise ValueError("bounded Lausanne selections must be sorted and unique")
        return value

    @model_validator(mode="after")
    def validate_bounds(self) -> Self:
        if self.schema_version != "2.0":
            raise ValueError("Lausanne smoke requires schema version 2.0")
        if self.road_speed_mps <= 0 or self.grid_cell_size_m <= 0:
            raise ValueError("speed and grid size must be positive")
        if self.observation_duration_s <= 0 or self.reporting_bin_s <= 0:
            raise ValueError("observation and reporting durations must be positive")
        if self.observation_duration_s > 7_200:
            raise ValueError("the default smoke cannot exceed two observation hours")
        if self.observation_duration_s % self.reporting_bin_s != 0:
            raise ValueError("reporting bins must exactly partition the observation interval")
        if not 1 <= self.replications <= 4:
            raise ValueError("the bounded smoke permits one to four replications")
        if not 1 <= self.max_gtfs_tasks <= 16:
            raise ValueError("the bounded smoke permits one to sixteen GTFS tasks")
        if not 0 < self.generated_rate_tasks_per_s <= 0.01:
            raise ValueError("generated demand rate is outside the bounded smoke limit")
        if self.generated_service_duration_s < 0:
            raise ValueError("generated service duration must be nonnegative")
        if self.memory_limit_bytes <= 0:
            raise ValueError("memory limit must be positive")
        return self


def _lausanne_environment(
    application: HeadlessApplication,
    data_root: Path,
    config: LausanneSmokeConfig,
) -> PreparedEnvironment:
    boundary_path = data_root / "boundary_lausanne.gpkg"
    boundary_names = tuple(sorted(gpd.read_file(boundary_path).name.astype(str)))
    catalog = LocalDatasetCatalog(
        (
            LocalDatasetResource("boundary_lausanne", "boundary", boundary_path, "EPSG:4326"),
            LocalDatasetResource(
                "roads_encoded",
                "network",
                data_root / "lausanne_roads_encoded.gpkg",
                "EPSG:4326",
                "roads_encoded",
            ),
            LocalDatasetResource(
                "population_swiss_2024",
                "population",
                data_root / "population_swiss.csv",
                "EPSG:2056",
                population_cell_size_m=100.0,
            ),
        ),
        network_dataset_id="roads_encoded",
        feature_dataset_ids={"population": "population_swiss_2024"},
        region_selections={
            "routing_extent_lausanne": LocalRegionSelection("boundary_lausanne", boundary_names)
        },
    )
    request = EnvironmentProviderRequest.model_validate(
        {
            "schema_version": "2.0",
            "provider": "environment.local_files@1",
            "boundary": {
                "kind": "local_municipalities",
                "dataset_id": "boundary_lausanne",
                "municipality_names": config.study_municipalities,
            },
            "routing_extent_ref": "routing_extent_lausanne",
            "target_crs": "EPSG:2056",
            "network_mode": "drive",
            "requested_features": ("population",),
            "cache_policy": "reuse",
        }
    )
    build = EnvironmentBuildConfig.model_validate(
        {
            "schema_version": "2.0",
            "provider": "environment.local_files@1",
            "boundary": request.boundary.model_dump(),
            "routing_extent_ref": "routing_extent_lausanne",
            "network_source": {
                "kind": "supplied",
                "dataset_id": "roads_encoded",
                "layer": "roads_encoded",
                "topology_policy": "conservative_repair@1",
                "endpoint_tolerance_m": 0.05,
            },
            "network_mode": "drive",
            "working_crs": "EPSG:2056",
            "travel_time_profiles": (
                {
                    "profile_id": config.routing_profile_id,
                    "source": {
                        "kind": "constant_speed",
                        "speed_mps": config.road_speed_mps,
                    },
                },
            ),
            "snapping": {"max_distance_m": 250.0},
            "grid": {
                "kind": "regular",
                "cell_size_m": config.grid_cell_size_m,
                "origin_easting_m": 2_530_000.0,
                "origin_northing_m": 1_150_000.0,
            },
            "population_features": {
                "dataset_id": "population_swiss_2024",
                "year": 2024,
                "missing_policy": "zero",
            },
        }
    )
    reference = application.prepare_environment(catalog, request, build)
    return PreparedEnvironmentReader(application.artifact_root).read(reference)


def _table_counts(artifact) -> dict[str, int]:
    return {
        table.name: sum(partition.row_count for partition in table.partitions)
        for table in artifact.manifest.tables
    }


def _population_location_weights(
    environment: PreparedEnvironment,
    locations,
    *,
    allow_uniform_proxy: bool,
) -> tuple[tuple[LocationWeight, ...], dict[str, object]]:
    population = {
        str(row.cell_id): float(row.residents)
        for row in environment.population_features.itertuples()
    }
    weighted = []
    zero_or_unmatched = 0
    for location in sorted(locations, key=lambda item: item.location_id):
        assert location.snapped_x is not None and location.snapped_y is not None
        point = Point(location.snapped_x, location.snapped_y)
        candidate_indices = sorted(
            set(environment.grid_cells.sindex.query(point, predicate="intersects"))
        )
        covering = sorted(
            str(row.cell_id)
            for row in environment.grid_cells.iloc[candidate_indices].itertuples()
            if row.geometry.covers(point)
        )
        weight = population.get(covering[0], 0.0) if covering else 0.0
        if weight > 0.0:
            weighted.append(LocationWeight(location_id=location.location_id, weight=weight))
        else:
            zero_or_unmatched += 1
    used_uniform_proxy = False
    if len(weighted) < 2:
        if not allow_uniform_proxy:
            raise ValueError(
                "fewer than two GTFS locations have positive population weights; "
                "set allow_uniform_population_proxy explicitly to use a proxy"
            )
        weighted = [
            LocationWeight(location_id=location.location_id, weight=1.0)
            for location in sorted(locations, key=lambda item: item.location_id)
        ]
        used_uniform_proxy = True
    result = tuple(weighted)
    return result, {
        "allocation_rule": "snapped_point_exact_covering_100m_cell",
        "positive_weight_location_count": len(result),
        "zero_or_unmatched_location_count": zero_or_unmatched,
        "total_location_weight": sum(item.weight for item in result),
        "uniform_proxy_requested": allow_uniform_proxy,
        "uniform_proxy_used": used_uniform_proxy,
    }


def run_lausanne_smoke(
    *,
    artifact_root: str | Path,
    data_root: str | Path,
    config: LausanneSmokeConfig,
    prepared_environment: PreparedEnvironment | None = None,
) -> dict[str, object]:
    """Execute and verify one bounded GTFS/location/OD vertical artifact chain."""

    config = LausanneSmokeConfig.model_validate(config)
    artifact_root = Path(artifact_root).resolve()
    data_root = Path(data_root).resolve()
    application = HeadlessApplication(artifact_root)
    routing_extent_municipalities = tuple(
        sorted(gpd.read_file(data_root / "boundary_lausanne.gpkg").name.astype(str))
    )
    timings: dict[str, float] = {}
    tracemalloc.start()
    overall_start = time.perf_counter()
    try:
        started = time.perf_counter()
        environment = prepared_environment or _lausanne_environment(application, data_root, config)
        timings["environment_s"] = time.perf_counter() - started

        reconstruction_config = GTFSReconstructionConfig(
            schema_version="2.0",
            fleet_id="lausanne_transit",
            service_date=config.service_date,
            route_ids=config.route_ids,
            routing_profile_id=config.routing_profile_id,
            turnaround_duration_s=300.0,
            max_selected_trips=1_000,
            max_stop_time_rows=100_000,
        )
        started = time.perf_counter()
        reconstruction = application.reconstruct_gtfs(
            discover_gtfs_directory(data_root / "gtfs"),
            reconstruction_config,
            environment,
        )
        timings["gtfs_reconstruction_s"] = time.perf_counter() - started

        rows_by_vehicle: dict[str, list[AssignmentRow]] = defaultdict(list)
        for row in reconstruction.assignments.rows:
            rows_by_vehicle[row.vehicle.vehicle_id].append(row)
        selected_vehicle_id = sorted(rows_by_vehicle)[0]
        selected_rows = tuple(
            sorted(rows_by_vehicle[selected_vehicle_id], key=lambda row: row.order_index)[
                : config.max_gtfs_tasks
            ]
        )
        task_by_id = {task.task_id: task for task in reconstruction.tasks}
        selected_tasks = tuple(task_by_id[row.task_id] for row in selected_rows)
        selected_spec = next(
            item for item in reconstruction.vehicles if item.key.vehicle_id == selected_vehicle_id
        )
        assignment = AssignmentPlan(
            assignment_plan_id=stable_id(
                "assignment_plan",
                {
                    "smoke": LAUSANNE_SMOKE_VERSION,
                    "source": reconstruction.assignments.assignment_plan_id,
                    "tasks": [row.task_id for row in selected_rows],
                },
            ),
            rows=tuple(
                AssignmentRow(
                    vehicle=row.vehicle,
                    order_index=index,
                    task_id=row.task_id,
                )
                for index, row in enumerate(selected_rows)
            ),
        )
        simulation_start_s = max(selected_spec.availability_start_s, selected_tasks[0].release_s)
        end_s = simulation_start_s + config.observation_duration_s
        selected_locations = tuple(sorted(reconstruction.locations, key=lambda x: x.location_id))
        location_weights, population_weight_diagnostics = _population_location_weights(
            environment,
            selected_locations,
            allow_uniform_proxy=config.allow_uniform_population_proxy,
        )
        location_by_id = {item.location_id: item for item in selected_locations}
        generator_locations = tuple(
            location_by_id[item.location_id] for item in location_weights[:2]
        )
        location_weight_id = stable_id(
            "weight_source", {"smoke": LAUSANNE_SMOKE_VERSION, "kind": "location"}
        )
        od_weight_id = stable_id("weight_source", {"smoke": LAUSANNE_SMOKE_VERSION, "kind": "od"})

        common_generated_supply = {
            "source": "generated",
            "catalog_size": 1,
            "availability": {
                "kind": "simultaneous",
                "start_s": simulation_start_s,
                "end_s": end_s,
            },
            "capacity": {"mode": "none"},
            "idle_policy": {"policy": "stationary"},
        }
        fleets = (
            {
                "fleet_id": "lausanne_transit",
                "label": "Bounded inferred Route 13 duty",
                "demand": {
                    "source": "gtfs",
                    "structure": "ordered",
                    "adapter": "demand.gtfs_reconstruction@1",
                    "parameters": {"reconstruction_id": reconstruction.reference.artifact_id},
                },
                "supply": {
                    "source": "gtfs_duties",
                    "reconstruction_id": reconstruction.reference.artifact_id,
                    "capacity": {"mode": "none"},
                    "idle_policy": {"policy": "stationary"},
                },
                "dispatch": {
                    "policy": "predefined",
                    "assignment_plan_ref": assignment.assignment_plan_id,
                },
                "routing": {"profile_id": config.routing_profile_id},
            },
            {
                "fleet_id": "synthetic_location",
                "label": "Synthetic location-service example",
                "demand": {
                    "source": "generator",
                    "structure": "location",
                    "adapter": "demand.poisson_piecewise_constant@1",
                    "generation_timing": "offline",
                    "parameters": {
                        "intervals": (
                            {
                                "start_s": simulation_start_s,
                                "end_s": end_s,
                                "rate_tasks_per_s": config.generated_rate_tasks_per_s,
                            },
                        ),
                        "location_weights_ref": location_weight_id,
                        "service_duration_s": config.generated_service_duration_s,
                    },
                },
                "supply": {
                    **common_generated_supply,
                    "catalog_id_namespace": "location_vehicle",
                    "initial_locations_ref": "location_initials",
                },
                "dispatch": {"policy": "nearest_matching"},
                "routing": {"profile_id": config.routing_profile_id},
            },
            {
                "fleet_id": "synthetic_od",
                "label": "Synthetic OD example",
                "demand": {
                    "source": "generator",
                    "structure": "od",
                    "adapter": "demand.poisson_piecewise_constant@1",
                    "generation_timing": "offline",
                    "parameters": {
                        "intervals": (
                            {
                                "start_s": simulation_start_s,
                                "end_s": end_s,
                                "rate_tasks_per_s": config.generated_rate_tasks_per_s,
                            },
                        ),
                        "od_weights_ref": od_weight_id,
                        "service_duration_s": config.generated_service_duration_s,
                    },
                },
                "supply": {
                    **common_generated_supply,
                    "catalog_id_namespace": "od_vehicle",
                    "initial_locations_ref": "od_initials",
                },
                "dispatch": {"policy": "nearest_matching"},
                "routing": {"profile_id": config.routing_profile_id},
            },
        )
        scenario = ScenarioConfig.model_validate(
            {
                "schema_version": "2.0",
                "environment_id": environment.reference.artifact_id,
                "clock": {
                    "origin_utc": gtfs_service_origin(config.service_date, config.agency_timezone),
                    "display_timezone": config.agency_timezone,
                    "simulation_start_s": simulation_start_s,
                    "observation_start_s": simulation_start_s,
                    "end_s": end_s,
                },
                "fleets": fleets,
                "replications": config.replications,
                "master_seed": config.master_seed,
                "joint_scenario_model": {"kind": "independent_conditional_environment"},
            }
        )
        resources = ScenarioResourceBundle(
            scenario=scenario,
            fleets=(
                FleetRuntimeResource(
                    fleet_id="lausanne_transit",
                    demand_artifact=reconstruction.reference,
                    supply_artifact=reconstruction.reference,
                    selected_task_ids=tuple(sorted(task.task_id for task in selected_tasks)),
                    selected_vehicle_ids=(selected_vehicle_id,),
                    locations=selected_locations,
                    tasks=selected_tasks,
                    vehicle_specs=(selected_spec,),
                    assignment_plan=assignment,
                    assumptions=tuple(
                        sorted(
                            (
                                "bounded_gtfs_selection",
                                "inferred_vehicle_duties",
                                "uncalibrated_constant_road_speed_mps="
                                f"{config.road_speed_mps:.15g}",
                            )
                        )
                    ),
                ),
                FleetRuntimeResource(
                    fleet_id="synthetic_location",
                    weight_source_id=location_weight_id,
                    initial_locations_source_id="location_initials",
                    locations=selected_locations,
                    generated_initial_location_ids=(generator_locations[0].location_id,),
                    location_weights=location_weights,
                    assumptions=(
                        "synthetic_demand",
                        "synthetic_population_weighted_destinations",
                    ),
                ),
                FleetRuntimeResource(
                    fleet_id="synthetic_od",
                    weight_source_id=od_weight_id,
                    initial_locations_source_id="od_initials",
                    locations=selected_locations,
                    generated_initial_location_ids=(generator_locations[1].location_id,),
                    od_weights=(
                        OdWeight(
                            origin_location_id=generator_locations[0].location_id,
                            destination_location_id=generator_locations[1].location_id,
                            weight=1.0,
                        ),
                    ),
                    assumptions=("synthetic_demand",),
                ),
            ),
        )

        started = time.perf_counter()
        validated = application.validate_scenario(environment, resources)
        timings["scenario_validation_s"] = time.perf_counter() - started
        started = time.perf_counter()
        simulation = application.run_simulation(
            validated,
            ExecutionOptions(
                workers=1,
                memory_limit_bytes=config.memory_limit_bytes,
                progress_frequency_events=100,
            ),
        )
        timings["simulation_s"] = time.perf_counter() - started
        bin_count = int(config.observation_duration_s / config.reporting_bin_s)
        bin_edges = tuple(
            simulation_start_s + index * config.reporting_bin_s for index in range(bin_count + 1)
        )
        started = time.perf_counter()
        exposure = application.allocate_exposure(
            environment,
            simulation.reference,
            ExposureConfig(
                schema_version="2.0",
                simulation_id=simulation.reference.artifact_id,
                sensing_geometry_id=environment.metadata.sensing_hash,
                bin_edges_s=bin_edges,
                active_movement_kinds=(
                    "cruise",
                    "depot_return",
                    "reposition",
                    "service_inter_step",
                    "service_pickup",
                ),
            ),
        )
        timings["exposure_s"] = time.perf_counter() - started
        timings["total_s"] = time.perf_counter() - overall_start
        _, peak_bytes = tracemalloc.get_traced_memory()

        simulation_reader = SimulationArtifactReader(artifact_root, simulation.reference)
        exposure_axes = ExposureArtifactReader(artifact_root).axes(exposure)
        simulation_artifact = simulation_reader.artifact
        exposure_artifact = exposure_axes["artifact"]
        task_counts_by_replication = {
            replication.replication_id: {
                fleet_id: sum(1 for task in replication.tasks if task.fleet_id == fleet_id)
                for fleet_id in sorted(item.fleet_id for item in scenario.fleets)
            }
            for replication in validated.replications
        }
        return {
            "schema_version": "2.0",
            "workflow_version": LAUSANNE_SMOKE_VERSION,
            "configuration_hash": scientific_hash(config),
            "service_date": config.service_date,
            "route_ids": list(config.route_ids),
            "study_municipalities": list(config.study_municipalities),
            "routing_extent_municipalities": list(routing_extent_municipalities),
            "replications_R": config.replications,
            "portfolio_sampling_rounds_J": None,
            "selected_gtfs_vehicle_ids": [selected_vehicle_id],
            "selected_gtfs_task_ids": [task.task_id for task in selected_tasks],
            "physical_vehicle_count": len(validated.vehicle_specs),
            "task_counts_by_replication": task_counts_by_replication,
            "source_diagnostics": json.loads(canonical_json_text(dict(reconstruction.diagnostics))),
            "population_weight_diagnostics": population_weight_diagnostics,
            "assumptions": list(validated.assumptions),
            "scenario_impact": list(validated.impact_summaries),
            "artifacts": {
                "environment": environment.reference.model_dump(mode="json"),
                "gtfs_reconstruction": reconstruction.reference.model_dump(mode="json"),
                "scenario_validation": validated.reference.model_dump(mode="json"),
                "simulation": simulation.reference.model_dump(mode="json"),
                "exposure": exposure.model_dump(mode="json"),
            },
            "simulation_output_rows": _table_counts(simulation_artifact),
            "exposure_output_rows": _table_counts(exposure_artifact),
            "logical_vehicle_exposure_matrices": len(exposure_axes["replication_ids"])
            * len(exposure_axes["vehicle_keys"]),
            "grid_cell_count": len(exposure_axes["cell_ids"]),
            "reporting_bin_count": len(exposure_axes["time_bin_ids"]),
            "timing_s": timings,
            "memory": {
                "python_tracemalloc_peak_bytes": peak_bytes,
                "conservative_scenario_estimate_bytes": validated.estimated_working_bytes,
                "configured_limit_bytes": config.memory_limit_bytes,
            },
        }
    finally:
        tracemalloc.stop()
