"""Resolve generator and table semantics into canonical Task/TaskStep records."""

import numpy as np
import pandas as pd
from mobile_sensing.application.civil_time import clock_seconds, input_seconds
from mobile_sensing.contracts import Task, TaskStep, stable_id
from mobile_sensing.datasets.inputs import load_input
from mobile_sensing.datasets.parsing import LocationResolver, parse_float


def table_input(root, input_id, role):
    metadata, path = load_input(root, input_id)
    if metadata["role"] not in role:
        raise ValueError(f"Select an input with role {role}")
    if path.suffix == ".csv":
        frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    elif path.suffix == ".parquet":
        frame = pd.read_parquet(path)
    else:
        raise ValueError("Task, vehicle, count and rate tables require CSV or Parquet")
    return frame, metadata


def service_task(fleet, task_id, release, location_ids, quantity, source_refs=(), schedule=None):
    mode = fleet.supply.capacity_mode
    quantity = float(quantity) if mode != "none" else None
    if mode == "occupancy" and len(location_ids) != 2:
        raise ValueError("Occupancy capacity requires OD tasks")
    steps = []
    for i, location in enumerate(location_ids):
        duration = (
            fleet.demand.pickup_seconds
            if len(location_ids) == 2 and i == 0
            else fleet.demand.service_seconds
        )
        delta = None
        if quantity is not None:
            delta = (
                (-quantity if i == 0 else quantity)
                if mode == "occupancy"
                else (-quantity if i == len(location_ids) - 1 else None)
            )
        steps.append(
            TaskStep(
                step_index=i + 1,
                location_id=location,
                service_duration_s=float(duration),
                quantity_delta=delta,
                scheduled_time_s=schedule[i] if schedule else None,
                source_record_refs=source_refs,
            )
        )
    return Task(
        task_id=task_id,
        fleet_id=fleet.fleet_id,
        release_s=float(release),
        steps=tuple(steps),
        required_capacity=quantity,
        source_record_refs=source_refs,
        source_policy=(
            "synthetic_demand@1"
            if fleet.demand.source == "generator" or fleet.demand.content != "instances"
            else "imported_tasks@1"
        ),
    )


def sparse_od_input(root, input_id, support):
    frame, metadata = table_input(root, input_id, {"demand", "feature", "weight", "population"})
    required = {"origin_cell_id", "destination_cell_id", "weight"}
    if not required <= set(frame):
        raise ValueError("Sparse OD input requires origin_cell_id, destination_cell_id, weight")
    if frame.duplicated(["origin_cell_id", "destination_cell_id"]).any():
        raise ValueError("Sparse OD weights contain duplicate OD pairs")
    rows = []
    for row_number, row in enumerate(frame.itertuples(index=False), start=2):
        try:
            weight = parse_float(row.weight, nonnegative=True)
            rows.append(
                (support.cell(row.origin_cell_id), support.cell(row.destination_cell_id), weight)
            )
        except ValueError as exc:
            raise ValueError(f"Input {metadata['name']}, source row {row_number}: {exc}") from exc
    if not rows or sum(row[2] for row in rows) <= 0:
        raise ValueError("Sparse OD weights require positive total mass")
    return tuple(rows)


def generate_tasks(
    fleet,
    support,
    clock,
    rng,
    replication_id,
    *,
    max_tasks=100000,
    cancellation=None,
    sparse_od=(),
    allowed_locations=None,
    temporal_intervals=None,
):
    demand = fleet.demand
    if demand.temporal_mode == "window":
        start, end = clock_seconds(demand.start_time, clock), clock_seconds(demand.end_time, clock)
        if not clock.observation_start_s <= start < end <= clock.end_s:
            raise ValueError("Generator window must lie inside the observation window")
        arrivals = rng.stream("studio.demand.arrivals", replication_id, fleet.fleet_id)
        if demand.task_volume > max_tasks:
            raise ValueError("Requested task total exceeds configured task limit")
        count = (
            int(demand.task_volume)
            if demand.volume_mode == "fixed"
            else int(arrivals.poisson(demand.task_volume))
        )
        if count > max_tasks:
            raise ValueError("Realized task total exceeds configured task limit")
        releases = (
            np.full(count, start)
            if demand.release_mode == "at_start"
            else np.sort(arrivals.uniform(start, end, count), kind="stable")
        )
    else:
        from mobile_sensing.application.temporal_authoring import draw_releases

        releases = draw_releases(
            demand,
            clock,
            rng.stream("studio.demand.profile", replication_id, fleet.fleet_id),
            temporal_intervals,
            max_tasks,
        )
        count = len(releases)
    origin_components = demand.spatial_weights or ((demand.spatial_feature, 1.0),)
    locations, probabilities = (
        support.weighted_mix(origin_components, allowed_locations) if not sparse_od else ([], [])
    )
    choices = rng.stream("studio.demand.locations", replication_id, fleet.fleet_id)
    destination_ids, destination_probabilities = (
        support.weighted_mix(
            demand.destination_spatial_weights or ((demand.destination_feature, 1.0),)
        )
        if demand.task_type == "od" and not sparse_od
        else ([], [])
    )
    rejected, tasks = [], []
    if demand.od_distribution_input and not sparse_od:
        raise ValueError("Sparse OD distribution must be resolved before generator execution")
    pair_probabilities = np.array([row[2] for row in sparse_od], dtype=float)
    if sparse_od:
        pair_probabilities /= pair_probabilities.sum()
    for index, release in enumerate(releases):
        if cancellation and index % 64 == 0:
            cancellation.raise_if_cancelled()
        for attempt in range(10000):
            if cancellation and attempt % 64 == 0:
                cancellation.raise_if_cancelled()
            if len(rejected) >= 100000:
                raise ValueError(
                    "Conditional OD sampling exceeded 100000 rejected pairs in one replication; inspect support and reachability"
                )
            pair = (
                sparse_od[int(choices.choice(len(sparse_od), p=pair_probabilities))]
                if sparse_od
                else None
            )
            origin = (
                pair[0] if pair else locations[int(choices.choice(len(locations), p=probabilities))]
            )
            selected = [origin]
            if demand.task_type == "od":
                destination = (
                    pair[1]
                    if pair
                    else destination_ids[
                        int(choices.choice(len(destination_ids), p=destination_probabilities))
                    ]
                )
                source_node, target_node = (
                    support.locations[origin].node_id,
                    support.locations[destination].node_id,
                )
                reason = (
                    "same_node"
                    if source_node == target_node
                    else (
                        "unreachable"
                        if not (
                            support.environment.routing.reachable(source_node, target_node)
                            if hasattr(support.environment.routing, "reachable")
                            else support.environment.routing.route(
                                fleet.routing_profile, source_node, target_node
                            ).reachable
                        )
                        else None
                    )
                )
                if reason:
                    rejected.append(
                        {
                            "task_rank": index,
                            "attempt": attempt,
                            "origin": origin,
                            "destination": destination,
                            "reason": reason,
                        }
                    )
                    continue
                selected.append(destination)
            break
        else:
            raise ValueError(
                "Conditional OD sampler exhausted 10000 draws for one task; inspect spatial support and directed reachability"
            )
        tasks.append(
            service_task(
                fleet,
                stable_id(
                    "task", {"replication": replication_id, "fleet": fleet.fleet_id, "rank": index}
                ),
                release,
                selected,
                demand.quantity,
            )
        )
    return tuple(tasks), rejected


def import_tasks(root, fleet, support, clock, rng, replication_id, *, max_tasks=100000):
    demand = fleet.demand
    if not demand.input_id:
        raise ValueError("Select a task input inside this fleet")
    frame, metadata = table_input(root, demand.input_id, {"demand"})
    if len(frame) > max_tasks:
        raise ValueError("Task input exceeds the configured row limit")
    columns = demand.columns
    resolver = LocationResolver(
        environment=support.environment.reference,
        known_locations=support.locations,
        snapper=support.environment.snapping,
        routing=support.environment.routing,
        routing_profile_id=fleet.routing_profile,
    )
    records, history = [], {}

    def field(row, name, default=None):
        column = columns.get(name, name)
        return row[column] if column in row and row[column] not in (None, "") else default

    def location(row, prefix, row_number):
        if demand.coordinate_kind == "cell_id":
            return support.cell(field(row, prefix + "cell_id"))
        if demand.coordinate_kind == "location_id":
            return resolver.by_id(str(field(row, prefix + "location_id"))).location_id
        if not metadata["source_crs"]:
            raise ValueError("Confirm source CRS in the input preview before mapping x/y")
        x, y = field(row, prefix + "x"), field(row, prefix + "y")
        identifier = stable_id(
            "input_location", {"input": demand.input_id, "x": str(x), "y": str(y)}
        )
        resolved = resolver.by_coordinates(
            location_id=identifier, x=x, y=y, source_crs=metadata["source_crs"]
        )
        support.locations[identifier] = resolved
        return identifier

    grouped = {}
    for row_number, row in enumerate(frame.to_dict("records"), start=2):
        try:
            refs = (f"{demand.input_id}:row:{row_number}",)
            ids = [location(row, "origin_" if demand.task_type == "od" else "", row_number)]
            if demand.task_type == "od":
                ids.append(location(row, "destination_", row_number))
            if demand.content != "instances":
                if demand.task_type == "ordered":
                    raise ValueError("Count and rate tables support location or OD tasks")
                amount = parse_float(field(row, "value"), nonnegative=True)
                start = input_seconds(
                    field(row, "interval_start", demand.start_time), demand.time_unit, clock
                )
                end = input_seconds(
                    field(row, "interval_end", demand.end_time), demand.time_unit, clock
                )
                if not clock.observation_start_s <= start < end <= clock.end_s:
                    raise ValueError("Count/rate interval must lie inside observation window")
                stream = rng.stream(
                    "studio.demand.table", replication_id, fleet.fleet_id, str(row_number)
                )
                quantity = demand.quantity
                if demand.content == "rates":
                    divisor = (
                        3600
                        if demand.rate_unit == "per_hour"
                        else (clock_seconds("24:00", clock) - clock_seconds("00:00", clock))
                    )
                    count = int(stream.poisson(amount * (end - start) / divisor))
                elif demand.count_semantics == "task_count":
                    if not amount.is_integer():
                        raise ValueError("Task count must be an integer")
                    count = int(amount)
                else:
                    count, quantity = (1 if amount > 0 else 0), amount
                    if fleet.supply.capacity_mode == "none":
                        raise ValueError(
                            "Service quantity requires an explicit capacity unit and mode"
                        )
                if len(records) + count > max_tasks:
                    raise ValueError("Expanded task count exceeds configured limit")
                releases = (
                    np.full(count, start)
                    if demand.release_mode == "at_start"
                    else np.sort(stream.uniform(start, end, count), kind="stable")
                )
                for index, release in enumerate(releases):
                    task_id = stable_id(
                        "table_task",
                        {
                            "input": demand.input_id,
                            "row": row_number,
                            "replication": replication_id,
                            "rank": index,
                        },
                    )
                    records.append(service_task(fleet, task_id, release, ids, quantity, refs))
                continue
            task_id = field(row, "task_id")
            if not isinstance(task_id, str) or not task_id:
                raise ValueError("Task IDs must be nonempty strings; map task_id")
            release = input_seconds(
                field(row, "release_time", demand.start_time), demand.time_unit, clock
            )
            quantity = parse_float(field(row, "quantity", demand.quantity), nonnegative=True)
            vehicle_id = field(row, "vehicle_id")
            if vehicle_id is not None:
                if not isinstance(vehicle_id, str):
                    raise ValueError("Vehicle IDs must use strings")
                if task_id in history and history[task_id] != vehicle_id:
                    raise ValueError("A task has conflicting vehicle assignments")
                history[task_id] = vehicle_id
            if demand.task_type == "ordered":
                sequence = parse_float(field(row, "step_index"))
                if not sequence.is_integer() or sequence < 0:
                    raise ValueError("Ordered tasks need nonnegative integer step indices")
                scheduled = input_seconds(field(row, "scheduled_time"), demand.time_unit, clock)
                duration = parse_float(
                    field(row, "service_seconds", demand.service_seconds), nonnegative=True
                )
                grouped.setdefault(task_id, []).append(
                    (int(sequence), ids[0], scheduled, duration, release, refs)
                )
            else:
                task = service_task(fleet, task_id, release, ids, quantity, refs)
                durations = [
                    parse_float(
                        field(row, "pickup_seconds", demand.pickup_seconds), nonnegative=True
                    ),
                    parse_float(
                        field(row, "service_seconds", demand.service_seconds), nonnegative=True
                    ),
                ]
                task = task.model_copy(
                    update={
                        "steps": tuple(
                            step.model_copy(
                                update={
                                    "service_duration_s": (
                                        durations[0] if len(ids) == 2 and i == 0 else durations[1]
                                    )
                                }
                            )
                            for i, step in enumerate(task.steps)
                        )
                    }
                )
                records.append(task)
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError(
                f"Input {metadata['name']}, source row {row_number}: {exc}. No partial multi-stop task was accepted."
            ) from exc
    for task_id, rows in sorted(grouped.items()):
        rows.sort(key=lambda row: row[0])
        if len({row[0] for row in rows}) != len(rows):
            raise ValueError(
                f"Task {task_id}: duplicate step index; source rows {[row[5] for row in rows]}"
            )
        if any(right[2] < left[2] + left[3] for left, right in zip(rows, rows[1:])):
            raise ValueError(f"Task {task_id}: ordered stop times overlap or decrease")
        if len({row[4] for row in rows}) != 1:
            raise ValueError(f"Task {task_id}: inconsistent release times across source rows")
        if fleet.supply.capacity_mode != "none":
            raise ValueError(
                "Imported ordered stops currently require no-capacity mode; use location or OD for quantity milestones"
            )
        records.append(
            Task(
                task_id=task_id,
                fleet_id=fleet.fleet_id,
                release_s=rows[0][4],
                steps=tuple(
                    TaskStep(
                        step_index=i + 1,
                        location_id=row[1],
                        scheduled_time_s=row[2],
                        service_duration_s=row[3],
                        source_record_refs=row[5],
                    )
                    for i, row in enumerate(rows)
                ),
                source_record_refs=tuple(ref for row in rows for ref in row[5]),
                source_policy="imported_ordered_tasks@1",
            )
        )
    if len({task.task_id for task in records}) != len(records):
        raise ValueError("Duplicate task IDs in instance input")
    return tuple(sorted(records, key=lambda task: (task.release_s, task.task_id))), history


def generate_daily_tasks(fleet, support, clock, rng, replication_id, *, warmup_hours=0, **kwargs):
    """Retain observation draws; repeat prior civil days on separate semantic streams."""
    tasks, rejected = generate_tasks(fleet, support, clock, rng, replication_id, **kwargs)
    if not warmup_hours:
        return tasks, rejected
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    from mobile_sensing.application.civil_time import local_instant

    day = clock.origin_utc.astimezone(ZoneInfo(clock.display_timezone)).date()
    if clock.observation_start_s != 0 or clock.end_s != clock_seconds("24:00", clock):
        raise ValueError("Generated-demand warm-up requires a full 00:00–24:00 observation day")
    retained = list(tasks)
    rejected = list(rejected)
    lower = -warmup_hours * 3600
    for offset in range(1, 4):
        previous = day - timedelta(days=offset)
        origin = local_instant(previous, "00:00", clock.display_timezone)
        end = local_instant(previous, "24:00", clock.display_timezone)
        shift = (origin - clock.origin_utc).total_seconds()
        if (end - clock.origin_utc).total_seconds() <= lower:
            break
        prior_clock = clock.model_copy(
            update={
                "origin_utc": origin,
                "simulation_start_s": 0,
                "observation_start_s": 0,
                "end_s": (end - origin).total_seconds(),
            }
        )
        prior, issues = generate_tasks(
            fleet, support, prior_clock, rng, f"{replication_id}:warmup:{offset}", **kwargs
        )
        retained.extend(
            task.model_copy(update={"release_s": task.release_s + shift})
            for task in prior
            if lower <= task.release_s + shift < 0
        )
        rejected.extend({**row, "phase": f"warmup_day_{offset}"} for row in issues)
        if len(retained) > kwargs.get("max_tasks", 100000):
            raise ValueError(
                "Observation and warm-up tasks together exceed the task resource limit"
            )
    return tuple(sorted(retained, key=lambda task: (task.release_s, task.task_id))), tuple(rejected)
