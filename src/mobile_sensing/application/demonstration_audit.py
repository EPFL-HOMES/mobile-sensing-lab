"""Executable acceptance of the revised Lausanne simulation, including mandatory deliveries."""

import json

import pandas as pd
import pyarrow.parquet as pq

from mobile_sensing.api.queries import ArtifactCatalog, _table_paths
from mobile_sensing.application.example_build import STATION_LONGITUDE, STATION_LATITUDE


def audit_demonstration_simulation(root, run):
    catalog = ArtifactCatalog(root)
    located = {}

    def artifact(reference):
        if reference.artifact_id not in located:
            located[reference.artifact_id] = catalog.locate(reference.artifact_id)
        return located[reference.artifact_id]

    def table(reference, name):
        return pd.concat(
            [pq.read_table(path).to_pandas() for path in _table_paths(artifact(reference), name)],
            ignore_index=True,
        )

    def require(condition, message):
        if not condition:
            raise ValueError("Demonstration audit failed: " + message)

    fleets = {fleet.fleet_id: fleet for fleet in run.config.fleets}
    postal = fleets["postal"]
    require(
        set(run.vehicle_counts) == {"bus", "postal", "taxi"}
        and run.vehicle_counts["bus"] == 34
        and run.vehicle_counts["postal"] == 20
        and run.vehicle_counts["taxi"] == 80,
        "fixed Bus, Postal and Taxi catalogs",
    )
    catalog_rows = table(run.resolution, "physical_catalog")
    require(
        catalog_rows.groupby("fleet_id").size().to_dict() == run.vehicle_counts,
        "reported vehicle counts match the frozen physical catalog",
    )
    require(
        postal.demand.task_volume == 2000
        and postal.demand.volume_mode == "fixed"
        and postal.demand.generation_timing == "at_start"
        and postal.demand.quantity == 1
        and postal.demand.service_seconds == 120
        and postal.supply.capacity == 100
        and postal.supply.work_hours == 8
        and postal.supply.operating_start == "06:00"
        and postal.supply.operating_end == "21:00",
        "requested Postal configuration",
    )
    availability = table(run.resolution, "availability")
    windows = {}
    for row in availability.itertuples():
        value = json.loads(row.availability_json)
        start, end = value["availability_start_s"], value["availability_end_s"]
        windows[row.replication_id, row.fleet_id, row.vehicle_id] = start, end
        if row.fleet_id == "postal":
            require(21600 <= start <= 43200 and end <= 75600, "Postal operating bounds")
            require(abs(end - start - 28800) < 1e-8, "eight-hour Postal shifts")
    outcomes = table(run.simulation, "task_outcomes")
    deliveries = outcomes[(outcomes.fleet_id == "postal") & (outcomes.kind == "service")]
    require(deliveries.status.eq("completed").all(), "every delivery completed by executor")
    require(
        len(deliveries) == 2000 * run.replications
        and deliveries.groupby("replication_id").size().eq(2000).all(),
        "2,000 mandatory independent deliveries per replication",
    )
    returns = outcomes[(outcomes.fleet_id == "postal") & (outcomes.kind == "depot_return")]
    require(
        len(returns) >= 20 * run.replications and returns.status.eq("completed").all(),
        "all depot returns",
    )
    reloads = returns[returns.source_policy.eq("dispatch.one_shot.reload@1")]
    require(
        len(returns) - len(reloads) == 20 * run.replications, "one final depot return per vehicle"
    )
    activities = table(run.simulation, "activity_intervals")
    refill_service = activities[
        activities.task_id.isin(reloads.task_id) & activities.activity_kind.eq("service")
    ]
    refill_duration = (
        (refill_service.end_s - refill_service.start_s)
        .groupby([refill_service.replication_id, refill_service.task_id])
        .sum()
    )
    require(
        len(refill_duration) == len(reloads) and refill_duration.ge(900 - 1e-6).all(),
        "minimum fifteen-minute depot replenishment",
    )
    events = table(run.simulation, "operational_events")
    capacity_events = events[
        events.fleet_id.eq("postal") & events.record_kind.eq("capacity_milestone")
    ]
    reset_count = 0
    for _, group in capacity_events.groupby(["replication_id", "vehicle_id"]):
        stock = 100.0
        for event in group.sort_values(["time_s", "event_index"]).itertuples():
            if event.capacity_reset:
                require(stock < 1, "reload only when another delivery cannot fit")
                require(
                    event.task_id in set(reloads.task_id),
                    "capacity reset belongs to a planned replenishment",
                )
                reset_count += 1
                stock = 100.0
            else:
                stock += event.quantity_delta
            require(-1e-6 <= stock <= 100 + 1e-6, "executed capacity conservation")
    require(reset_count == len(reloads), "exactly one stock reset per completed reload")
    for row in pd.concat([deliveries, returns]).itertuples():
        start, end = windows[row.replication_id, row.fleet_id, row.vehicle_id]
        require(
            row.assigned_at_s >= start and row.completion_s <= end + 1e-6,
            "executor respects each shift",
        )
    tasks = table(run.resolution, "realized_tasks")
    service = tasks[tasks.fleet_id == "postal"]
    for value in map(json.loads, service.task_json):
        if value["kind"] == "service":
            require(
                value["required_capacity"] == 1 and value["steps"][0]["service_duration_s"] == 120,
                "realized quantity and service",
            )
    physical = table(run.resolution, "physical_catalog")
    depot_id = json.loads(physical.loc[physical.fleet_id == "postal", "spec_json"].iloc[0])[
        "depot_location_id"
    ]
    positions = table(run.resolution, "resolved_locations")
    depot = json.loads(positions.loc[positions.location_id == depot_id, "location_json"].iloc[0])
    require(
        depot["original_x"] == STATION_LONGITUDE and depot["original_y"] == STATION_LATITUDE,
        "station depot coordinate provenance",
    )
    ride = fleets["taxi"]
    require(
        ride.demand.generation_timing == "online" and ride.demand.task_volume == 800,
        "online expected demand of 800",
    )
    require([g.count for g in ride.supply.shift_groups] == [8, 24, 24, 24], "exact shift shares")
    for _, group in availability[availability.fleet_id.eq("taxi")].groupby("replication_id"):
        periods = [json.loads(v) for v in group.availability_json]
        starts = [v["availability_start_s"] for v in periods]
        require(
            len(starts) == 80
            and sum(v < 21600 for v in starts) == 8
            and all(0 <= v <= 57600 for v in starts),
            "80 vehicles: eight starts before 06:00, all by 16:00",
        )
        require(
            all(
                abs(v["availability_end_s"] - v["availability_start_s"] - 28800) < 1e-6
                for v in periods
            ),
            "eight-hour Taxi shifts",
        )
    require(
        all(f.sensing_mode == "operating_duration" for f in fleets.values()),
        "operating-state sensing",
    )
    status = table(run.exposure, "replication_status")
    require(
        len(status) == run.replications and status.complete.all(), "complete joint replications"
    )
    require(status.conservation_residual_s.abs().max() < 1e-5, "exposure conservation")
    bins = table(run.exposure, "time_bins").sort_values("start_s")
    require(
        len(bins) == 24 and bins.start_s.iloc[0] == 0 and bins.end_s.iloc[-1] == 86400,
        "hourly natural day",
    )
    resolution = artifact(run.resolution).manifest.scientific_identity.resolved_config
    reports = resolution["reports"]["postal"]["planning"]
    return {
        "passed": True,
        "run_id": run.run_id,
        "replications": run.replications,
        "deliveries_completed": len(deliveries),
        "depot_returns_completed": len(returns),
        "reloads_completed": len(reloads),
        "depot_minimum_stay_seconds": (
            None if refill_duration.empty else float(refill_duration.min())
        ),
        "deliveries_per_vehicle_range": [
            int(v)
            for v in deliveries.groupby(["replication_id", "vehicle_id"]).size().agg(["min", "max"])
        ],
        "station_depot": depot,
        "postal_search_seconds": table(run.resolution, "planning_metrics").search_seconds.tolist(),
        "postal_travel_seconds": [
            report["objective_travel_seconds"] for report in reports.values()
        ],
        "elapsed_seconds": run.elapsed_seconds,
        "conservation_residual_s_max": float(status.conservation_residual_s.abs().max()),
    }
