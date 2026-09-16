"""Bounded temporal profiles, shift groups and deterministic analytic previews."""

from datetime import timedelta
from decimal import Decimal
import math

import numpy as np

from mobile_sensing.application.civil_time import clock_seconds, local_instant
from mobile_sensing.application.project_models import TemporalInterval


def profile_intervals(root, demand):
    if not demand.time_profile_input:
        return demand.time_profile
    from mobile_sensing.application.task_authoring import table_input

    frame, metadata = table_input(root, demand.time_profile_input, {"demand"})
    required = {"start_time", "end_time", "value"}
    if not required <= set(frame) or len(frame) > 1000:
        raise ValueError("Temporal profile needs start_time, end_time, value and at most 1000 rows")
    values = []
    for number, row in enumerate(frame.to_dict("records"), 2):
        try:
            values.append(
                TemporalInterval(
                    start_time=str(row["start_time"]),
                    end_time=str(row["end_time"]),
                    value=float(row["value"]),
                )
            )
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Input {metadata['name']}, source row {number}: {exc}") from exc
    return tuple(values)


def resolve_intervals(demand, clock, intervals=None):
    rows = []
    intervals = demand.time_profile if intervals is None else intervals
    for number, interval in enumerate(intervals, 1):
        start, end = clock_seconds(interval.start_time, clock), clock_seconds(
            interval.end_time, clock
        )
        if not clock.observation_start_s <= start < end <= clock.end_s:
            raise ValueError(
                f"Temporal profile row {number}: interval lies outside the observation window"
            )
        rows.append((start, end, interval.value))
    rows.sort()
    if not rows or any(a[1] > b[0] for a, b in zip(rows, rows[1:])):
        raise ValueError("Temporal intervals must be nonempty and nonoverlapping")
    mass = sum(value for _, _, value in rows)
    if not math.isfinite(mass) or (demand.temporal_mode == "shares" and mass <= 0):
        raise ValueError("Temporal shares require finite positive total mass")
    return rows


def draw_releases(demand, clock, stream, intervals=None, max_tasks=100000):
    rows = resolve_intervals(demand, clock, intervals)
    values = np.array([row[2] for row in rows], dtype=float)
    if demand.temporal_mode == "shares":
        if demand.task_volume > max_tasks:
            raise ValueError("Requested temporal task total exceeds the task resource limit")
        probabilities = values / values.sum()
        if demand.volume_mode == "fixed":
            counts = stream.multinomial(int(demand.task_volume), probabilities)
        else:
            counts = stream.poisson(demand.task_volume * probabilities)
    else:
        means = values * np.array([(end - start) / 3600 for start, end, _ in rows])
        if not np.isfinite(means).all() or means.sum() > max_tasks:
            raise ValueError("Integrated temporal demand rate exceeds the task resource limit")
        counts = stream.poisson(means)
    if counts.sum() > max_tasks:
        raise ValueError("Realized temporal task total exceeds the task resource limit")
    return np.sort(
        np.concatenate(
            [stream.uniform(start, end, int(count)) for (start, end, _), count in zip(rows, counts)]
        ),
        kind="stable",
    )


def shifted_clock_time(text, day_offset, clock):
    from zoneinfo import ZoneInfo

    day = clock.origin_utc.astimezone(ZoneInfo(clock.display_timezone)).date() + timedelta(
        days=day_offset
    )
    return (local_instant(day, text, clock.display_timezone) - clock.origin_utc).total_seconds()


def group_windows(supply, clock):
    groups = []
    if supply.shift_groups:
        for group in supply.shift_groups:
            start = shifted_clock_time(group.start_time, group.day_offset, clock)
            latest = shifted_clock_time(group.latest_start, group.day_offset, clock)
            end = latest + group.work_hours * 3600
            if latest < start:
                raise ValueError(f"Shift {group.name}: latest start precedes start")
            if end > clock_seconds(supply.operating_end, clock):
                raise ValueError(f"Shift {group.name}: work extends beyond the operating window")
            if group.day_offset == 0 and start < clock_seconds(supply.operating_start, clock):
                raise ValueError(f"Shift {group.name}: start precedes the operating window")
            groups.append((group.count, start, latest, group.work_hours * 3600))
    else:
        start = clock_seconds(supply.operating_start, clock)
        latest = (
            clock_seconds(supply.latest_start, clock)
            if supply.activation == "uniform_bounded"
            else start
        )
        duration = supply.work_hours * 3600
        if latest < start or latest + duration > clock_seconds(supply.operating_end, clock):
            raise ValueError(
                "Vehicle starts and fixed work duration must fit entirely within the operating window"
            )
        groups.append((supply.fleet_size, start, latest, duration))
    return groups


def expand_range(value, *, integers=False, limit=10000):
    a, b, step = (Decimal(str(x)) for x in (value.minimum, value.maximum, value.step))
    if integers and any(x != x.to_integral_value() for x in (a, b, step)):
        raise ValueError("Sensor-count ranges require integer endpoints and steps")
    count = int((b - a) // step) + 1
    if count > limit:
        raise ValueError(f"Range exceeds {limit} levels; increase its interval")
    result = [a + i * step for i in range(count)]
    if result[-1] != b:
        result.append(b)
    if len(result) > limit:
        raise ValueError(f"Range exceeds {limit} levels")
    return tuple(int(x) if integers else float(x) for x in result)


def resolve_portfolio(editor, vehicle_counts):
    budgets = expand_range(editor.budget_range, limit=64) if editor.budget_range else editor.budgets
    if len(budgets) > 64:
        raise ValueError("Portfolio analysis supports at most 64 budget levels")
    fleets = []
    combinations = 1
    for fleet in editor.fleets:
        counts = (
            expand_range(fleet.count_range, integers=True) if fleet.count_range else fleet.counts
        )
        if not counts or any(
            count < 0 or count > vehicle_counts.get(fleet.fleet_id, -1) for count in counts
        ):
            raise ValueError(
                f"Sensor counts for {fleet.fleet_id} must lie inside the physical catalog"
            )
        counts = tuple(sorted(set(counts)))
        combinations *= len(counts)
        if combinations > 100000:
            raise ValueError("Count ranges exceed 100000 combinations; increase count intervals")
        fleets.append(fleet.model_copy(update={"counts": counts}))
    if not budgets or any(not math.isfinite(x) or x < 0 for x in budgets):
        raise ValueError("Budgets must be finite nonnegative amounts")
    return editor.model_copy(
        update={"budgets": tuple(sorted(set(budgets))), "fleets": tuple(fleets)}
    )
