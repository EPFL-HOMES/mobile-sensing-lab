"""Current-project notebook adapters; scientific work stays in the formal services."""

import numpy as np
import pandas as pd
import geopandas as gpd
from shapely.ops import substring

from mobile_sensing.api.fleet_queries import fleet_summary
from mobile_sensing.api.models import MatrixQueryRequest
from mobile_sensing.api.queries import (
    query_environment,
    query_matrix,
    query_portfolio_frontier,
)
from mobile_sensing.application.example_bundle import install_example
from mobile_sensing.application.project_models import ProjectConfig, PortfolioEditor
from mobile_sensing.application.run_pipeline import read_named_record, run_project, run_analysis
from mobile_sensing.exposure import ExposureArtifactReader
from mobile_sensing.jobs.models import JobStoreLimits
from mobile_sensing.portfolio import PortfolioAnalysisArtifactReader
from mobile_sensing.simulation.storage import SimulationArtifactReader


class NotebookCancellation:
    """Synchronous notebook execution is interrupted through Kernel Interrupt."""

    def raise_if_cancelled(self):
        return None

    def is_cancelled(self):
        return False


class NotebookProgress:
    def __init__(self):
        self.phase = None

    def update(self, *, phase, **values):
        if phase != self.phase:
            print(phase)
            self.phase = phase


def open_tutorial(root):
    """Install the local checksummed bundle once, without network or UI metadata."""
    bundle = install_example(
        root,
        cancellation=NotebookCancellation(),
        progress=NotebookProgress(),
        generate_reports=False,
    )
    runs = [read_named_record(root, item, "studio_run") for item in bundle.config.linked_run_ids]
    run = next(item for item in runs if item.realization_source_run_id is None)
    analyses = [
        read_named_record(root, item, "studio_analysis")
        for item in bundle.config.linked_analysis_ids
    ]
    analysis = next(item for item in analyses if item.source_run_id == run.run_id)
    return bundle, run, analysis


def simulation_result(root, config, source, *, recompute=None, options):
    config = ProjectConfig.model_validate(config)
    fields = ("environment", "prepared_environment", "fleets", "simulation")
    changed = source is None or any(
        getattr(config, key) != getattr(source.config, key) for key in fields
    )
    if recompute is None:
        recompute = changed
    if not recompute:
        if changed:
            raise ValueError("Simulation settings changed. Set RUN_SIMULATION=True and rerun.")
        return source
    return run_project(
        root,
        config,
        name="Notebook tutorial simulation",
        source_revision_id=None,
        options=options,
        cancellation=NotebookCancellation(),
        progress=NotebookProgress(),
    )


def analysis_result(root, editor, source, *, recompute=None, options):
    from mobile_sensing.application.temporal_authoring import resolve_portfolio

    editor = PortfolioEditor.model_validate(editor)
    run = read_named_record(root, editor.source_run_id, "studio_run")
    editor = resolve_portfolio(editor, run.vehicle_counts)
    changed = source is None or editor != source.config
    if recompute is None:
        recompute = changed
    if not recompute:
        if changed:
            raise ValueError("Portfolio source or settings changed. Set RUN_PORTFOLIO=True.")
        return source
    return run_analysis(
        root,
        editor,
        name="Notebook tutorial portfolio",
        source_revision_id=None,
        options=options,
        cancellation=NotebookCancellation(),
        progress=NotebookProgress(),
    )


def fleet_results(root, run, fleet_id):
    """Use the same bounded mean projections as Fleet results in the web app."""
    if fleet_id not in run.vehicle_counts:
        raise ValueError("The fleet is absent from this simulation")
    limits = JobStoreLimits()
    summary = fleet_summary(root, run.exposure.artifact_id, fleet_id=fleet_id, limits=limits)
    axes = ExposureArtifactReader(root).axes(run.exposure)
    vehicles = [key for key in axes["vehicle_keys"] if key.fleet_id == fleet_id]
    matrix = query_matrix(
        root,
        MatrixQueryRequest(
            resource_id=run.exposure.artifact_id,
            kind="operational_aggregate",
            statistic="mean",
            vehicle_keys=vehicles,
            temporal_aggregation="sum",
        ),
        limits,
    )
    environment = query_environment(root, run.config.prepared_environment.artifact)
    grid = environment.grid_cells[["cell_id", "geometry"]].copy()
    values = {row["cell_id"]: row["value"] for row in matrix["values"]}
    grid["mean_sensing_minutes"] = grid.cell_id.map(values).fillna(0) / 60
    grid["mean_per_vehicle_minutes"] = grid.mean_sensing_minutes / run.vehicle_counts[fleet_id]
    time = pd.DataFrame(summary["time_summary"]).sort_values("start_s")
    durations = {row["time_bin_id"]: row["value"] for row in matrix["time_summary"]}
    time["mean_sensing_hours"] = time.time_bin_id.map(durations) / 3600
    return summary, grid, time, environment


def sampled_vehicle_sensing(root, run, fleet_id, *, count=10, seed=20260116):
    """Return deterministic random vehicles and their hourly/full-day exposure views."""

    limits = JobStoreLimits()
    axes = ExposureArtifactReader(root).axes(run.exposure)
    catalog = sorted(
        (key for key in axes["vehicle_keys"] if key.fleet_id == fleet_id),
        key=lambda key: key.vehicle_id,
    )
    if count > len(catalog):
        raise ValueError("Requested display sample exceeds the physical fleet catalog")
    selected_indices = np.random.default_rng(seed).choice(len(catalog), count, replace=False)
    selected = tuple(catalog[int(index)] for index in sorted(selected_indices))
    environment = query_environment(root, run.config.prepared_environment.artifact)
    base_grid = environment.grid_cells[["cell_id", "geometry"]].copy()
    spatial, temporal = {}, []
    for key in selected:
        matrix = query_matrix(
            root,
            MatrixQueryRequest(
                resource_id=run.exposure.artifact_id,
                kind="vehicle_exposure",
                statistic="mean",
                vehicle_keys=(key,),
                temporal_aggregation="bins",
            ),
            limits,
        )
        by_cell = {}
        for row in matrix["values"]:
            by_cell[row["cell_id"]] = by_cell.get(row["cell_id"], 0.0) + row["value"]
        grid = base_grid.copy()
        grid["sensing_minutes"] = grid.cell_id.map(by_cell).fillna(0) / 60
        spatial[key.vehicle_id] = grid
        by_bin = {row["time_bin_id"]: row["value"] / 60 for row in matrix["time_summary"]}
        start_parts = [float(part) for part in run.config.simulation.start_time.split(":")]
        observation_start_h = sum(value / (60**index) for index, value in enumerate(start_parts))
        bin_hours = run.config.simulation.temporal_resolution_minutes / 60
        for index, time_bin_id in enumerate(axes["time_bin_ids"]):
            temporal.append(
                {
                    "vehicle_id": key.vehicle_id,
                    "time_bin_id": time_bin_id,
                    "start_hour": observation_start_h + index * bin_hours,
                    "end_hour": observation_start_h + (index + 1) * bin_hours,
                    "sensing_minutes": by_bin.get(time_bin_id, 0.0),
                }
            )
    return tuple(key.vehicle_id for key in selected), spatial, pd.DataFrame(temporal), environment


def vehicle_trajectory(root, run, fleet_id, vehicle_id):
    """Recover ordered metric line segments for one vehicle in the sole replication."""

    reader = SimulationArtifactReader(root, run.simulation)
    if len(reader.replication_ids) != 1:
        raise ValueError("Trajectory tutorial requires exactly one simulation replication")
    rows = reader.read_movements(reader.replication_ids)
    rows = rows.loc[(rows.fleet_id == fleet_id) & (rows.vehicle_id == vehicle_id)].sort_values(
        ["start_s", "movement_index"]
    )
    environment = query_environment(root, run.config.prepared_environment.artifact)
    edges = environment.road_edges.set_index("edge_id").geometry
    records = []
    for row in rows.itertuples(index=False):
        geometry = edges.get(row.edge_id)
        if geometry is None:
            raise ValueError(f"Trajectory references unknown edge {row.edge_id}")
        segment = substring(
            geometry,
            row.edge_start_fraction,
            row.edge_end_fraction,
            normalized=True,
        )
        records.append(
            {
                "start_s": row.start_s,
                "end_s": row.end_s,
                "movement_kind": row.movement_kind,
                "geometry": segment,
            }
        )
    return (
        gpd.GeoDataFrame(records, geometry="geometry", crs=environment.road_edges.crs),
        environment,
    )


def portfolio_results(root, analysis, *, budget=None):
    reader = PortfolioAnalysisArtifactReader(root, analysis.frontier)
    budgets = reader.read("budget_levels").to_pylist()
    if budget is None:
        selected = max(budgets, key=lambda item: item["budget_minor"])
    else:
        selected = next(
            (item for item in budgets if item["budget_minor"] / item["minor_unit_scale"] == budget),
            None,
        )
        if selected is None:
            raise ValueError("Choose one of the saved budget levels")
    result = query_portfolio_frontier(
        root,
        analysis.frontier.artifact_id,
        budget_id=selected["budget_id"],
        limits=JobStoreLimits(),
    )
    frame = pd.DataFrame(result["points"])
    counts = pd.DataFrame(frame.count_by_fleet.tolist()).add_prefix("sensors_")
    frame = pd.concat([frame, counts], axis=1)
    return result, frame


def selected_frontier_points(frontiers):
    """Show budget-wise mean optima and the largest-budget lower-tail extreme."""
    result, seen = [], set()
    largest = max(view["budget"]["budget_minor"] for view, _ in frontiers)
    for view, frame in frontiers:
        candidates = frame[frame.nondominated.fillna(False)]
        if candidates.empty:
            continue
        objectives = [("utility_mean", "utility_p05")]
        if view["budget"]["budget_minor"] == largest:
            objectives.append(("utility_p05", "utility_mean"))
        for primary, secondary in objectives:
            point = (
                candidates.sort_values(
                    [primary, secondary, "total_cost", "portfolio_id"],
                    ascending=[False, False, True, True],
                )
                .iloc[0]
                .to_dict()
            )
            if point["portfolio_id"] in seen:
                continue
            seen.add(point["portfolio_id"])
            point["budget"] = view["budget"]["budget_minor"] / view["budget"]["minor_unit_scale"]
            result.append(point)
    return result


def portfolio_map(root, analysis, point, environment):
    matrix = query_matrix(
        root,
        MatrixQueryRequest(
            resource_id=analysis.frontier.artifact_id,
            kind="portfolio_summary",
            statistic="mean",
            portfolio_id=point["portfolio_id"],
            temporal_aggregation="sum",
        ),
        JobStoreLimits(),
    )
    grid = environment.grid_cells[["cell_id", "geometry"]].copy()
    values = {row["cell_id"]: row["value"] for row in matrix["values"]}
    grid["mean_sensing_minutes"] = grid.cell_id.map(values).fillna(0) / 60
    return grid
