"""Explicit conversion into a new revision; historical revisions stay immutable."""

from datetime import datetime
from zoneinfo import ZoneInfo

from mobile_sensing.application.project_models import (
    ProjectConfig,
    FleetEditor,
    DemandEditor,
    SupplyEditor,
    DispatchEditor,
    SimulationEditor,
)
from mobile_sensing.application.run_models import MigrationView
from mobile_sensing.application.studio_models import EnvironmentEditor


def migrate_project(payload, source_revision):
    if payload.get("schema_version") == "3.4":
        return MigrationView(
            config=ProjectConfig.model_validate_json(__import__("json").dumps(payload)), notices=()
        )
    if payload.get("schema_version") == "3.3":
        config = ProjectConfig.model_validate_json(__import__("json").dumps(payload))
        return MigrationView(
            config=config.model_copy(
                update={"schema_version": "3.4", "source_revision": source_revision}
            ),
            notices=(
                "Version 3.4 adds explicit none, uploaded and auto service-area modes. Existing configurations preserve their prior disabled or uploaded-area behavior.",
            ),
        )
    if payload.get("schema_version") == "3.2":
        config = ProjectConfig.model_validate_json(__import__("json").dumps(payload))
        return MigrationView(
            config=config.model_copy(
                update={"schema_version": "3.4", "source_revision": source_revision}
            ),
            notices=(
                "Version 3.3 separates simulation reporting bins from utility intervals. The migrated utility interval preserves the former reporting-bin behavior; change it explicitly for a new analysis.",
            ),
        )
    if payload.get("schema_version") == "3.1":
        config = ProjectConfig.model_validate_json(__import__("json").dumps(payload))
        return MigrationView(
            config=config.model_copy(
                update={"schema_version": "3.4", "source_revision": source_revision}
            ),
            notices=(
                "Version 3.2 preserves historical sensing and single-trip settings. Select operating sensing and replenishment explicitly for a new run; retained results remain unchanged.",
            ),
        )
    if payload.get("schema_version") == "3.0":
        config = ProjectConfig.model_validate_json(__import__("json").dumps(payload))
        from mobile_sensing.application.project_models import NumericRange
        from mobile_sensing.application.temporal_authoring import expand_range

        fleets = []
        for fleet in config.portfolio.fleets:
            counts = tuple(sorted(set(fleet.counts)))
            if counts:
                candidate = NumericRange(
                    minimum=counts[0],
                    maximum=counts[-1],
                    step=counts[1] - counts[0] if len(counts) > 1 else 1,
                )
                if len(counts) <= 10000 and tuple(expand_range(candidate, integers=True)) == counts:
                    fleet = fleet.model_copy(update={"count_range": candidate})
            fleets.append(fleet)
        config = config.model_copy(
            update={"portfolio": config.portfolio.model_copy(update={"fleets": tuple(fleets)})}
        )
        return MigrationView(
            config=config.model_copy(
                update={"schema_version": "3.4", "source_revision": source_revision}
            ),
            notices=(
                "Version 3.0 retains its exact calendar date and original result sources. Saving creates a version 3.4 revision with historical sensing retained.",
            ),
        )
    if payload.get("schema_version") != "m10-ui-draft@1":
        raise ValueError("This legacy configuration has no declared migration adapter")
    if payload.get("authoring"):
        config = ProjectConfig.model_validate_json(__import__("json").dumps(payload["authoring"]))
        return MigrationView(
            config=config.model_copy(update={"source_revision": source_revision}),
            notices=(
                "Version-three authoring settings were recovered. Saving or running creates a new revision; historical artifacts remain linked to their original sources.",
            ),
        )
    old_environment, old_simulation = payload.get("environment", {}), payload.get("simulation", {})
    timezone = old_simulation.get("displayTimezone", "UTC")
    origin = datetime.fromisoformat(
        old_simulation.get("originUtc", "2026-01-14T00:00:00Z").replace("Z", "+00:00")
    ).astimezone(ZoneInfo(timezone))
    from datetime import timedelta

    start = origin + timedelta(seconds=old_simulation.get("observationStartS", 0))
    end = origin + timedelta(seconds=old_simulation.get("endS", 3600))
    if start.date() != end.date() and not (
        end.date() == start.date() + timedelta(days=1) and end.hour == end.minute == end.second == 0
    ):
        raise ValueError(
            "Legacy multi-day observation needs an explicit civil-day selection before migration"
        )
    simulation = SimulationEditor(
        service_date=start.date(),
        start_time=start.strftime("%H:%M:%S"),
        end_time="24:00" if end.date() != start.date() else end.strftime("%H:%M:%S"),
        replications=old_simulation.get("operationalReplicationsR", 2),
        temporal_resolution_minutes=old_simulation.get("reportingIntervalS", 900) / 60,
        seed=old_simulation.get("masterSeed", 20260114),
    )
    fleets, notices = [], [
        "Review this migration before saving or running. Original input references and every old result remain in the source revision.",
        "Legacy path/normalized-artifact inputs require registration in Data or Fleet Configuration; prepare the environment before a new run. No path or artifact was silently substituted.",
    ]
    for entry in payload.get("fleets", []):
        old = entry.get("config", {})
        if not old.get("fleet_id"):
            continue
        demand = old.get("demand", {})
        dispatch = old.get("dispatch", {})
        task_type = demand.get("structure", "location")
        fleets.append(
            FleetEditor(
                fleet_id=old["fleet_id"],
                name=old.get("label", old["fleet_id"]),
                demand=DemandEditor(
                    source="import",
                    task_type=task_type,
                    template="gtfs" if demand.get("source") == "gtfs" else "table",
                ),
                supply=SupplyEditor(source="import"),
                dispatch=DispatchEditor(
                    mode="scheduled" if dispatch.get("policy") == "predefined" else "sequential",
                    max_pickup_minutes=(dispatch.get("max_pickup_time_s") or 900) / 60,
                ),
            )
        )
        notices.append(
            f"{old['fleet_id']}: re-register demand and the complete vehicle catalog from the preserved source revision; legacy generator/rate and normalized-artifact settings require an explicit new mapping."
        )
    return MigrationView(
        config=ProjectConfig(
            environment=EnvironmentEditor(
                working_crs=old_environment.get("workingCrs", "auto"),
                timezone=timezone,
                grid_size_m=old_environment.get("gridCellSizeM", 100),
                speed_kph=old_environment.get("speedMps", 8.33) * 3.6,
            ),
            fleets=tuple(fleets),
            simulation=simulation,
            source_revision=source_revision,
        ),
        notices=tuple(notices),
    )
