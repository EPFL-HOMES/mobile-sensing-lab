"""Audit a real city example before packaging; never manufacture results."""

import argparse
import json
import platform
import zipfile
from importlib.metadata import version
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

from mobile_sensing.api.queries import ArtifactCatalog, query_environment
from mobile_sensing.application.example_build import digest_file, feasible_portfolio_count
from mobile_sensing.application.run_models import RunView, AnalysisView
from mobile_sensing.contracts import canonical_json_text


def audit_example(root):
    root = Path(root).resolve()
    run = RunView.model_validate_json((root / "example-run.json").read_bytes())
    analysis_paths = [root / "example-analysis.json"]
    if (root / "example-analysis-std.json").is_file():
        analysis_paths.append(root / "example-analysis-std.json")
    analyses = tuple(AnalysisView.model_validate_json(path.read_bytes()) for path in analysis_paths)
    from mobile_sensing.application.example_build import ROUTE_IDS

    fleet_ids = {fleet.fleet_id for fleet in run.config.fleets}
    is_lausanne = fleet_ids == {"bus", "postal", "taxi"}
    bus_configs = [fleet for fleet in run.config.fleets if fleet.fleet_id == "bus"]
    if is_lausanne:
        postal = next(fleet for fleet in run.config.fleets if fleet.fleet_id == "postal")
        if postal.supply.service_area_mode != "auto" or postal.supply.auto_service_area_count != 4:
            raise ValueError("Lausanne requires four frozen Postal Auto service areas")
        if bus_configs[0].demand.route_ids != ROUTE_IDS:
            raise ValueError("Lausanne requires lines 1, 9, 21, 33 and 54 in one Bus fleet")
    elif fleet_ids != {"taxi"}:
        raise ValueError("San Francisco requires exactly one Taxi fleet")
    expected_saturation = 5
    expected_risks = {"p05"} if is_lausanne else {"p05", "std"}
    if (
        {item.config.risk_metric for item in analyses} != expected_risks
        or any(
            item.config.spatial_weight != ("population" if is_lausanne else "uniform")
            for item in analyses
        )
        or any(item.config.saturation_minutes != expected_saturation for item in analyses)
        or any(item.config.utility_temporal_resolution_minutes != 1440 for item in analyses)
    ):
        raise ValueError("Example portfolio utility/risk settings disagree with the city design")
    catalog = ArtifactCatalog(root)
    located = {}

    def artifact(reference):
        if reference.artifact_id not in located:
            located[reference.artifact_id] = catalog.locate(reference.artifact_id)
        return located[reference.artifact_id]

    def table(reference, name):
        source = artifact(reference)
        manifest = next(t for t in source.manifest.tables if t.name == name)
        paths = sorted((source.directory / manifest.relative_path).glob("*.parquet"))
        return (
            pd.concat([pq.read_table(p).to_pandas() for p in paths], ignore_index=True)
            if paths
            else pd.DataFrame()
        )

    def require(condition, message):
        if not condition:
            raise ValueError("Example audit failed: " + message)

    expected_replications = 50 if is_lausanne else 10
    expected_sampling_runs = 200 if is_lausanne else 100
    require(
        run.replications == expected_replications,
        f"{expected_replications} complete joint replications",
    )
    require(
        all(
            item.sampling_runs == expected_sampling_runs
            and item.count_portfolios == feasible_portfolio_count(item.config)
            for item in analyses
        ),
        "portfolio R/J/count design",
    )
    resolution = artifact(run.resolution).manifest.scientific_identity.resolved_config
    reports, clock = resolution["reports"], resolution["clock"]
    require(
        clock["observation_start_s"] == 0
        and clock["end_s"] == 86400
        and (clock["simulation_start_s"] < 0 if is_lausanne else clock["simulation_start_s"] == 0),
        "natural-day window and preceding execution",
    )
    environment = query_environment(root, run.config.prepared_environment.artifact)
    cell_count = len(environment.grid_cells)
    require(cell_count == 17553 if is_lausanne else cell_count > 0, "complete zero-inclusive grid")
    features = table(run.config.prepared_environment.features, "grid_features")
    population = features[features.feature == "population"]
    if is_lausanne:
        require(
            len(population) == 17553 and population.value.sum() == 315757,
            "full population axis and mass",
        )
    locations = {
        row.location_id: json.loads(row.location_json)
        for row in table(run.resolution, "resolved_locations").itertuples()
    }
    tasks = table(run.resolution, "realized_tasks")
    rep_ids = sorted(tasks.replication_id.unique())
    leg_pairs, per_route, unreachable = set(), {}, []
    if is_lausanne:
        bus = [
            json.loads(row.task_json)
            for row in tasks[
                (tasks.replication_id == rep_ids[0])
                & tasks.fleet_id.isin([f.fleet_id for f in bus_configs])
            ].itertuples()
        ]
        gtfs_input = bus_configs[0].demand.input_id
        archive_path = next((root / "inputs" / gtfs_input).glob("source.*"))
        with zipfile.ZipFile(archive_path) as archive:
            raw_stops = pd.read_csv(
                archive.open("stop_times.txt"), dtype=str, keep_default_na=False
            )
            raw_trips = pd.read_csv(archive.open("trips.txt"), dtype=str, keep_default_na=False)
        expected = {}
        for trip, group in raw_stops.groupby("trip_id", sort=False):
            expected[trip] = sorted(group.stop_sequence.tolist(), key=int)
        route_by_trip = dict(zip(raw_trips.trip_id, raw_trips.route_id, strict=True))
        leg_pairs, per_route = set(), {}
        for task in bus:
            if task["kind"] != "service":
                continue
            refs = list(
                dict.fromkeys(ref for step in task["steps"] for ref in step["source_record_refs"])
            )
            first = refs[0].split(":stop_time:", 1)
            service_date = first[0]
            trip = first[1].rsplit(":", 1)[0]
            require(
                [ref.rsplit(":", 1)[1] for ref in refs] == expected[trip],
                f"complete ordered stop sequence for {service_date}/{trip}",
            )
            key = f"{service_date}/{route_by_trip[trip]}"
            report = per_route.setdefault(
                key, {"trips": 0, "source_stops": 0, "task_steps": 0, "legs": 0}
            )
            report["trips"] += 1
            report["source_stops"] += len(refs)
            report["task_steps"] += len(task["steps"])
            for left, right in zip(task["steps"], task["steps"][1:]):
                pair = (
                    locations[left["location_id"]]["node_id"],
                    locations[right["location_id"]]["node_id"],
                )
                leg_pairs.add(pair)
                report["legs"] += 1
        unreachable = [
            pair for pair in sorted(leg_pairs) if not environment.routing.reachable(*pair)
        ]
        require(not unreachable, "all selected timetable stop legs are directed-reachable")
    operations, sensing = {}, {}
    for name, value in (("Full day", run),):
        statuses = table(value.simulation, "replication_status")
        require(
            len(statuses) == expected_replications and statuses.complete.all(),
            "complete operational replication set",
        )
        require(
            set(statuses.replication_id) == set(rep_ids) and statuses.catalog_hash.nunique() == 1,
            "joint IDs and frozen catalog",
        )
        outcomes = table(value.simulation, "task_outcomes")
        services = outcomes[
            (outcomes.kind == "service") & (outcomes.release_s >= 0) & (outcomes.release_s < 86400)
        ]
        if is_lausanne:
            postal_outcomes = services[services.fleet_id == "postal"]
            realized_postal = [
                json.loads(row.task_json) for row in tasks[tasks.fleet_id == "postal"].itertuples()
            ]
            expected_postal = sum(task["kind"] == "service" for task in realized_postal)
            require(
                len(postal_outcomes) == expected_postal
                and expected_postal > 0
                and (postal_outcomes.status == "completed").all(),
                "all realized mandatory Postal tasks complete in every replication",
            )
        counts = (
            services.groupby(["replication_id", "fleet_id", "status"])
            .size()
            .reset_index(name="count")
        )
        operations[name] = {
            "service_outcomes_in_observation": counts.to_dict("records"),
            "pre_run_task_count": int((outcomes.release_s < 0).sum()),
            "depot_returns_completed": int(
                ((outcomes.kind == "depot_return") & (outcomes.status == "completed")).sum()
            ),
            "elapsed_seconds": value.elapsed_seconds,
            "algorithm_versions": artifact(
                value.simulation
            ).manifest.scientific_identity.algorithm_versions,
        }
        bins = table(value.exposure, "time_bins").sort_values("canonical_index")
        require(
            len(bins) == 1440 / value.config.simulation.temporal_resolution_minutes
            and bins.start_s.iloc[0] == 0
            and bins.end_s.iloc[-1] == 86400
            and (bins.end_s - bins.start_s)
            .eq(value.config.simulation.temporal_resolution_minutes * 60)
            .all(),
            "complete observation-only reporting bins at the declared resolution",
        )
        exposure_status = table(value.exposure, "replication_status")
        exposure_artifact = artifact(value.exposure)
        exposure_grid = table(value.exposure, "grid_axis")
        grid_domain = exposure_artifact.manifest.scientific_identity.resolved_config.get(
            "grid_domain"
        )
        require(
            exposure_artifact.manifest.scientific_identity.algorithm_versions.get("grid_domain")
            == "positive-length-road-grid@1"
            and grid_domain
            == {
                "algorithm": "positive-length-road-grid@1",
                "definition": "prepared cells with a positive-length sensing-road intersection",
                "prepared_cell_count": cell_count,
                "eligible_cell_count": len(exposure_grid),
            }
            and 0 < len(exposure_grid) <= cell_count,
            "positive-length road-intersection exposure grid domain",
        )
        require(
            exposure_status.complete.all()
            and exposure_status.conservation_residual_s.abs().max() < 1e-5,
            "exposure conservation",
        )
        require(
            (exposure_status.sensor_active_time_s > 0).all()
            and (exposure_status.sensor_active_stationary_time_s > 0).all()
            and (
                (exposure_status.excluded_depot_time_s > 0).all()
                if is_lausanne
                else (exposure_status.excluded_depot_time_s == 0).all()
            )
            and (
                (exposure_status.excluded_offduty_time_s > 0).all()
                if is_lausanne
                else (exposure_status.excluded_offduty_time_s >= 0).all()
            )
            and (
                exposure_status.sensor_active_time_s
                - exposure_status.sensor_active_moving_time_s
                - exposure_status.sensor_active_stationary_time_s
            )
            .abs()
            .max()
            < 1e-5
            and (
                exposure_status.sensor_active_time_s
                - exposure_status.in_grid_duration_s
                - exposure_status.outside_grid_duration_s
            )
            .abs()
            .max()
            < 1e-5,
            "retained operating diagnostics agree with conserved exposure",
        )
        sparse = table(value.exposure, "exposure")
        require(set(sparse.time_bin_id) <= set(bins.time_bin_id), "no pre-run exposure")
        sensing[name] = {
            "sparse_rows": len(sparse),
            "grid_cells": len(exposure_grid),
            "prepared_grid_cells": cell_count,
            "time_bins": len(bins),
            "positive_cells_across_replications": int(sparse.cell_id.nunique()),
            "zero_cells_across_replications": len(exposure_grid) - int(sparse.cell_id.nunique()),
            "duration_seconds_by_fleet": sparse.groupby("fleet_id").duration_s.sum().to_dict(),
            "conservation": exposure_status.to_dict("records"),
        }
    portfolio_reports = []
    for item in analyses:
        samples = table(item.samples, "portfolio_samples")
        require(
            len(samples) == item.count_portfolios * expected_sampling_runs
            and samples.groupby("portfolio_id").size().eq(expected_sampling_runs).all(),
            f"all count portfolios by {expected_sampling_runs} sample utilities retained",
        )
        require(
            set(samples.selected_joint_replication_id) <= set(rep_ids),
            "joint replication sampling identity",
        )
        portfolio_reports.append(
            {
                "analysis_id": item.analysis_id,
                "risk_metric": item.config.risk_metric,
                "sampling_rounds": expected_sampling_runs,
                "count_portfolios": item.count_portfolios,
                "sample_utilities": len(samples),
                "elapsed_seconds": item.elapsed_seconds,
                "config": item.config.model_dump(mode="json"),
            }
        )
    report = {
        "audit_version": "city-example-audit@6",
        "passed": True,
        "run_id": run.run_id,
        "analysis_ids": [item.analysis_id for item in analyses],
        "clock": clock,
        "vehicle_counts": run.vehicle_counts,
        "environment": {
            "cells": cell_count,
            "population": int(population.value.sum()) if is_lausanne else None,
            "zero_population_cells": int(population.value.eq(0).sum()) if is_lausanne else None,
            "feature_names": sorted(features.feature.unique().tolist()),
            "working_crs": str(environment.grid_cells.crs),
            "road_edges": len(environment.road_edges),
        },
        "bus": {
            "source_route_counts": {
                f.fleet_id: reports[f.fleet_id]["route_counts"] for f in bus_configs
            },
            "included_complete_trips": per_route,
            "unique_directed_stop_legs": len(leg_pairs),
            "unreachable_legs": unreachable,
            "service_day_selection": {
                f.fleet_id: reports[f.fleet_id]["service_days"] for f in bus_configs
            },
        },
        "spatial_support": reports["spatial_support"],
        "service_areas": reports["postal"]["service_areas"] if is_lausanne else None,
        "postal_location_condition": (
            reports["postal"]["location_condition"] if is_lausanne else None
        ),
        "od_rejections": len(table(run.resolution, "od_rejections")),
        "postal_search_seconds": table(run.resolution, "planning_metrics").to_dict("records"),
        "operations": operations,
        "sensing": sensing,
        "portfolios": portfolio_reports,
        "reference_machine": {
            "system": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "dependencies": {
                name: version(name)
                for name in ("numpy", "scipy", "pandas", "geopandas", "ortools", "pyarrow")
            },
        },
        "assumptions": list(run.assumptions),
        "limitations": [
            "Heuristic duties and routes; no minimum fleet or global optimum guarantee",
            "Routing and speeds are supplied/assumed, not calibrated",
            "Elapsed times describe the recorded completed stages; cache/replay provenance is retained in each source run",
            "P05 is an empirical quantile conditional on the simulated pool, not a guaranteed worst-case bound",
        ],
    }
    framework = Path("docs/MOBILE_SENSING_FRAMEWORK.tex")
    if framework.is_file():
        report["framework_sha256"] = digest_file(framework)
    (root / "example-audit.json").write_text(canonical_json_text(report) + "\n")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    print(json.dumps({"passed": audit_example(parser.parse_args().root)["passed"]}))
