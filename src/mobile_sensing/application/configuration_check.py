"""Fast authoring checks; deliberately does not realize demand or solve routes."""

from pathlib import Path

from mobile_sensing.application.calendar_authoring import resolve_calendar
from mobile_sensing.application.civil_time import civil_clock, clock_seconds
from mobile_sensing.application.project_models import (
    ProjectConfig,
    ProjectResolutionResult,
)
from mobile_sensing.application.temporal_authoring import (
    group_windows,
    profile_intervals,
    resolve_intervals,
)
from mobile_sensing.datasets.inputs import load_input


def check_configuration(root, config, *, cancellation, progress):
    root = Path(root)
    config = ProjectConfig.model_validate(config)
    if config.prepared_environment is None or not config.fleets:
        raise ValueError("Prepare an environment and configure at least one fleet")
    progress.update(phase="check.calendar_and_inputs", completed=0, total=len(config.fleets))
    cancellation.raise_if_cancelled()
    config, calendar = resolve_calendar(root, config)
    prepared = config.prepared_environment
    # Only existence/metadata checks here. Run verifies all immutable bytes.
    for collection, reference in (
        ("environments", prepared.artifact),
        ("datasets", prepared.features),
    ):
        if not (root / collection / reference.artifact_id / "manifest.json").is_file():
            raise ValueError(
                f"Prepared {collection} input is missing; prepare the environment again"
            )
    clock = civil_clock(config.simulation, prepared.timezone)
    counts = {}
    for index, fleet in enumerate(config.fleets):
        cancellation.raise_if_cancelled()
        demand, supply, dispatch = fleet.demand, fleet.supply, fleet.dispatch
        try:
            references = [
                (
                    demand.input_id,
                    {"gtfs"} if demand.template == "gtfs" else {"demand"},
                ),
                (supply.input_id, {"supply"}),
                (dispatch.assignment_input, {"assignment"}),
                (supply.service_area_input, {"service_area"}),
                (supply.area_assignment_input, {"area_assignment"}),
                (demand.time_profile_input, {"demand"}),
                (
                    demand.od_distribution_input,
                    {"demand", "feature", "weight", "population"},
                ),
            ]
            for identifier, roles in references:
                if identifier:
                    metadata, _ = load_input(root, identifier, verify=False)
                    if metadata["role"] not in roles:
                        raise ValueError(f"Input {metadata['name']} has an incompatible role")
                    if metadata.get("crs_required"):
                        raise ValueError(f"Confirm the CRS of input {metadata['name']}")
            if demand.source == "import" and not demand.input_id:
                raise ValueError("Select a demand input")
            if supply.source == "import" and not supply.input_id:
                raise ValueError("Select a vehicle catalog input")
            required_features = []
            if demand.source == "generator":
                (
                    required_features.extend(item.feature for item in demand.spatial_weights)
                    if demand.spatial_weights
                    else required_features.append(demand.spatial_feature)
                )
                if demand.task_type == "od" and not demand.od_distribution_input:
                    (
                        required_features.extend(
                            item.feature for item in demand.destination_spatial_weights
                        )
                        if demand.destination_spatial_weights
                        else required_features.append(demand.destination_feature)
                    )
                if demand.temporal_mode == "window":
                    if not clock_seconds(demand.start_time, clock) < clock_seconds(
                        demand.end_time, clock
                    ):
                        raise ValueError("Demand start must precede end")
                else:
                    resolve_intervals(demand, clock, profile_intervals(root, demand))
            if supply.source == "generated":
                group_windows(supply, clock)
                counts[fleet.fleet_id] = supply.fleet_size
                if supply.initial_location == "input":
                    raise ValueError("Input initial locations require an imported catalog")
                if supply.initial_location == "spatial_feature" or supply.synthetic_depot:
                    (
                        required_features.extend(item.feature for item in supply.spatial_weights)
                        if supply.spatial_weights
                        else required_features.append(supply.spatial_feature)
                    )
            if any(
                feature not in {*prepared.feature_names, "uniform"} for feature in required_features
            ):
                raise ValueError(
                    "A selected spatial feature is absent from the prepared environment"
                )
            depot = (
                supply.depot_cell_id is not None
                or supply.depot_longitude is not None
                or supply.synthetic_depot
            )
            if supply.initial_location == "depot" and not depot:
                raise ValueError("Select a depot for depot initial locations")
            if supply.capacity_mode != "none" and supply.capacity is None:
                raise ValueError("Specify capacity per vehicle")
            if supply.source == "timetable" and (
                demand.source != "import" or demand.task_type != "ordered"
            ):
                raise ValueError("Timetable duties require imported ordered-stop tasks")
            if dispatch.mode == "one_shot":
                if demand.generation_timing != "offline" or demand.task_type != "location":
                    raise ValueError("One-shot requires offline location tasks")
                if not depot or supply.initial_location != "depot":
                    raise ValueError("One-shot requires vehicles starting at a shared depot")
                if supply.capacity_mode == "occupancy":
                    raise ValueError("One-shot does not support occupancy capacity")
            if supply.post_service == "return_after_plan" and dispatch.mode != "one_shot":
                raise ValueError("Return after plan requires One-shot dispatch")
        except ValueError as exc:
            raise ValueError(f"{fleet.name}: {exc}") from exc
        progress.update(
            phase="check.fleet_configuration",
            completed=index + 1,
            total=len(config.fleets),
        )
    return ProjectResolutionResult(
        validation_level="configuration",
        vehicle_counts=counts,
        task_counts={},
        assumptions=(
            "Configuration checks passed; operational feasibility is checked during Run.",
        ),
        reports={
            "calendar": calendar.model_dump(mode="json"),
            "deferred_checks": [
                "Verify complete input contents and spatial feature mass; resolve locations",
                "Generate joint replications and infer timetable duties",
                "Check directed routes and solve One-shot plans",
            ],
        },
    )
