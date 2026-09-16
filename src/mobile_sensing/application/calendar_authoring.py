"""Resolve representative weekdays from actual selected GTFS service calendars."""

from collections import Counter
from datetime import date, timedelta

import pandas as pd

from mobile_sensing.application.project_models import ProjectConfig
from mobile_sensing.contracts import ContractModel, stable_id


class CalendarPreview(ContractModel):
    time_mode: str
    resolved_date: str
    timezone: str
    candidate_days: int
    route_counts: dict[str, int]
    interpretation: str


def calendar_days(root, fleet, start=None, end=None):
    from mobile_sensing.datasets.inputs import load_input
    from mobile_sensing.datasets.gtfs import register_gtfs_source

    if not fleet.demand.input_id or not fleet.demand.route_ids:
        raise ValueError("Select a GTFS input and routes before resolving a service day")
    metadata, path = load_input(root, fleet.demand.input_id)
    if metadata["role"] != "gtfs":
        raise ValueError("Service-day selection requires a GTFS input")
    source = register_gtfs_source(path, artifact_root=root)
    trips = pd.read_csv(source.tables["trips"], dtype=str, keep_default_na=False)
    if not set(fleet.demand.route_ids) <= set(trips.route_id):
        raise ValueError("One or more selected routes do not occur in this feed")
    trips = trips[trips.route_id.isin(fleet.demand.route_ids)]
    if trips.empty:
        raise ValueError("Selected routes do not occur in this feed")
    services = set(trips.service_id)
    calendar = (
        pd.read_csv(source.tables["calendar"], dtype=str, keep_default_na=False)
        if "calendar" in source.tables
        else pd.DataFrame()
    )
    exceptions = (
        pd.read_csv(source.tables["calendar_dates"], dtype=str, keep_default_na=False)
        if "calendar_dates" in source.tables
        else pd.DataFrame()
    )
    if not calendar.empty:
        calendar = calendar[calendar.service_id.isin(services)]
    if not exceptions.empty:
        exceptions = exceptions[exceptions.service_id.isin(services)]
    if not calendar.empty and calendar.service_id.duplicated().any():
        raise ValueError("GTFS calendar has duplicate service rows")
    if not exceptions.empty and exceptions.duplicated(["service_id", "date"]).any():
        raise ValueError("GTFS calendar exceptions contain duplicate service/date rows")
    dates = []
    if not calendar.empty:
        dates.extend(calendar.start_date)
        dates.extend(calendar.end_date)
    if not exceptions.empty:
        dates.extend(exceptions.date)
    if not dates:
        raise ValueError("The selected routes have no dated service coverage")

    def parse(value):
        return date(int(value[:4]), int(value[4:6]), int(value[6:8]))

    lower, upper = min(map(parse, dates)), max(map(parse, dates))
    lower = max(lower, start) if start else lower
    upper = min(upper, end) if end else upper
    if (upper - lower).days > 3660:
        raise ValueError("GTFS calendar spans more than ten years; narrow the service period")
    grouped_exceptions = (
        {} if exceptions.empty else {key: value for key, value in exceptions.groupby("date")}
    )
    frequencies = trips.groupby(["service_id", "route_id"]).size()
    result = {}
    for offset in range(max(0, (upper - lower).days + 1)):
        day = lower + timedelta(days=offset)
        compact = day.strftime("%Y%m%d")
        active = set()
        if not calendar.empty:
            active.update(
                calendar.loc[
                    (calendar.start_date <= compact)
                    & (calendar.end_date >= compact)
                    & (calendar[day.strftime("%A").lower()] == "1"),
                    "service_id",
                ]
            )
        for row in grouped_exceptions.get(compact, pd.DataFrame()).itertuples(index=False):
            if row.exception_type == "1":
                active.add(row.service_id)
            elif row.exception_type == "2":
                active.discard(row.service_id)
            else:
                raise ValueError(f"Invalid GTFS calendar exception on {day}")
        counts = {route: 0 for route in fleet.demand.route_ids}
        for (service, route), count in frequencies.items():
            if service in active:
                counts[route] += int(count)
        if sum(counts.values()):
            result[day] = {
                "signature": stable_id("service_pattern", sorted(active)),
                "counts": counts,
            }
    agency = pd.read_csv(source.tables["agency"], dtype=str)
    zones = set(agency.agency_timezone)
    if len(zones) != 1:
        raise ValueError("GTFS service-day resolution requires one agency timezone per feed")
    return result, zones.pop()


def resolve_calendar(root, config):
    config = ProjectConfig.model_validate(config)
    simulation = config.simulation
    zone = simulation.timezone or (
        config.prepared_environment.timezone
        if config.prepared_environment
        else config.environment.timezone
    )
    feeds = [fleet for fleet in config.fleets if fleet.demand.template == "gtfs"]
    route_counts, candidate_count = {}, 0
    if simulation.time_mode == "relative":
        if feeds:
            raise ValueError("GTFS requires Representative weekday or Exact date time mode")
        if any(
            fleet.demand.source == "import" and fleet.demand.time_unit == "datetime"
            for fleet in config.fleets
        ):
            raise ValueError("Dated task instances require Exact date time mode")
        day, zone = date(2001, 1, 1), "UTC"
        interpretation = (
            "Calendar-independent day; the internal reference date has no business meaning."
        )
    elif not feeds:
        day = (
            simulation.service_date
            if simulation.time_mode == "calendar"
            else date(2001, 1, 1) + timedelta(days=simulation.weekday)
        )
        if simulation.time_mode == "weekday":
            zone = "UTC"
        interpretation = (
            "Exact civil day."
            if simulation.time_mode == "calendar"
            else "Representative weekday without dated input; no GTFS schedule is inferred."
        )
    else:
        calendars, zones = {}, set()
        for fleet in feeds:
            if simulation.time_mode == "calendar":
                values, tz = calendar_days(
                    root, fleet, simulation.service_date, simulation.service_date
                )
            else:
                values, tz = calendar_days(
                    root, fleet, simulation.calendar_period_start, simulation.calendar_period_end
                )
            calendars[fleet.fleet_id] = values
            zones.add(tz)
        common = set.intersection(*(set(values) for values in calendars.values()))
        if simulation.time_mode == "weekday":
            common = {day for day in common if day.weekday() == simulation.weekday}
        if not common:
            raise ValueError(
                "No common active service day for the selected routes and period. Select a covered date/weekday or another feed; no timetable was substituted."
            )
        candidate_count = len(common)
        signatures = {
            day: tuple(calendars[fleet.fleet_id][day]["signature"] for fleet in feeds)
            for day in common
        }
        frequency = Counter(signatures.values())
        day = min(common, key=lambda day: (-frequency[signatures[day]], day))
        if simulation.time_mode == "weekday":
            if len(zones) != 1:
                raise ValueError(
                    "Representative feeds must share one timezone; use an explicit calendar scenario"
                )
            zone = next(iter(zones))
        for fleet in feeds:
            route_counts.update(
                {
                    fleet.fleet_id + "/" + key: value
                    for key, value in calendars[fleet.fleet_id][day]["counts"].items()
                }
            )
        interpretation = (
            "Most frequent active service pattern for this weekday; ties use earliest covered date. Actual exceptions and adjacent service days are retained."
            if simulation.time_mode == "weekday"
            else "Exact GTFS calendar and exception-date selection."
        )
    if (
        simulation.calendar_period_start
        and simulation.calendar_period_end
        and simulation.calendar_period_start > simulation.calendar_period_end
    ):
        raise ValueError("Service period end precedes start")
    updates = {"service_date": day}
    if simulation.time_mode == "weekday":
        updates["timezone"] = zone
    config = config.model_copy(update={"simulation": simulation.model_copy(update=updates)})
    return config, CalendarPreview(
        time_mode=simulation.time_mode,
        resolved_date=day.isoformat(),
        timezone=zone,
        candidate_days=candidate_count,
        route_counts=route_counts,
        interpretation=interpretation,
    )
