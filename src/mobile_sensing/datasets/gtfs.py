"""GTFS service-day normalization and deterministic physical-duty reconstruction."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd
from pydantic import field_validator, model_validator
from shapely.geometry import Point

from mobile_sensing.contracts import (
    ArtifactDependency,
    ArtifactRef,
    AssignmentPlan,
    AssignmentRow,
    CatalogIdentity,
    ContractModel,
    LocationRef,
    Task,
    TaskStep,
    VehicleAvailability,
    VehicleKey,
    VehicleSpec,
    canonical_json_text,
    scientific_hash,
    scientific_projection,
    stable_id,
)
from mobile_sensing.datasets.parsing import LocationResolver
from mobile_sensing.datasets.storage import publish_dataset
from mobile_sensing.simulation.timing import (
    TIMING_ALGORITHM_VERSION,
    TaskTiming,
    UnreachableTaskLeg,
    estimate_task_timing,
    task_with_release,
)


GTFS_RECONSTRUCTION_VERSION = "gtfs-service-day-reconstruction@1"
GTFS_SOURCE_VERSION = "bounded-gtfs-source@1"

_ALIASES = {
    "agency": ("agency.txt", "agency.csv"),
    "routes": ("routes.txt", "routes.csv", "buses.csv"),
    "trips": ("trips.txt", "trips.csv"),
    "stops": ("stops.txt", "stops.csv"),
    "stop_times": ("stop_times.txt", "stop_times.csv"),
    "calendar": ("calendar.txt", "calendar.csv"),
    "calendar_dates": ("calendar_dates.txt", "calendar_dates.csv"),
    "frequencies": ("frequencies.txt", "frequencies.csv"),
    "transfers": ("transfers.txt", "transfers.csv"),
}
_REQUIRED = {"agency", "routes", "trips", "stops", "stop_times"}


class GTFSImportError(ValueError):
    """A selected GTFS scope cannot be normalized without changing semantics."""


class GTFSReconstructionConfig(ContractModel):
    schema_version: Literal["2.0"]
    adapter: Literal["demand.gtfs_reconstruction@1"] = "demand.gtfs_reconstruction@1"
    fleet_id: str
    service_date: str
    route_ids: tuple[str, ...]
    routing_profile_id: str
    turnaround_duration_s: float
    error_policy: Literal["strict", "quarantine_invalid_trips"] = "strict"
    allow_instantaneous_missing_time_copy: bool = False
    civil_day_complete_requested: Literal[False] = False
    user_vehicle_assignments: dict[str, str] = {}
    compatible_group_overrides: dict[str, str] = {}
    max_selected_trips: int = 10_000
    max_stop_time_rows: int = 2_000_000

    scientific_identity_excluded_fields = frozenset({"max_selected_trips", "max_stop_time_rows"})

    @field_validator("fleet_id", "routing_profile_id")
    @classmethod
    def nonempty(cls, value: str) -> str:
        if not value:
            raise ValueError("identifier must be nonempty")
        return value

    @field_validator("service_date")
    @classmethod
    def valid_date(cls, value: str) -> str:
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("service_date must use YYYY-MM-DD") from exc
        if parsed.isoformat() != value:
            raise ValueError("service_date must use canonical YYYY-MM-DD")
        return value

    @field_validator("route_ids")
    @classmethod
    def canonical_routes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or any(not item for item in value):
            raise ValueError("route_ids must be nonempty strings")
        if value != tuple(sorted(set(value))):
            raise ValueError("route_ids must be sorted and unique")
        return value

    @model_validator(mode="after")
    def validate_limits(self):
        if self.turnaround_duration_s < 0:
            raise ValueError("turnaround_duration_s must be nonnegative")
        if self.max_selected_trips <= 0 or self.max_stop_time_rows <= 0:
            raise ValueError("resource limits must be positive")
        if any(not key or not value for key, value in self.user_vehicle_assignments.items()):
            raise ValueError("user vehicle assignment IDs must be nonempty")
        if any(not key or not value for key, value in self.compatible_group_overrides.items()):
            raise ValueError("compatible group overrides must be nonempty")
        return self


@dataclass(frozen=True, slots=True)
class GTFSSource:
    directory: Path
    source_id: str
    content_hash: str
    tables: Mapping[str, Path]


@dataclass(frozen=True, slots=True)
class GTFSPreflight:
    service_date: str
    agency_timezone: str
    service_origin_utc: datetime
    route_ids: tuple[str, ...]
    active_service_ids: tuple[str, ...]
    selected_trip_ids: tuple[str, ...]
    counts: Mapping[str, Any]
    source: GTFSSource


@dataclass(frozen=True, slots=True)
class GTFSReconstruction:
    reference: ArtifactRef
    directory: Path
    tasks: tuple[Task, ...]
    locations: tuple[LocationRef, ...]
    vehicles: tuple[VehicleSpec, ...]
    availability: tuple[VehicleAvailability, ...]
    assignments: AssignmentPlan
    catalog: CatalogIdentity
    diagnostics: Mapping[str, Any]


@dataclass(slots=True)
class _Trip:
    trip_id: str
    route_id: str
    block_id: str
    task: Task
    first_location_id: str
    last_location_id: str
    scheduled_start_s: float
    standalone: TaskTiming


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _discover(directory: Path) -> GTFSSource:
    directory = directory.resolve()
    if not directory.is_dir():
        raise GTFSImportError("GTFS source must resolve to a directory")
    lower = {item.name.casefold(): item for item in directory.iterdir() if item.is_file()}
    tables: dict[str, Path] = {}
    for logical, aliases in _ALIASES.items():
        matches = [lower[name] for name in aliases if name in lower]
        if len(matches) > 1:
            raise GTFSImportError(f"ambiguous aliases for GTFS table {logical!r}")
        if matches:
            if matches[0].is_symlink():
                raise GTFSImportError("GTFS sources may not contain symlinks")
            tables[logical] = matches[0]
    missing = sorted(_REQUIRED - set(tables))
    if missing:
        raise GTFSImportError(f"GTFS source lacks required tables: {missing}")
    if "calendar" not in tables and "calendar_dates" not in tables:
        raise GTFSImportError("GTFS source requires calendar or calendar_dates")
    hashes = {name: _sha256_file(path) for name, path in sorted(tables.items())}
    content_hash = scientific_hash({"algorithm": GTFS_SOURCE_VERSION, "tables": hashes})
    return GTFSSource(
        directory=directory,
        source_id=stable_id("gtfs_source", content_hash),
        content_hash=content_hash,
        tables=MappingProxyType(tables),
    )


def register_gtfs_source(
    source: str | Path,
    *,
    artifact_root: str | Path,
    max_files: int = 128,
    max_input_bytes: int = 1024**3,
    max_expanded_bytes: int = 2 * 1024**3,
) -> GTFSSource:
    """Copy a bounded directory/ZIP into immutable managed GTFS storage."""

    source_path = Path(source).resolve()
    root = Path(artifact_root).resolve() / "raw_gtfs"
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".gtfs-staging-", dir=root) as temp_name:
        staging = Path(temp_name)
        if source_path.is_dir():
            files = [item for item in source_path.iterdir() if item.is_file()]
            if any(item.is_symlink() for item in files):
                raise GTFSImportError("GTFS sources may not contain symlinks")
            if (
                len(files) > max_files
                or sum(item.stat().st_size for item in files) > max_expanded_bytes
            ):
                raise GTFSImportError("GTFS source exceeds configured extraction limits")
            for item in files:
                shutil.copyfile(item, staging / item.name)
        elif source_path.is_file() and zipfile.is_zipfile(source_path):
            if source_path.stat().st_size > max_input_bytes:
                raise GTFSImportError("GTFS archive exceeds configured input byte limit")
            with zipfile.ZipFile(source_path) as archive:
                infos = [item for item in archive.infolist() if not item.is_dir()]
                if (
                    len(infos) > max_files
                    or sum(item.file_size for item in infos) > max_expanded_bytes
                ):
                    raise GTFSImportError("GTFS archive exceeds configured extraction limits")
                for info in infos:
                    parts = Path(info.filename).parts
                    if len(parts) != 1 or parts[0] in {"", ".", ".."}:
                        raise GTFSImportError("GTFS archive entries must be flat safe filenames")
                    target = staging / parts[0]
                    with archive.open(info) as reader, target.open("wb") as writer:
                        shutil.copyfileobj(reader, writer)
        else:
            raise GTFSImportError("GTFS source must be a directory or ZIP file")
        discovered = _discover(staging)
        retained_paths = {path.resolve() for path in discovered.tables.values()}
        for path in staging.iterdir():
            if path.is_file() and path.resolve() not in retained_paths:
                path.unlink()
        final = root / discovered.source_id
        if final.exists():
            existing = _discover(final)
            if existing.content_hash != discovered.content_hash:
                raise GTFSImportError("existing raw GTFS identity mismatch")
            return existing
        os.replace(staging, final)
    return _discover(final)


def discover_gtfs_directory(source: str | Path) -> GTFSSource:
    """Inspect an already managed/local directory without copying it."""

    return _discover(Path(source))


def _verified_source(source: GTFSSource) -> GTFSSource:
    current = _discover(source.directory)
    if current.source_id != source.source_id or current.content_hash != source.content_hash:
        raise GTFSImportError("GTFS source changed after discovery")
    return current


def gtfs_service_origin(service_date: str, agency_timezone: str) -> datetime:
    """Return local-noon-minus-12-elapsed-hours as a UTC instant."""

    day = date.fromisoformat(service_date)
    try:
        zone = ZoneInfo(agency_timezone)
    except ZoneInfoNotFoundError as exc:
        raise GTFSImportError(f"unknown agency timezone {agency_timezone!r}") from exc
    local_noon = datetime(day.year, day.month, day.day, 12, tzinfo=zone)
    return local_noon.astimezone(timezone.utc) - timedelta(hours=12)


def parse_gtfs_time(value: str) -> int:
    match = re.fullmatch(r"([0-9]+):([0-9]{2}):([0-9]{2})", str(value))
    if match is None:
        raise GTFSImportError(f"invalid GTFS time {value!r}")
    hours, minutes, seconds = map(int, match.groups())
    if minutes >= 60 or seconds >= 60:
        raise GTFSImportError(f"invalid GTFS time {value!r}")
    return hours * 3600 + minutes * 60 + seconds


def gtfs_time_to_utc(service_date: str, agency_timezone: str, value: str) -> datetime:
    """Convert one GTFS clock value without civil-midnight or modulo assumptions."""

    return gtfs_service_origin(service_date, agency_timezone) + timedelta(
        seconds=parse_gtfs_time(value)
    )


def _read(path: Path, *, usecols: Sequence[str] | None = None) -> pd.DataFrame:
    return pd.read_csv(path, dtype=str, keep_default_na=False, usecols=usecols)


def _active_services(source: GTFSSource, day: date) -> set[str]:
    compact = day.strftime("%Y%m%d")
    active: set[str] = set()
    if "calendar" in source.tables:
        calendar = _read(source.tables["calendar"])
        required = {"service_id", "start_date", "end_date", day.strftime("%A").casefold()}
        if not required <= set(calendar):
            raise GTFSImportError("calendar table lacks required fields")
        weekday = day.strftime("%A").casefold()
        mask = (
            (calendar["start_date"] <= compact)
            & (calendar["end_date"] >= compact)
            & (calendar[weekday] == "1")
        )
        active.update(calendar.loc[mask, "service_id"])
    if "calendar_dates" in source.tables:
        seen: dict[str, str] = {}
        for chunk in pd.read_csv(
            source.tables["calendar_dates"],
            dtype=str,
            keep_default_na=False,
            usecols=["service_id", "date", "exception_type"],
            chunksize=250_000,
        ):
            for row in chunk.loc[chunk["date"] == compact].itertuples(index=False):
                previous = seen.get(row.service_id)
                if previous is not None and previous != row.exception_type:
                    raise GTFSImportError("conflicting calendar exceptions for one service/date")
                if row.exception_type not in {"1", "2"}:
                    raise GTFSImportError("calendar exception_type must be 1 or 2")
                seen[row.service_id] = row.exception_type
        for service_id, exception_type in seen.items():
            if exception_type == "1":
                active.add(service_id)
            else:
                active.discard(service_id)
    return active


def _selected_trips(source: GTFSSource, config: GTFSReconstructionConfig):
    agency = _read(source.tables["agency"])
    if "agency_timezone" not in agency or agency.empty:
        raise GTFSImportError("agency table requires agency_timezone")
    timezones = tuple(sorted(set(agency["agency_timezone"])))
    if len(timezones) != 1 or not timezones[0]:
        raise GTFSImportError("v1 reconstruction requires exactly one agency timezone")
    origin = gtfs_service_origin(config.service_date, timezones[0])
    active = _active_services(source, date.fromisoformat(config.service_date))
    routes = _read(source.tables["routes"])
    if "route_id" not in routes or routes["route_id"].duplicated().any():
        raise GTFSImportError("routes require unique route_id strings")
    missing_routes = sorted(set(config.route_ids) - set(routes["route_id"]))
    if missing_routes:
        raise GTFSImportError(f"selected route IDs are absent: {missing_routes}")
    selected_routes = routes.loc[routes["route_id"].isin(config.route_ids)]
    for field in ("continuous_pickup", "continuous_drop_off"):
        if field in selected_routes and any(
            value not in {"", "1"} for value in selected_routes[field]
        ):
            raise GTFSImportError(f"selected routes use unsupported {field}")
    trips = _read(source.tables["trips"])
    required = {"route_id", "service_id", "trip_id"}
    if not required <= set(trips) or trips["trip_id"].duplicated().any():
        raise GTFSImportError("trips require unique trip_id and route/service foreign keys")
    active_trips = trips.loc[trips["service_id"].isin(active)]
    route_scope = trips.loc[trips["route_id"].isin(config.route_ids)]
    selected = trips.loc[
        trips["route_id"].isin(config.route_ids) & trips["service_id"].isin(active)
    ].copy()
    if len(selected) > config.max_selected_trips:
        raise GTFSImportError("selected trips exceed configured resource limit")
    if selected.empty:
        raise GTFSImportError("selected service date/routes contain no active trips")
    selected_ids = set(selected["trip_id"])
    if "frequencies" in source.tables:
        frequencies = _read(source.tables["frequencies"], usecols=["trip_id"])
        if any(frequencies["trip_id"].isin(selected_ids)):
            raise GTFSImportError("selected trips depend on unsupported frequencies")
    if "transfers" in source.tables:
        transfers = _read(source.tables["transfers"])
        linked = set()
        for field in ("from_trip_id", "to_trip_id"):
            if field in transfers:
                linked.update(value for value in transfers[field] if value)
        if linked & selected_ids:
            raise GTFSImportError("selected trips depend on unsupported linked-trip transfers")
    counts = {
        "source_routes": len(routes),
        "source_trips": len(trips),
        "active_services": len(active),
        "active_trips_all_routes": len(active_trips),
        "selected_route_trips_all_services": len(route_scope),
        "selected_routes": len(config.route_ids),
        "selected_trips": len(selected),
        "selected_trip_counts_by_route": {
            str(key): int(value)
            for key, value in selected.groupby("route_id", sort=True).size().items()
        },
    }
    return selected.sort_values("trip_id", kind="stable"), active, timezones[0], origin, counts


def preflight_gtfs(source: GTFSSource, config: GTFSReconstructionConfig) -> GTFSPreflight:
    """Validate service/route scope without requiring a routable environment."""

    source = _verified_source(source)
    selected, active, agency_timezone, origin, counts = _selected_trips(source, config)
    return GTFSPreflight(
        service_date=config.service_date,
        agency_timezone=agency_timezone,
        service_origin_utc=origin,
        route_ids=config.route_ids,
        active_service_ids=tuple(sorted(active)),
        selected_trip_ids=tuple(sorted(selected["trip_id"])),
        counts=counts,
        source=source,
    )


def _load_stop_times(
    source: GTFSSource, selected_ids: set[str], config: GTFSReconstructionConfig
) -> pd.DataFrame:
    pieces = []
    observed = 0
    for chunk in pd.read_csv(
        source.tables["stop_times"], dtype=str, keep_default_na=False, chunksize=100_000
    ):
        selected = chunk.loc[chunk["trip_id"].isin(selected_ids)].copy()
        observed += len(selected)
        if observed > config.max_stop_time_rows:
            raise GTFSImportError("selected stop-time rows exceed configured resource limit")
        pieces.append(selected)
    if not pieces:
        raise GTFSImportError("stop_times table contains no rows")
    return pd.concat(pieces, ignore_index=True)


def _catalog(vehicles: Sequence[VehicleSpec]) -> CatalogIdentity:
    ordered = tuple(sorted(vehicles, key=lambda item: (item.key.fleet_id, item.key.vehicle_id)))
    physical = scientific_hash(
        [
            {
                "key": item.key,
                "initial_location_id": item.initial_location_id,
                "assigned_area_ids": item.assigned_area_ids,
                "identity_provenance": item.identity_provenance,
            }
            for item in ordered
        ]
    )
    keys = tuple(item.key for item in ordered)
    catalog_hash = scientific_hash(
        {
            "vehicle_keys": [[item.fleet_id, item.vehicle_id] for item in keys],
            "physical_metadata_hash": physical,
        }
    )
    return CatalogIdentity(
        catalog_id=stable_id("catalog", catalog_hash),
        vehicle_keys=keys,
        physical_metadata_hash=physical,
        catalog_hash=catalog_hash,
    )


def _frames(
    *,
    preflight: GTFSPreflight,
    tasks: Sequence[Task],
    locations: Sequence[LocationRef],
    vehicles: Sequence[VehicleSpec],
    availability: Sequence[VehicleAvailability],
    assignments: AssignmentPlan,
    catalog: CatalogIdentity,
    trip_rows: list[dict[str, Any]],
    stop_rows: list[dict[str, Any]],
    duty_rows: list[dict[str, Any]],
    deadheads: list[dict[str, Any]],
    diagnostics: Mapping[str, Any],
    route_failures: list[dict[str, Any]],
) -> dict[str, pd.DataFrame]:
    return {
        "assignments": pd.DataFrame(
            [
                {
                    "fleet_id": row.vehicle.fleet_id,
                    "vehicle_id": row.vehicle.vehicle_id,
                    "order_index": row.order_index,
                    "task_id": row.task_id,
                }
                for row in assignments.rows
            ]
        ),
        "deadheads": pd.DataFrame(
            deadheads,
            columns=(
                "vehicle_id",
                "from_trip_id",
                "to_trip_id",
                "distance_m",
                "travel_time_s",
                "turnaround_s",
                "idle_slack_s",
            ),
        ),
        "catalog_identity": pd.DataFrame([catalog.model_dump(mode="json")]),
        "diagnostics": pd.DataFrame([{"diagnostics_json": canonical_json_text(diagnostics)}]),
        "duties": pd.DataFrame(duty_rows),
        "gtfs_metadata": pd.DataFrame(
            [
                {
                    "service_date": preflight.service_date,
                    "agency_timezone": preflight.agency_timezone,
                    "service_origin_utc": preflight.service_origin_utc.isoformat(),
                    "route_ids_json": canonical_json_text(preflight.route_ids),
                    "source_content_hash": preflight.source.content_hash,
                }
            ]
        ),
        "locations": pd.DataFrame(
            [
                item.model_dump(mode="json")
                for item in sorted(locations, key=lambda x: x.location_id)
            ]
        ),
        "route_failures": pd.DataFrame(route_failures, columns=("trip_id", "reason")),
        "task_steps": pd.DataFrame(
            [
                {
                    "task_id": task.task_id,
                    **step.model_dump(mode="json"),
                }
                for task in sorted(tasks, key=lambda x: x.task_id)
                for step in task.steps
            ]
        ),
        "tasks": pd.DataFrame(
            [
                {
                    "task_id": item.task_id,
                    "fleet_id": item.fleet_id,
                    "release_s": item.release_s,
                    "kind": item.kind,
                    "source_record_refs_json": canonical_json_text(item.source_record_refs),
                    "source_policy": item.source_policy,
                }
                for item in sorted(tasks, key=lambda x: x.task_id)
            ]
        ),
        "trip_diagnostics": pd.DataFrame(trip_rows),
        "trip_stops": pd.DataFrame(stop_rows),
        "vehicle_availability": pd.DataFrame(
            [item.model_dump(mode="json") for item in availability]
        ),
        "vehicle_catalog": pd.DataFrame([item.model_dump(mode="json") for item in vehicles]),
    }


def reconstruct_gtfs(
    source: GTFSSource,
    config: GTFSReconstructionConfig,
    *,
    artifact_root: str | Path,
    resolver: LocationResolver,
    sensing_boundary=None,
) -> GTFSReconstruction:
    """Normalize one selected service day and publish a fixed duty catalog."""

    config = GTFSReconstructionConfig.model_validate(config)
    if resolver.routing is None or resolver.routing_profile_id != config.routing_profile_id:
        raise GTFSImportError("GTFS reconstruction requires the configured shared routing service")
    source = _verified_source(source)
    selected, active, agency_timezone, origin, counts = _selected_trips(source, config)
    preflight = GTFSPreflight(
        service_date=config.service_date,
        agency_timezone=agency_timezone,
        service_origin_utc=origin,
        route_ids=config.route_ids,
        active_service_ids=tuple(sorted(active)),
        selected_trip_ids=tuple(sorted(selected["trip_id"])),
        counts=counts,
        source=source,
    )
    selected_ids = set(selected["trip_id"])
    if config.user_vehicle_assignments and set(config.user_vehicle_assignments) != selected_ids:
        raise GTFSImportError("user vehicle assignments must cover exactly the selected trips")
    unknown_groups = sorted(set(config.compatible_group_overrides) - selected_ids)
    if unknown_groups:
        raise GTFSImportError(f"group overrides reference unselected trips: {unknown_groups}")

    raw_stops = _read(source.tables["stops"])
    if not {"stop_id", "stop_lat", "stop_lon"} <= set(raw_stops):
        raise GTFSImportError("stops table lacks stop_id/stop_lat/stop_lon")
    if raw_stops["stop_id"].duplicated().any():
        raise GTFSImportError("stops require unique stop_id")
    raw_stops = raw_stops.set_index("stop_id", drop=False)
    stop_times = _load_stop_times(source, selected_ids, config)
    required_stop_fields = {"trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"}
    if not required_stop_fields <= set(stop_times):
        raise GTFSImportError("stop_times lacks required fields")
    for field in ("continuous_pickup", "continuous_drop_off"):
        if field in stop_times and any(value not in {"", "1"} for value in stop_times[field]):
            raise GTFSImportError(f"selected stop times use unsupported {field}")

    resolved: dict[str, LocationRef] = {}
    trips: list[_Trip] = []
    trip_rows: list[dict[str, Any]] = []
    stop_rows: list[dict[str, Any]] = []
    route_failures: list[dict[str, Any]] = []
    crossing_count = 0
    trip_lookup = selected.set_index("trip_id", drop=False)
    for trip_id in sorted(selected_ids):
        try:
            local_stop_rows: list[dict[str, Any]] = []
            is_crossing = False
            rows = stop_times.loc[stop_times["trip_id"] == trip_id].copy()
            if rows.empty:
                raise GTFSImportError("selected trip has no stop times")
            try:
                rows["_sequence"] = rows["stop_sequence"].map(int)
            except ValueError as exc:
                raise GTFSImportError("stop_sequence must be an integer") from exc
            rows = rows.sort_values(["_sequence"], kind="stable")
            seq = tuple(rows["_sequence"])
            if len(set(seq)) != len(seq) or any(right <= left for left, right in zip(seq, seq[1:])):
                raise GTFSImportError("stop_sequence must be unique and strictly increasing")
            steps: list[TaskStep] = []
            prior_departure = -float("inf")
            inside_flags: list[bool] = []
            for step_index, row in enumerate(rows.itertuples(index=False), start=1):
                if row.stop_id not in raw_stops.index:
                    raise GTFSImportError(f"unknown stop_id {row.stop_id!r}")
                arrival_raw, departure_raw = row.arrival_time, row.departure_time
                if not arrival_raw or not departure_raw:
                    if not config.allow_instantaneous_missing_time_copy or (
                        not arrival_raw and not departure_raw
                    ):
                        raise GTFSImportError("missing required stop timing is unsupported")
                    arrival_raw = arrival_raw or departure_raw
                    departure_raw = departure_raw or arrival_raw
                arrival = float(parse_gtfs_time(arrival_raw))
                departure = float(parse_gtfs_time(departure_raw))
                if arrival > departure or arrival < prior_departure:
                    raise GTFSImportError("trip schedule is not nondecreasing")
                prior_departure = departure
                stop = raw_stops.loc[row.stop_id]
                location_id = stable_id(
                    "gtfs_stop",
                    {"stop_id": row.stop_id, "lon": stop.stop_lon, "lat": stop.stop_lat},
                )
                if location_id not in resolved:
                    resolved[location_id] = resolver.by_coordinates(
                        location_id=location_id,
                        x=stop.stop_lon,
                        y=stop.stop_lat,
                        source_crs="EPSG:4326",
                    )
                location = resolved[location_id]
                if sensing_boundary is not None:
                    inside_flags.append(
                        sensing_boundary.covers(Point(location.snapped_x, location.snapped_y))
                    )
                steps.append(
                    TaskStep(
                        step_index=step_index,
                        location_id=location_id,
                        scheduled_time_s=arrival,
                        service_duration_s=departure - arrival,
                        source_record_refs=(f"stop_time:{trip_id}:{row.stop_sequence}",),
                    )
                )
                local_stop_rows.append(
                    {
                        "trip_id": trip_id,
                        "step_index": step_index,
                        "source_stop_sequence": int(row.stop_sequence),
                        "stop_id": row.stop_id,
                        "location_id": location_id,
                        "arrival_s": arrival,
                        "departure_s": departure,
                    }
                )
            if inside_flags and any(inside_flags) and not all(inside_flags):
                is_crossing = True
            selected_row = trip_lookup.loc[trip_id]
            task = Task(
                task_id=stable_id(
                    "gtfs_trip_task",
                    {
                        "service_date": config.service_date,
                        "route_id": selected_row.route_id,
                        "trip_id": trip_id,
                    },
                ),
                fleet_id=config.fleet_id,
                release_s=steps[0].scheduled_time_s,
                steps=tuple(steps),
                source_record_refs=(f"trip:{trip_id}",),
            )
            assert steps[0].scheduled_time_s is not None
            standalone = estimate_task_timing(
                task,
                start_location_id=steps[0].location_id,
                start_s=steps[0].scheduled_time_s,
                locations=resolved,
                routing=resolver.routing,
                profile_id=config.routing_profile_id,
            )
            trips.append(
                _Trip(
                    trip_id=trip_id,
                    route_id=selected_row.route_id,
                    block_id=str(selected_row.get("block_id", "")),
                    task=task,
                    first_location_id=steps[0].location_id,
                    last_location_id=steps[-1].location_id,
                    scheduled_start_s=steps[0].scheduled_time_s,
                    standalone=standalone,
                )
            )
            crossing_count += int(is_crossing)
            stop_rows.extend(local_stop_rows)
            trip_rows.append(
                {
                    "trip_id": trip_id,
                    "route_id": selected_row.route_id,
                    "status": "accepted",
                    "stop_count": len(steps),
                    "nominal_finish_s": standalone.end_s,
                    "maximum_lateness_s": max(item.lateness_s for item in standalone.steps),
                    "reason": "",
                }
            )
        except (GTFSImportError, ValueError, UnreachableTaskLeg) as exc:
            route_failures.append({"trip_id": trip_id, "reason": str(exc)})
            if config.error_policy == "strict":
                raise GTFSImportError(f"trip {trip_id!r}: {exc}") from exc
            trip_rows.append(
                {
                    "trip_id": trip_id,
                    "route_id": trip_lookup.loc[trip_id].route_id,
                    "status": "rejected",
                    "stop_count": 0,
                    "nominal_finish_s": None,
                    "maximum_lateness_s": None,
                    "reason": str(exc),
                }
            )
    if not trips:
        raise GTFSImportError("no selected trip survived normalization")

    accepted_ids = {item.trip_id for item in trips}
    if config.user_vehicle_assignments and set(config.user_vehicle_assignments) != accepted_ids:
        raise GTFSImportError("quarantine changed the scope of the supplied vehicle assignment")
    chains: list[tuple[str, str, list[_Trip]]] = []
    if config.user_vehicle_assignments:
        grouped: dict[str, list[_Trip]] = defaultdict(list)
        for trip in trips:
            grouped[config.user_vehicle_assignments[trip.trip_id]].append(trip)
        chains = [(vehicle_id, "uploaded", items) for vehicle_id, items in sorted(grouped.items())]
    else:
        block_groups: dict[str, list[_Trip]] = defaultdict(list)
        inferred_groups: dict[str, list[_Trip]] = defaultdict(list)
        for trip in trips:
            if trip.block_id:
                block_groups[trip.block_id].append(trip)
            else:
                inferred_groups[
                    config.compatible_group_overrides.get(trip.trip_id, trip.route_id)
                ].append(trip)
        for block, items in sorted(block_groups.items()):
            ordered = sorted(items, key=lambda item: (item.scheduled_start_s, item.trip_id))
            vehicle_id = stable_id(
                "gtfs_vehicle",
                {
                    "date": config.service_date,
                    "block": block,
                    "trips": [item.trip_id for item in ordered],
                },
            )
            chains.append((vehicle_id, "gtfs_block", ordered))
        for group, items in sorted(inferred_groups.items()):
            duty_lists: list[list[_Trip]] = []
            duty_finishes: list[float] = []
            for trip in sorted(items, key=lambda item: (item.scheduled_start_s, item.trip_id)):
                candidates = []
                for index, duty in enumerate(duty_lists):
                    last = duty[-1]
                    route = resolver.routing.route(
                        config.routing_profile_id,
                        resolved[last.last_location_id].node_id,
                        resolved[trip.first_location_id].node_id,
                    )
                    if not route.reachable:
                        continue
                    ready = (
                        duty_finishes[index] + config.turnaround_duration_s + route.total_duration_s
                    )
                    if ready <= trip.scheduled_start_s:
                        provisional_id = stable_id(
                            "gtfs_duty",
                            {
                                "date": config.service_date,
                                "group": group,
                                "first_trip": duty[0].trip_id,
                            },
                        )
                        candidates.append(
                            (
                                route.total_duration_s,
                                trip.scheduled_start_s - ready,
                                provisional_id,
                                index,
                            )
                        )
                if candidates:
                    index = min(candidates)[3]
                    duty_lists[index].append(trip)
                    duty_finishes[index] = trip.standalone.end_s
                else:
                    duty_lists.append([trip])
                    duty_finishes.append(trip.standalone.end_s)
            for duty in duty_lists:
                vehicle_id = stable_id(
                    "gtfs_vehicle",
                    {
                        "date": config.service_date,
                        "group": group,
                        "trips": [item.trip_id for item in duty],
                    },
                )
                chains.append((vehicle_id, "inferred_duty", duty))

    all_tasks: list[Task] = []
    vehicles: list[VehicleSpec] = []
    availability: list[VehicleAvailability] = []
    assignment_rows: list[AssignmentRow] = []
    duty_rows: list[dict[str, Any]] = []
    deadheads: list[dict[str, Any]] = []
    owned_trips: set[str] = set()
    availability_scenario_id = stable_id(
        "gtfs_availability",
        {
            "service_date": config.service_date,
            "route_ids": config.route_ids,
            "environment": resolver.environment.content_hash,
        },
    )
    for vehicle_id, provenance, chain in sorted(chains, key=lambda item: item[0]):
        chain = sorted(chain, key=lambda item: (item.scheduled_start_s, item.trip_id))
        activation = chain[0].scheduled_start_s
        current_location = chain[0].first_location_id
        clock = activation
        ordered_tasks: list[Task] = []
        for trip_index, trip in enumerate(chain):
            if trip_index:
                previous = chain[trip_index - 1]
                route = resolver.routing.route(
                    config.routing_profile_id,
                    resolved[current_location].node_id,
                    resolved[trip.first_location_id].node_id,
                )
                if not route.reachable:
                    raise GTFSImportError("duty contains an unreachable deadhead")
                ready = clock + config.turnaround_duration_s + route.total_duration_s
                if ready > trip.scheduled_start_s:
                    raise GTFSImportError(
                        f"impossible {provenance} chain before trip {trip.trip_id!r}"
                    )
                reposition_steps = [
                    TaskStep(
                        step_index=1,
                        location_id=current_location,
                        service_duration_s=config.turnaround_duration_s,
                    )
                ]
                if current_location != trip.first_location_id:
                    reposition_steps.append(
                        TaskStep(step_index=2, location_id=trip.first_location_id)
                    )
                reposition = Task(
                    task_id=stable_id(
                        "gtfs_reposition_task",
                        {
                            "date": config.service_date,
                            "vehicle_id": vehicle_id,
                            "from_trip": previous.trip_id,
                            "to_trip": trip.trip_id,
                        },
                    ),
                    fleet_id=config.fleet_id,
                    release_s=activation,
                    steps=tuple(reposition_steps),
                    kind="reposition",
                    source_record_refs=(f"trip:{previous.trip_id}", f"trip:{trip.trip_id}"),
                    source_policy="gtfs_prescribed_deadhead@1",
                )
                timing = estimate_task_timing(
                    reposition,
                    start_location_id=current_location,
                    start_s=clock,
                    locations=resolved,
                    routing=resolver.routing,
                    profile_id=config.routing_profile_id,
                )
                clock = timing.end_s
                current_location = timing.end_location_id
                ordered_tasks.append(reposition)
                deadheads.append(
                    {
                        "vehicle_id": vehicle_id,
                        "from_trip_id": previous.trip_id,
                        "to_trip_id": trip.trip_id,
                        "distance_m": route.total_distance_m,
                        "travel_time_s": route.total_duration_s,
                        "turnaround_s": config.turnaround_duration_s,
                        "idle_slack_s": trip.scheduled_start_s - ready,
                    }
                )
            duty_task = task_with_release(trip.task, activation)
            timing = estimate_task_timing(
                duty_task,
                start_location_id=current_location,
                start_s=clock,
                locations=resolved,
                routing=resolver.routing,
                profile_id=config.routing_profile_id,
            )
            clock = timing.end_s
            current_location = timing.end_location_id
            ordered_tasks.append(duty_task)
            if trip.trip_id in owned_trips:
                raise AssertionError("accepted trip assigned more than once")
            owned_trips.add(trip.trip_id)
        if clock <= activation:
            raise GTFSImportError("duty availability must have positive duration")
        key = VehicleKey(fleet_id=config.fleet_id, vehicle_id=vehicle_id)
        vehicle = VehicleSpec(
            key=key,
            availability_start_s=activation,
            availability_end_s=clock,
            initial_location_id=chain[0].first_location_id,
            identity_provenance=provenance,
        )
        vehicles.append(vehicle)
        availability.append(
            VehicleAvailability(
                replication_id=availability_scenario_id,
                vehicle=key,
                active=True,
                availability_start_s=activation,
                availability_end_s=clock,
                initial_location_id=chain[0].first_location_id,
            )
        )
        for order_index, task in enumerate(ordered_tasks):
            all_tasks.append(task)
            assignment_rows.append(
                AssignmentRow(vehicle=key, order_index=order_index, task_id=task.task_id)
            )
        duty_rows.append(
            {
                "vehicle_id": vehicle_id,
                "identity_provenance": provenance,
                "trip_ids_json": canonical_json_text([item.trip_id for item in chain]),
                "activation_s": activation,
                "nominal_end_s": clock,
            }
        )
    if owned_trips != accepted_ids:
        raise AssertionError("accepted trips and duty ownership are not bijective")
    assignments = AssignmentPlan(
        assignment_plan_id=stable_id(
            "assignment_plan",
            [row.model_dump(mode="json") for row in assignment_rows],
        ),
        rows=tuple(assignment_rows),
    )
    vehicles = sorted(vehicles, key=lambda item: (item.key.fleet_id, item.key.vehicle_id))
    availability = sorted(
        availability, key=lambda item: (item.vehicle.fleet_id, item.vehicle.vehicle_id)
    )
    catalog = _catalog(vehicles)
    diagnostics = {
        **preflight.counts,
        "accepted_trips": len(accepted_ids),
        "rejected_trips": len(selected_ids - accepted_ids),
        "duties": len(vehicles),
        "service_tasks": len(accepted_ids),
        "reposition_tasks": len(all_tasks) - len(accepted_ids),
        "crossing_trips": crossing_count,
        "route_failures": tuple(route_failures),
        "identity_assumption": (
            "user_supplied" if config.user_vehicle_assignments else "block_then_inferred_duty"
        ),
    }
    frames = _frames(
        preflight=preflight,
        tasks=all_tasks,
        locations=tuple(resolved.values()),
        vehicles=vehicles,
        availability=availability,
        assignments=assignments,
        catalog=catalog,
        trip_rows=trip_rows,
        stop_rows=stop_rows,
        duty_rows=duty_rows,
        deadheads=deadheads,
        diagnostics=diagnostics,
        route_failures=route_failures,
    )
    reference, directory = publish_dataset(
        artifact_root=Path(artifact_root),
        resolved_config={
            "gtfs": scientific_projection(config),
            "agency_timezone": preflight.agency_timezone,
            "service_origin_utc": preflight.service_origin_utc.isoformat(),
            "output_hash": scientific_hash(
                {
                    "tasks": all_tasks,
                    "locations": tuple(sorted(resolved.values(), key=lambda x: x.location_id)),
                    "vehicles": vehicles,
                    "availability": availability,
                    "assignments": assignments,
                    "diagnostics": diagnostics,
                }
            ),
        },
        dependencies=(
            ArtifactDependency(
                role="raw_gtfs", artifact_id=source.source_id, content_hash=source.content_hash
            ),
            ArtifactDependency(
                role="environment",
                artifact_id=resolver.environment.artifact_id,
                content_hash=resolver.environment.content_hash,
            ),
        ),
        algorithm_versions={
            "gtfs_reconstruction": GTFS_RECONSTRUCTION_VERSION,
            "task_timing": TIMING_ALGORITHM_VERSION,
        },
        frames=frames,
    )
    return GTFSReconstruction(
        reference=reference,
        directory=directory,
        tasks=tuple(all_tasks),
        locations=tuple(sorted(resolved.values(), key=lambda item: item.location_id)),
        vehicles=tuple(vehicles),
        availability=tuple(availability),
        assignments=assignments,
        catalog=catalog,
        diagnostics=diagnostics,
    )
