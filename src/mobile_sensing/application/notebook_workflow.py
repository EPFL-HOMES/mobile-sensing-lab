"""Local notebook orchestration and static result views; no alternate simulator.

Scientific execution, exposure and portfolio statistics delegate to the existing
backend. Plotting imports are lazy so headless application users need no plotting extra.
"""

from __future__ import annotations

import hashlib
import json
import math
import platform
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import Point

from mobile_sensing.application.models import FleetRuntimeResource, ScenarioResourceBundle
from mobile_sensing.contracts import (
    AssignmentPlan,
    AssignmentRow,
    EnvironmentBuildConfig,
    EnvironmentProviderRequest,
    ExposureConfig,
    PortfolioConfig,
    ScenarioConfig,
    canonical_json_text,
    stable_id,
)
from mobile_sensing.datasets import discover_gtfs_directory
from mobile_sensing.datasets.parsing import LocationResolver
from mobile_sensing.environment import (
    LocalDatasetCatalog,
    LocalDatasetResource,
    LocalRegionSelection,
    PreparedEnvironmentReader,
)
from mobile_sensing.exposure import ExposureArtifactReader
from mobile_sensing.portfolio import (
    CellTimeWeight,
    PortfolioAnalysisArtifactReader,
    UtilityWeightResource,
)
from mobile_sensing.simulation import LocationWeight, OdWeight, SimulationArtifactReader


def prepare_environment(application, data_root, build: EnvironmentBuildConfig):
    """Bind the supplied Lausanne files to a fully editable backend build config."""
    data_root = Path(data_root)
    names = tuple(sorted(gpd.read_file(data_root / "boundary_lausanne.gpkg").name.astype(str)))
    resources = (
        LocalDatasetResource(
            "boundary_lausanne", "boundary", data_root / "boundary_lausanne.gpkg", "EPSG:4326"
        ),
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
    )
    catalog = LocalDatasetCatalog(
        resources,
        network_dataset_id="roads_encoded",
        feature_dataset_ids={"population": "population_swiss_2024"},
        region_selections={
            "routing_extent_lausanne": LocalRegionSelection("boundary_lausanne", names)
        },
    )
    request = EnvironmentProviderRequest(
        schema_version="2.0",
        provider=build.provider,
        boundary=build.boundary,
        routing_extent_ref=build.routing_extent_ref,
        target_crs=build.working_crs,
        network_mode=build.network_mode,
        requested_features=("population",) if build.population_features else (),
        cache_policy="reuse",
    )
    reference = application.prepare_environment(catalog, request, build)
    return PreparedEnvironmentReader(application.artifact_root).read(reference)


def reconstruct_gtfs(application, environment, data_root, config):
    return application.reconstruct_gtfs(
        discover_gtfs_directory(Path(data_root) / "gtfs"), config, environment
    )


def bus_resources(reconstruction, *, vehicle_count=None, vehicle_ids=None, end_s):
    """Retain source duties and their complete task prefix up to the run horizon.

    Execution starts at the earliest selected duty entry, even if the observation
    starts later. This preserves vehicle position and preceding timetable history.
    """
    if vehicle_ids is not None and vehicle_count is not None:
        raise ValueError("Choose vehicle_ids or vehicle_count, not both")
    catalog = {v.key.vehicle_id: v for v in reconstruction.vehicles}
    if vehicle_ids is None:
        if vehicle_count is not None and (
            isinstance(vehicle_count, bool)
            or not isinstance(vehicle_count, int)
            or not 1 <= vehicle_count <= len(catalog)
        ):
            raise ValueError(f"vehicle_count must lie in [1, {len(catalog)}], or be None")
        vehicle_ids = sorted(catalog)[:vehicle_count]
    else:
        vehicle_ids = list(vehicle_ids)
        if (
            not vehicle_ids
            or len(set(vehicle_ids)) != len(vehicle_ids)
            or not set(vehicle_ids) <= catalog.keys()
        ):
            raise ValueError(
                "vehicle_ids must be a nonempty unique subset of the GTFS duty catalog"
            )
    specs = tuple(catalog[key] for key in sorted(vehicle_ids))
    start_s = min(spec.availability_start_s for spec in specs)
    if start_s >= end_s:
        raise ValueError("Selected duties all start at or after the simulation horizon")
    task_by_id = {task.task_id: task for task in reconstruction.tasks}
    rows = [
        row
        for row in reconstruction.assignments.rows
        if row.vehicle.vehicle_id in vehicle_ids and task_by_id[row.task_id].release_s < end_s
    ]
    if not rows:
        raise ValueError("No selected GTFS task is released before end_s")
    ordered = []
    for vehicle_id in sorted(vehicle_ids):
        own = sorted(
            (r for r in rows if r.vehicle.vehicle_id == vehicle_id), key=lambda r: r.order_index
        )
        ordered.extend(
            AssignmentRow(vehicle=r.vehicle, order_index=i, task_id=r.task_id)
            for i, r in enumerate(own)
        )
    task_ids = tuple(sorted(row.task_id for row in rows))
    assignment = AssignmentPlan(
        assignment_plan_id=stable_id(
            "assignment_plan",
            {
                "source": reconstruction.assignments.assignment_plan_id,
                "tasks": task_ids,
            },
        ),
        rows=tuple(ordered),
    )
    resource = FleetRuntimeResource(
        fleet_id=specs[0].key.fleet_id,
        demand_artifact=reconstruction.reference,
        supply_artifact=reconstruction.reference,
        selected_task_ids=task_ids,
        selected_vehicle_ids=tuple(sorted(vehicle_ids)),
        assignment_plan=assignment,
        assumptions=(
            "inferred_vehicle_duties",
            "selected_gtfs_duty_catalog",
            "uncalibrated_road_speeds",
        ),
    )
    return resource, start_s


def service_locations(
    environment, support, *, coordinates=None, weighting="population", profile_id
):
    """Resolve editable location inputs and raw generator weights.

    coordinates: optional rows {location_id, x, y, source_crs, weight}.
    Otherwise use GTFS stops with exact-cell population or uniform raw weights.
    """
    if coordinates is not None:
        if weighting != "custom":
            raise ValueError("Set weighting='custom' when supplying coordinates and weights")
        resolver = LocationResolver(
            environment=environment.reference,
            snapper=environment.snapping,
            routing=environment.routing,
            routing_profile_id=profile_id,
        )
        locations = tuple(
            resolver.by_coordinates(
                location_id=row["location_id"], x=row["x"], y=row["y"], source_crs=row["source_crs"]
            )
            for row in coordinates
        )
        weights = tuple(
            LocationWeight(location_id=row["location_id"], weight=row["weight"])
            for row in coordinates
        )
    else:
        if weighting not in ("population", "uniform"):
            raise ValueError("weighting must be population, uniform, or custom with coordinates")
        if support is None:
            raise ValueError("GTFS support is required when coordinates are not supplied")
        locations = tuple(sorted(support.locations, key=lambda loc: loc.location_id))
        population = (
            dict(
                zip(
                    environment.population_features.cell_id.astype(str),
                    environment.population_features.residents,
                )
            )
            if weighting == "population"
            else {}
        )
        weights = []
        for loc in locations:
            raw = 1.0
            if weighting == "population":
                point = Point(loc.snapped_x, loc.snapped_y)
                indices = environment.grid_cells.sindex.query(point, predicate="intersects")
                cell_ids = sorted(
                    str(row.cell_id)
                    for row in environment.grid_cells.iloc[indices].itertuples()
                    if row.geometry.covers(point)
                )
                raw = float(population.get(cell_ids[0], 0.0)) if cell_ids else 0.0
            weights.append(LocationWeight(location_id=loc.location_id, weight=raw))
        weights = tuple(weights)
    if len({loc.location_id for loc in locations}) != len(locations):
        raise ValueError("Location IDs must be unique")
    if not weights or math.fsum(w.weight for w in weights) <= 0:
        raise ValueError("Demand location weights must have positive total mass")
    return locations, weights


def generated_resources(
    fleet,
    locations,
    weights,
    *,
    initial_location_ids=None,
    depot_location_ids=None,
    od_weights=None,
    max_od_pairs=10_000,
):
    """Bind the editable backend fleet config to concrete spatial and supply inputs."""
    if fleet.supply.source != "generated" or fleet.demand.source != "generator":
        raise ValueError("This helper requires generated supply and generator demand")
    positive = tuple(w for w in weights if w.weight > 0)
    if not positive:
        raise ValueError("At least one positive demand location weight is required")
    n = fleet.supply.catalog_size
    if initial_location_ids is None:
        initial_location_ids = tuple(positive[i % len(positive)].location_id for i in range(n))
    if len(initial_location_ids) != n:
        raise ValueError("Supply initial_location_ids must have exactly catalog_size entries")
    if fleet.supply.depot_policy is not None and depot_location_ids is None:
        raise ValueError("Specify one depot_location_id for each vehicle with a depot policy")
    pairs = ()
    if fleet.demand.structure == "od":
        if od_weights is None:
            pair_count = len(positive) * (len(positive) - 1)
            if not 0 < pair_count <= max_od_pairs:
                raise ValueError(
                    f"OD product has {pair_count} pairs; provide sparse od_weights or raise max_od_pairs"
                )
            pairs = tuple(
                OdWeight(
                    origin_location_id=a.location_id,
                    destination_location_id=b.location_id,
                    weight=a.weight * b.weight,
                )
                for a in positive
                for b in positive
                if a.location_id != b.location_id
            )
        else:
            if len(od_weights) > max_od_pairs:
                raise ValueError("od_weights exceeds max_od_pairs")
            pairs = tuple(OdWeight.model_validate(row) for row in od_weights)
    elif od_weights is not None:
        raise ValueError("Location demand does not accept od_weights")
    weight_ref = (
        fleet.demand.parameters.location_weights_ref or fleet.demand.parameters.od_weights_ref
    )
    return FleetRuntimeResource(
        fleet_id=fleet.fleet_id,
        weight_source_id=weight_ref,
        initial_locations_source_id=fleet.supply.initial_locations_ref,
        locations=tuple(locations),
        generated_initial_location_ids=tuple(initial_location_ids),
        generated_depot_location_ids=tuple(depot_location_ids or ()),
        location_weights=tuple(weights) if fleet.demand.structure == "location" else (),
        od_weights=pairs,
        assumptions=(
            "declared_service_location_and_weight_inputs",
            "synthetic_poisson_demand",
            "uncalibrated_road_speeds",
        ),
    )


def scenario_bundle(environment, clock, fleet, resource, *, replications, master_seed):
    scenario = ScenarioConfig(
        schema_version="2.0",
        environment_id=environment.reference.artifact_id,
        clock=clock,
        fleets=(fleet,),
        replications=replications,
        master_seed=master_seed,
        joint_scenario_model={"kind": "independent_conditional_environment"},
    )
    return ScenarioResourceBundle(scenario=scenario, fleets=(resource,))


def reporting_edges(start_s, end_s, bin_width_s):
    """Half-open bins; retain an exact shorter final bin when needed."""
    if (
        not all(math.isfinite(x) for x in (start_s, end_s, bin_width_s))
        or bin_width_s <= 0
        or end_s <= start_s
    ):
        raise ValueError("Require finite start_s < end_s and positive bin_width_s")
    count = math.ceil((end_s - start_s) / bin_width_s)
    if count > 100_000:
        raise ValueError("Reporting configuration exceeds 100,000 bins")
    return tuple(start_s + i * bin_width_s for i in range(count)) + (end_s,)


def allocate_exposure(application, environment, simulation, *, bin_width_s, active_movement_kinds):
    stored = SimulationArtifactReader(application.artifact_root, simulation).artifact
    clock = stored.manifest.scientific_identity.resolved_config["simulation"]["scenario"]["clock"]
    config = ExposureConfig(
        schema_version="2.0",
        simulation_id=simulation.artifact_id,
        sensing_geometry_id=environment.metadata.sensing_hash,
        bin_edges_s=reporting_edges(clock["observation_start_s"], clock["end_s"], bin_width_s),
        active_movement_kinds=tuple(active_movement_kinds),
    )
    return application.allocate_exposure(environment, simulation, config)


def utility_weights(environment, root, exposure, *, kind):
    if kind == "uniform":
        return UtilityWeightResource(
            weights_id="uniform_spatial_duration_temporal",
            kind="uniform_spatial_duration_temporal",
            provenance="Uniform cells times actual bin duration",
        )
    if kind != "population":
        raise ValueError("Utility weight kind must be uniform or population")
    axes = ExposureArtifactReader(root).axes(exposure)
    cfg = axes["artifact"].manifest.scientific_identity.resolved_config["exposure"]
    edges = cfg["bin_edges_s"]
    durations = dict(zip(axes["time_bin_ids"], np.diff(edges), strict=True))
    values = tuple(
        CellTimeWeight(
            cell_id=str(row.cell_id), time_bin_id=b, raw_weight=float(row.residents) * float(d)
        )
        for row in environment.population_features.itertuples()
        if row.residents > 0
        for b, d in durations.items()
    )
    return UtilityWeightResource(
        weights_id=stable_id(
            "weights",
            {"environment": environment.reference.content_hash, "bins": edges, "kind": kind},
        ),
        kind="resolved_cell_time",
        missing_policy="zero",
        values=values,
        provenance="Supplied 2024 population times actual reporting-bin duration; zero elsewhere",
    )


def portfolio_config(
    root,
    exposure,
    weights,
    *,
    count_step=1,
    count_levels=None,
    utility_kind="exponential_saturation",
    saturation_s=300.0,
    sampling_rounds=100,
    sampling_seed=31415,
    unit_cost_minor=100000,
    budgets_minor=None,
):
    axes = ExposureArtifactReader(root).axes(exposure)
    fleets = sorted({v.fleet_id for v in axes["vehicle_keys"]})
    if len(fleets) != 1:
        raise ValueError("These notebooks analyze one fleet; use PortfolioConfig for joint fleets")
    fleet_id = fleets[0]
    n = len(axes["vehicle_keys"])
    if count_levels is None:
        counts = {"min_count": 0, "max_count": n, "step": count_step, "include_max": True}
    else:
        counts = {"count_levels": tuple(count_levels)}
    return PortfolioConfig(
        schema_version="2.0",
        exposure_id=exposure.artifact_id,
        utility={
            "kind": utility_kind,
            "saturation_s": saturation_s,
            "weights_ref": weights.weights_id,
        },
        count_enumeration={"fleets": {fleet_id: counts}},
        budgets={
            "levels_minor": (
                tuple(budgets_minor) if budgets_minor is not None else (unit_cost_minor * n,)
            )
        },
        costs={
            "unit": "CHF",
            "minor_unit_scale": 100,
            "by_fleet_minor": {fleet_id: unit_cost_minor},
        },
        sampling_rounds=sampling_rounds,
        sampling_seed=sampling_seed,
        sampling_design="joint_replication_uniform_vehicle",
        comparison_resolution={"mean_utility": 1e-12, "std_utility": 1e-12},
    )


def operations_tables(simulation, validated):
    """Summarize service tasks due before the horizon, including warm-up tasks."""
    inputs = {
        replication.replication_id: {task.task_id: task for task in replication.tasks}
        for replication in validated.replications
    }
    summary, vehicles = [], []
    for result in simulation.results:
        service, future_scheduled = [], 0
        for row in result.task_outcomes:
            if row.kind != "service":
                continue
            target = inputs[result.replication_id][row.task_id].steps[0].scheduled_time_s
            if target is not None and target >= result.end_s:
                future_scheduled += 1
            else:
                service.append(row)
        completed = [row for row in service if row.status.value == "completed"]
        waits = [row.assignment_wait_s for row in service if row.assignment_wait_s is not None]
        summary.append(
            {
                "replication_id": result.replication_id,
                "service_tasks": len(service),
                "future_scheduled_tasks_excluded": future_scheduled,
                "completed_service_tasks": len(completed),
                "completion_fraction": len(completed) / len(service) if service else np.nan,
                "mean_assigned_wait_s": float(np.mean(waits)) if waits else np.nan,
                "processed_heap_events": result.processed_heap_events,
            }
        )
        for vehicle in result.vehicle_outcomes:
            vehicles.append(
                {
                    "replication_id": result.replication_id,
                    "vehicle_id": vehicle.vehicle.vehicle_id,
                    "entered_at_s": vehicle.entered_at_s,
                    "exited_at_s": vehicle.exited_at_s,
                }
            )
    return pd.DataFrame(summary), pd.DataFrame(vehicles)


def exposure_catalog(root, exposure):
    axes = ExposureArtifactReader(root).axes(exposure)
    return pd.DataFrame(
        [{"fleet_id": v.fleet_id, "vehicle_id": v.vehicle_id} for v in axes["vehicle_keys"]]
    )


@dataclass
class SensingView:
    cells: pd.DataFrame
    time_series: pd.DataFrame
    vehicle_ids: tuple[str, ...]
    replication_ids: tuple[str, ...]
    bin_indices: tuple[int, ...]
    statistic: str
    exposure_id: str


def sensing_view(
    root,
    exposure,
    *,
    vehicle_ids=None,
    vehicle_count=None,
    selection_seed=7,
    replication_ids=None,
    bin_indices=None,
    statistic="mean",
    max_accumulator_rows=1_000_000,
):
    """Stream a sparse selection; sum vehicles and bins inside each replication first.

    None/None selects the full catalog; [] or count=0 selects the empty set.
    count sampling is a display-only uniform subset and uses an independent RNG.
    """
    reader = ExposureArtifactReader(root)
    axes = reader.axes(exposure)
    catalog = {key.vehicle_id: key for key in axes["vehicle_keys"]}
    if len(catalog) != len(axes["vehicle_keys"]):
        raise ValueError("Vehicle IDs are ambiguous across fleets; use a single-fleet exposure")
    if vehicle_ids is not None and vehicle_count is not None:
        raise ValueError("Choose vehicle_ids or vehicle_count, not both")
    if vehicle_count is not None:
        if (
            isinstance(vehicle_count, bool)
            or not isinstance(vehicle_count, int)
            or not 0 <= vehicle_count <= len(catalog)
        ):
            raise ValueError("vehicle_count is outside the full catalog")
        vehicle_ids = (
            np.random.default_rng(selection_seed)
            .choice(sorted(catalog), size=vehicle_count, replace=False)
            .tolist()
        )
    selected = tuple(sorted(catalog if vehicle_ids is None else vehicle_ids))
    if len(set(selected)) != len(selected) or not set(selected) <= catalog.keys():
        raise ValueError("Unknown or duplicate selected vehicle IDs")
    reps = tuple(axes["replication_ids"] if replication_ids is None else replication_ids)
    if not reps or len(set(reps)) != len(reps) or not set(reps) <= set(axes["replication_ids"]):
        raise ValueError("Select a nonempty unique subset of complete replication IDs")
    if statistic not in ("mean", "std", "realization"):
        raise ValueError("statistic must be mean, std, or realization")
    if statistic == "realization" and len(reps) != 1:
        raise ValueError("realization requires exactly one replication_id")
    if statistic == "std" and len(reps) < 2:
        raise ValueError("Sample SD requires at least two complete replications")
    all_bins = axes["time_bin_ids"]
    indices = tuple(range(len(all_bins))) if bin_indices is None else tuple(bin_indices)
    if (
        not indices
        or len(set(indices)) != len(indices)
        or any(type(i) is not int or not 0 <= i < len(all_bins) for i in indices)
    ):
        raise ValueError("bin_indices must be nonempty unique zero-based reporting-bin indices")
    selected_bins = {all_bins[i] for i in indices}
    per_cell, per_bin = {}, {}
    if selected:
        _, chunks = reader.read_sparse_chunks(
            exposure, replication_ids=reps, vehicle_keys=[catalog[v] for v in selected]
        )
        for chunk in chunks:
            for row in chunk.rows:
                if row.time_bin_id in selected_bins:
                    key = (row.replication_id, row.cell_id)
                    per_cell[key] = per_cell.get(key, 0.0) + row.duration_s
                    key = (row.replication_id, row.time_bin_id)
                    per_bin[key] = per_bin.get(key, 0.0) + row.duration_s
                    if len(per_cell) + len(per_bin) > max_accumulator_rows:
                        raise ValueError(
                            "Map aggregation exceeds max_accumulator_rows; narrow the selection"
                        )

    def reduce_values(values):
        return float(np.std(values, ddof=1)) if statistic == "std" else float(np.mean(values))

    cells = pd.DataFrame(
        {
            "cell_id": axes["cell_ids"],
            "duration_s": [
                reduce_values([per_cell.get((rep, cell), 0.0) for rep in reps])
                for cell in axes["cell_ids"]
            ],
        }
    )
    edges = axes["artifact"].manifest.scientific_identity.resolved_config["exposure"]["bin_edges_s"]
    series = pd.DataFrame(
        [
            {
                "bin_index": i,
                "start_s": edges[i],
                "end_s": edges[i + 1],
                "duration_s": reduce_values([per_bin.get((rep, all_bins[i]), 0.0) for rep in reps]),
            }
            for i in sorted(indices)
        ]
    )
    return SensingView(cells, series, selected, reps, indices, statistic, exposure.artifact_id)


def plot_sensing(
    environment,
    view,
    *,
    cmap="YlOrRd",
    vmax=None,
    figsize=(10, 8),
    show_roads=True,
    zoom_to_sensed=False,
):
    import matplotlib.pyplot as plt

    if vmax is not None and (not math.isfinite(vmax) or vmax <= 0):
        raise ValueError("vmax must be finite and positive")
    grid = environment.grid_cells[["cell_id", "geometry"]].copy()
    grid["cell_id"] = grid.cell_id.astype(str)
    grid = grid.merge(view.cells, on="cell_id", how="left", validate="one_to_one")
    if grid.duration_s.isna().any():
        raise ValueError("Environment grid and exposure axes differ")
    fig, ax = plt.subplots(figsize=figsize, constrained_layout=True)
    grid.plot(ax=ax, color="#f2f4f7", linewidth=0)
    if show_roads:
        xmin, ymin, xmax, ymax = environment.boundary.total_bounds
        environment.road_edges.cx[xmin:xmax, ymin:ymax].plot(
            ax=ax, color="#cbd0d6", linewidth=0.25, zorder=1
        )
    positive = grid[grid.duration_s > 0]
    scale_max = float(grid.duration_s.max()) if vmax is None else vmax
    if scale_max > 0 and not positive.empty:
        positive.plot(
            ax=ax,
            column="duration_s",
            cmap=cmap,
            vmin=0,
            vmax=scale_max,
            linewidth=0,
            legend=True,
            legend_kwds={"label": f"{view.statistic} sensing duration (s)", "shrink": 0.75},
            zorder=2,
        )
    else:
        ax.text(0.02, 0.02, "No sensing duration in this selection", transform=ax.transAxes)
    environment.boundary.boundary.plot(ax=ax, color="#27364b", linewidth=0.8, zorder=3)
    if zoom_to_sensed and not positive.empty:
        xmin, ymin, xmax, ymax = positive.total_bounds
        padding = max(500.0, 0.1 * max(xmax - xmin, ymax - ymin))
        ax.set_xlim(xmin - padding, xmax + padding)
        ax.set_ylim(ymin - padding, ymax + padding)
    bin_label = (
        ",".join(map(str, view.bin_indices))
        if len(view.bin_indices) <= 8
        else f"{len(view.bin_indices)} selected"
    )
    ax.set_title(
        f"Lausanne | {len(view.vehicle_ids)} vehicles | {view.statistic}\n"
        f"{len(view.replication_ids)} replications; bins {bin_label}"
    )
    ax.set_xlabel(f"Easting (m), {environment.metadata.working_crs}")
    ax.set_ylabel("Northing (m)")
    ax.ticklabel_format(useOffset=False, style="plain")
    return fig


def portfolio_statistics(root, analysis):
    reader = PortfolioAnalysisArtifactReader(root, analysis)
    frame = reader.read("portfolio_statistics").to_pandas()
    frame["vehicle_count"] = frame.count_by_fleet_json.map(
        lambda value: sum(json.loads(value).values())
    )
    return frame.sort_values("vehicle_count").reset_index(drop=True)


def plot_utility(statistics):
    import matplotlib.pyplot as plt

    if statistics.empty:
        raise ValueError("No feasible portfolios; check count levels and budget")
    x = statistics.vehicle_count.to_numpy()
    mean = statistics.utility_mean.to_numpy()
    sd = statistics.utility_sample_std.to_numpy(dtype=float)
    variance = statistics.utility_sample_variance.to_numpy(dtype=float)
    fig, axes = plt.subplots(2, 1, figsize=(10, 7), sharex=True, constrained_layout=True)
    axes[0].plot(x, mean, "o-", color="#1464a0", label="Mean utility")
    if np.isfinite(sd).any():
        axes[0].fill_between(
            x,
            mean - sd,
            mean + sd,
            alpha=0.25,
            color="#1464a0",
            label="Mean ± sample SD (not a confidence interval)",
        )
        axes[1].plot(x, variance, "o-", color="#ad6417")
        axes[1].fill_between(x, 0, variance, color="#ad6417", alpha=0.25)
    else:
        axes[1].text(0.05, 0.5, "J=1: variance and SD are undefined", transform=axes[1].transAxes)
    axes[0].set_ylabel("Sensing utility")
    axes[0].legend(fontsize=9)
    axes[1].set_ylabel("Sample variance (utility²)")
    axes[1].set_xlabel("Sensor-equipped vehicles in the fixed operational catalog")
    axes[0].set_title(
        f"Utility versus vehicle count | R={statistics.replications_R.iloc[0]}, J={statistics.sampling_rounds_J.iloc[0]}"
    )
    for ax in axes:
        ax.grid(alpha=0.2)
    return fig


def export_run(root, *, project_root, references, configurations, timings, tables, figures):
    """Create a new timestamped report directory; never overwrite a prior report."""
    target = Path(root) / "reports" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    target.mkdir(parents=True, exist_ok=False)
    package_root = Path(project_root) / "src" / "mobile_sensing"
    hashes = {
        str(p.relative_to(package_root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(package_root.rglob("*.py"))
    }
    record = {
        "python": sys.version,
        "executable": sys.executable,
        "platform": platform.platform(),
        "source_file_sha256": hashes,
        "references": references,
        "configurations": configurations,
        "timings_s": timings,
    }
    (target / "run.json").write_text(canonical_json_text(record) + "\n", encoding="utf-8")
    for name, table in tables.items():
        table.to_csv(target / f"{name}.csv", index=False)
    for name, figure in figures.items():
        figure.savefig(target / f"{name}.png", dpi=180, bbox_inches="tight")
        figure.savefig(target / f"{name}.svg", bbox_inches="tight")
    return target


class StageTimer:
    """Notebook timing collector: measured wall time including backend publication."""

    def __init__(self):
        self.seconds = {}

    def call(self, stage, function, *args, **kwargs):
        start = perf_counter()
        result = function(*args, **kwargs)
        self.seconds[stage] = perf_counter() - start
        print(f"{stage}: {self.seconds[stage]:.3f} s")
        return result
