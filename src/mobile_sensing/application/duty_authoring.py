"""Deterministic feasible duty inference over one absolute task timeline."""

from collections import defaultdict
from datetime import timedelta
import pandas as pd

from mobile_sensing.contracts import (
    AssignmentPlan,
    AssignmentRow,
    Task,
    TaskStep,
    VehicleKey,
    VehicleSpec,
    stable_id,
)
from mobile_sensing.datasets.gtfs import (
    GTFSReconstructionConfig,
    GTFSImportError,
    register_gtfs_source,
    reconstruct_gtfs,
    gtfs_service_origin,
)
from mobile_sensing.datasets.inputs import load_input
from mobile_sensing.datasets.parsing import LocationResolver
from mobile_sensing.simulation.timing import estimate_task_timing


def infer_duties(fleet, tasks, support, *, fixed_size=None, assignments=None):
    """Greedy feasible chaining, never a minimum-fleet certificate."""
    locations, routing = support.locations, support.environment.routing
    profile = fleet.routing_profile
    timing = {}
    for task in tasks:
        start = task.steps[0].scheduled_time_s
        if start is None:
            raise ValueError(
                "Timetable duty inference requires a scheduled first-stop time on every task"
            )
        timing[task.task_id] = estimate_task_timing(
            task,
            start_location_id=task.steps[0].location_id,
            start_s=start,
            locations=locations,
            routing=routing,
            profile_id=profile,
        )
    duties, ends = [], []
    if assignments:
        if set(assignments) != {task.task_id for task in tasks}:
            raise ValueError("Input vehicle assignments must cover every timetable task")
        groups = defaultdict(list)
        for task in tasks:
            groups[assignments[task.task_id]].append(task)
        vehicle_names = sorted(groups)
        duties = [
            sorted(groups[name], key=lambda task: (task.steps[0].scheduled_time_s, task.task_id))
            for name in vehicle_names
        ]
        if fixed_size is not None and fixed_size < len(duties):
            raise ValueError("Input assignments use more vehicles than the fixed catalog")
    else:
        for task in sorted(tasks, key=lambda task: (task.steps[0].scheduled_time_s, task.task_id)):
            start = task.steps[0].scheduled_time_s
            candidates = []
            for index, duty in enumerate(duties):
                route = routing.route(
                    profile,
                    locations[duty[-1].steps[-1].location_id].node_id,
                    locations[task.steps[0].location_id].node_id,
                )
                if route.reachable:
                    ready = ends[index] + fleet.supply.turnaround_seconds + route.total_duration_s
                    if ready <= start:
                        candidates.append(
                            (route.total_duration_s, start - ready, duty[0].task_id, index)
                        )
            if candidates:
                selected = min(candidates)[3]
                duties[selected].append(task)
                ends[selected] = timing[task.task_id].end_s
            elif fixed_size is None or len(duties) < fixed_size:
                duties.append([task])
                ends.append(timing[task.task_id].end_s)
            else:
                raise ValueError(
                    "Current chaining rules did not find a feasible plan for the fixed vehicle count; this is not a proof of mathematical infeasibility"
                )
        vehicle_names = [
            stable_id("inferred_vehicle", {"fleet": fleet.fleet_id, "first_task": duty[0].task_id})
            for duty in duties
        ]
    if not duties:
        raise ValueError("No timetable tasks in the selected scope")
    specs, planned_tasks, rows, report = [], [], [], []
    for name, duty in zip(vehicle_names, duties):
        key = VehicleKey(fleet_id=fleet.fleet_id, vehicle_id=name)
        current = duty[0].steps[0].location_id
        start = float(duty[0].steps[0].scheduled_time_s)
        now = start
        sequence = []
        for index, task in enumerate(duty):
            if index:
                route = routing.route(
                    profile,
                    locations[current].node_id,
                    locations[task.steps[0].location_id].node_id,
                )
                ready = now + fleet.supply.turnaround_seconds + route.total_duration_s
                if not route.reachable or ready > task.steps[0].scheduled_time_s:
                    raise ValueError(
                        f"Input duty {name!r} cannot reach task {task.task_id!r} under the selected travel times"
                    )
                reposition = Task(
                    task_id=stable_id("duty_reposition", {"vehicle": name, "next": task.task_id}),
                    fleet_id=fleet.fleet_id,
                    release_s=now + fleet.supply.turnaround_seconds,
                    kind="reposition",
                    source_policy="inferred_duty@1",
                    steps=(TaskStep(step_index=1, location_id=task.steps[0].location_id),),
                )
                sequence.append(reposition)
                now = ready
            service = task.model_copy(update={"release_s": float(task.steps[0].scheduled_time_s)})
            sequence.append(service)
            now = timing[task.task_id].end_s
            current = task.steps[-1].location_id
        specs.append(
            VehicleSpec(
                key=key,
                availability_start_s=start,
                availability_end_s=max(now, start + 1e-6),
                initial_location_id=duty[0].steps[0].location_id,
                identity_provenance="uploaded" if assignments else "inferred_duty",
            )
        )
        for index, task in enumerate(sequence):
            rows.append(AssignmentRow(vehicle=key, order_index=index, task_id=task.task_id))
            planned_tasks.append(task)
        report.append(
            {
                "vehicle_id": name,
                "service_tasks": len(duty),
                "start_s": start,
                "end_s": now,
                "identity": (
                    "input vehicle reference"
                    if assignments
                    else "inferred vehicle on merged absolute timeline"
                ),
            }
        )
    if fixed_size is not None:
        for index in range(len(specs), fixed_size):
            specs.append(
                VehicleSpec(
                    key=VehicleKey(fleet_id=fleet.fleet_id, vehicle_id=f"unused-{index+1:04d}"),
                    availability_start_s=min(spec.availability_start_s for spec in specs),
                    availability_end_s=max(spec.availability_end_s for spec in specs),
                    initial_location_id=specs[0].initial_location_id,
                    identity_provenance="generated",
                )
            )
    plan = AssignmentPlan(
        assignment_plan_id=stable_id("assignment", [row.model_dump(mode="json") for row in rows]),
        rows=tuple(rows),
    )
    return tuple(planned_tasks), tuple(specs), plan, report


def civil_gtfs(root, fleet, support, clock, cancellation=None):
    if not fleet.demand.input_id or not fleet.demand.route_ids:
        raise ValueError("Select a GTFS archive and at least one route")
    metadata, path = load_input(root, fleet.demand.input_id)
    if metadata["role"] != "gtfs":
        raise ValueError("GTFS shortcut requires a registered GTFS input")
    source = register_gtfs_source(path, artifact_root=root)
    resolver = LocationResolver(
        environment=support.environment.reference,
        snapper=support.environment.snapping,
        routing=support.environment.routing,
        routing_profile_id=fleet.routing_profile,
    )
    # Discover the maximal service-hour offset without forming an OD product.
    selected_trips = pd.read_csv(source.tables["trips"], dtype=str, keep_default_na=False)
    selected_ids = set(
        selected_trips.loc[selected_trips.route_id.isin(fleet.demand.route_ids), "trip_id"]
    )
    max_hours = 24
    for chunk in pd.read_csv(
        source.tables["stop_times"],
        usecols=["trip_id", "departure_time"],
        dtype=str,
        keep_default_na=False,
        chunksize=100000,
    ):
        if cancellation:
            cancellation.raise_if_cancelled()
        selected = chunk.loc[chunk.trip_id.isin(selected_ids), "departure_time"]
        hours = [int(value.split(":")[0]) for value in selected if value]
        max_hours = max(max_hours, max(hours, default=0))
    previous_days = max(1, max_hours // 24)
    if previous_days > 7:
        raise ValueError("GTFS service-hour range exceeds the seven-day carry-in resource bound")
    day = clock.origin_utc.astimezone(
        __import__("zoneinfo").ZoneInfo(clock.display_timezone)
    ).date()
    tasks, reports, dependencies, route_counts = [], [], [], []
    for offset in range(-previous_days, 2):
        if cancellation:
            cancellation.raise_if_cancelled()
        service_date = (day + timedelta(days=offset)).isoformat()
        config = GTFSReconstructionConfig(
            schema_version="2.0",
            fleet_id=fleet.fleet_id,
            service_date=service_date,
            route_ids=tuple(sorted(fleet.demand.route_ids)),
            routing_profile_id=fleet.routing_profile,
            turnaround_duration_s=fleet.supply.turnaround_seconds,
        )
        try:
            result = reconstruct_gtfs(
                source,
                config,
                artifact_root=root,
                resolver=resolver,
                sensing_boundary=support.environment.boundary.geometry.iloc[0],
            )
        except GTFSImportError as exc:
            if "no active" in str(exc).lower() or "no selected trips" in str(exc).lower():
                reports.append({"service_date": service_date, "status": "no active trips"})
                continue
            raise
        zone = result.diagnostics.get("agency_timezone")
        if zone is None:
            agency = pd.read_csv(source.tables["agency"], dtype=str)
            zone = str(agency.agency_timezone.iloc[0])
        shift = (gtfs_service_origin(service_date, zone) - clock.origin_utc).total_seconds()
        support.locations.update({location.location_id: location for location in result.locations})
        kept = 0
        for task in result.tasks:
            if task.kind != "service":
                continue
            steps = tuple(
                step.model_copy(
                    update={
                        "scheduled_time_s": step.scheduled_time_s + shift,
                        "source_record_refs": tuple(
                            f"{service_date}:{ref}" for ref in step.source_record_refs
                        ),
                    }
                )
                for step in task.steps
            )
            if steps[0].scheduled_time_s >= clock.end_s:
                continue
            tasks.append(
                task.model_copy(
                    update={
                        "release_s": steps[0].scheduled_time_s,
                        "steps": steps,
                        "source_record_refs": tuple(
                            f"{service_date}:{ref}" for ref in task.source_record_refs
                        ),
                    }
                )
            )
            kept += 1
        trip_report = (
            pd.read_parquet(result.directory / "trip_diagnostics.parquet")
            if (result.directory / "trip_diagnostics.parquet").exists()
            else None
        )
        if trip_report is not None:
            for route, rows in trip_report.groupby("route_id"):
                route_counts.append(
                    {
                        "service_date": service_date,
                        "route_id": route,
                        "trips": len(rows),
                        "stops": int(rows.stop_count.sum()),
                    }
                )
        reports.append(
            {
                "service_date": service_date,
                "included_tasks": kept,
                "utc_shift_s": shift,
                "diagnostics": dict(result.diagnostics),
            }
        )
        dependencies.append(result.reference)
    # Dates identify trip instances, never persistent physical registration identities.
    return (
        tuple(tasks),
        {
            "service_days": reports,
            "route_counts": route_counts,
            "pre_run_rule": "All preceding selected service-day trips execute before the observation window; only observation-window movement enters sensing utility",
            "identity": "Duties are inferred anew on one merged absolute timeline, without daily block identities",
        },
        tuple(dependencies),
    )
