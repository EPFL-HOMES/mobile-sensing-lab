"""Deterministic demand and availability summaries; no simulation RNG access."""

from mobile_sensing.contracts.base import ContractModel
from mobile_sensing.application.calendar_authoring import CalendarPreview, resolve_calendar
from mobile_sensing.application.civil_time import civil_clock, clock_seconds
from mobile_sensing.application.temporal_authoring import (
    profile_intervals,
    resolve_intervals,
    group_windows,
)


class TemporalBin(ContractModel):
    start_hour: float
    end_hour: float
    expected_tasks: float
    expected_active_vehicles: float | None


class FleetTemporalPreview(ContractModel):
    fleet_id: str
    bins: tuple[TemporalBin, ...]
    expected_task_total: float
    interpretation: str


class TemporalPreview(ContractModel):
    calendar: CalendarPreview
    fleets: tuple[FleetTemporalPreview, ...]


def expected_overlap(start, end, first, latest, duration):
    """Integral of P(A <= t < A + duration), A ~ Uniform(first, latest)."""
    if latest == first:
        return max(0, min(end, first + duration) - max(start, first))

    def primitive(x):
        return (
            max(0, x - first) ** 2
            - max(0, x - latest) ** 2
            - max(0, x - first - duration) ** 2
            + max(0, x - latest - duration) ** 2
        ) / (2 * (latest - first))

    return max(0, primitive(end) - primitive(start))


def temporal_preview(root, config):
    config, calendar = resolve_calendar(root, config)
    clock = civil_clock(config.simulation, calendar.timezone)
    result = []
    for fleet in config.fleets:
        demand = fleet.demand
        if demand.source != "generator":
            continue
        if demand.temporal_mode == "window":
            start, end = clock_seconds(demand.start_time, clock), clock_seconds(
                demand.end_time, clock
            )
            if not clock.observation_start_s <= start < end <= clock.end_s:
                raise ValueError(f"{fleet.name}: demand window must lie inside observation")
            intervals = [(start, end, demand.task_volume)]
        else:
            intervals = resolve_intervals(demand, clock, profile_intervals(root, demand))
            mass = sum(value for _, _, value in intervals)
            intervals = [
                (
                    a,
                    b,
                    (
                        value * (b - a) / 3600
                        if demand.temporal_mode == "rates"
                        else demand.task_volume * value / mass
                    ),
                )
                for a, b, value in intervals
            ]
        windows = group_windows(fleet.supply, clock) if fleet.supply.source == "generated" else None
        bins = []
        start = clock.observation_start_s
        while start < clock.end_s:
            end = min(start + 3600, clock.end_s)
            expected = sum(
                value * max(0, min(end, b) - max(start, a)) / (b - a) for a, b, value in intervals
            )
            if demand.release_mode == "at_start":
                expected = demand.task_volume if start <= intervals[0][0] < end else 0
            active = (
                sum(
                    n * expected_overlap(start, end, a, b, d) / (end - start)
                    for n, a, b, d in windows
                )
                if windows is not None
                else None
            )
            bins.append(
                TemporalBin(
                    start_hour=start / 3600,
                    end_hour=end / 3600,
                    expected_tasks=expected,
                    expected_active_vehicles=active,
                )
            )
            start = end
        result.append(
            FleetTemporalPreview(
                fleet_id=fleet.fleet_id,
                bins=tuple(bins),
                expected_task_total=sum(row.expected_tasks for row in bins),
                interpretation="Expected arrivals and mean vehicles on shift. These are not sensing duration or realized demand. Warm-up uses separate prior-day realizations.",
            )
        )
    return TemporalPreview(calendar=calendar, fleets=tuple(result))
