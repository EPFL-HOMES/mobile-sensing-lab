"""Shared strict scalar, time, identifier, and location parsing."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd
from pyproj import CRS

from mobile_sensing.contracts import (
    EnvironmentArtifactRef,
    IssueSeverity,
    LocationRef,
    ResolutionStatus,
    RoutingService,
    ValidationIssue,
)
from mobile_sensing.datasets.models import (
    ElapsedTimeMapping,
    ServiceClockTimeMapping,
    TimeMapping,
    TimestampTimeMapping,
)
from mobile_sensing.environment.snapping import NodeSnapper


_TIME_SCALE = {"seconds": 1.0, "minutes": 60.0, "hours": 3600.0}


def issue(
    code: str,
    message: str,
    *,
    dataset_id: str,
    source_row: int | None,
    field: str,
    task_id: str | None = None,
) -> ValidationIssue:
    return ValidationIssue(
        severity=IssueSeverity.ERROR,
        code=code,
        field_path=field,
        message=message,
        corrective_action="Correct the source value or saved mapping and normalize again.",
        dataset_id=dataset_id,
        table="source",
        source_row=source_row,
        task_id=task_id,
    )


def parse_identifier(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("identifier values must use a string column type")
    if value == "":
        raise ValueError("identifier must be nonempty")
    value.encode("utf-8")
    return value


def parse_float(value: Any, *, nonnegative: bool = False) -> float:
    if isinstance(value, bool):
        raise ValueError("boolean is not a numeric value")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("value must be numeric") from exc
    if not math.isfinite(result):
        raise ValueError("value must be finite")
    if nonnegative and result < 0:
        raise ValueError("value must be nonnegative")
    return result


def parse_time(value: Any, mapping: TimeMapping) -> float:
    if isinstance(mapping, ElapsedTimeMapping):
        return parse_float(value) * _TIME_SCALE[mapping.unit]
    if isinstance(mapping, TimestampTimeMapping):
        try:
            timestamp = pd.Timestamp(value)
        except Exception as exc:
            raise ValueError("timestamp is not parseable") from exc
        if timestamp.tzinfo is None:
            raise ValueError("timestamp must include an explicit UTC offset")
        return float(
            (
                timestamp.to_pydatetime().astimezone(timezone.utc) - mapping.origin_utc
            ).total_seconds()
        )
    assert isinstance(mapping, ServiceClockTimeMapping)
    try:
        zone = ZoneInfo(mapping.timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("service-clock timezone must be an IANA timezone") from exc
    if not isinstance(value, str):
        raise ValueError("service clock must be a string")
    parts = value.split(":")
    if len(parts) != 3:
        raise ValueError("service clock must use HH:MM:SS")
    try:
        hours, minutes, seconds = (int(item) for item in parts)
    except ValueError as exc:
        raise ValueError("service clock must use integer HH:MM:SS fields") from exc
    if hours < 0 or not 0 <= minutes < 60 or not 0 <= seconds < 60:
        raise ValueError("service clock has invalid hour, minute, or second")
    naive_midnight = datetime.strptime(mapping.service_date, "%Y-%m-%d")
    naive_instant = naive_midnight + timedelta(hours=hours, minutes=minutes, seconds=seconds)
    candidates = tuple(naive_instant.replace(tzinfo=zone, fold=fold) for fold in (0, 1))
    valid = tuple(
        candidate
        for candidate in candidates
        if candidate.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) == naive_instant
    )
    if not valid:
        raise ValueError("service clock falls in a nonexistent local-time interval")
    if len(valid) == 2 and valid[0].utcoffset() != valid[1].utcoffset():
        raise ValueError("service clock is ambiguous at a daylight-saving transition")
    instant = valid[0]
    return float((instant.astimezone(timezone.utc) - mapping.origin_utc).total_seconds())


def parse_duration(value: Any, unit: str) -> float:
    return parse_float(value, nonnegative=True) * _TIME_SCALE[unit]


class LocationResolver:
    """Resolve explicit IDs or coordinates without inferring semantic locations."""

    def __init__(
        self,
        *,
        environment: EnvironmentArtifactRef,
        known_locations: dict[str, LocationRef] | None = None,
        snapper: NodeSnapper | None = None,
        routing: RoutingService | None = None,
        routing_profile_id: str | None = None,
    ) -> None:
        self.environment = EnvironmentArtifactRef.model_validate(environment)
        self.known_locations = dict(known_locations or {})
        self.snapper = snapper
        if (routing is None) != (routing_profile_id is None):
            raise ValueError("routing and routing_profile_id must be supplied together")
        self.routing = routing
        self.routing_profile_id = routing_profile_id

    def by_id(self, location_id: str) -> LocationRef:
        try:
            location = self.known_locations[location_id]
        except KeyError as exc:
            raise ValueError(f"unknown location_id {location_id!r}") from exc
        if location.resolution_status != ResolutionStatus.RESOLVED:
            raise ValueError(f"location_id {location_id!r} is not resolved")
        return location

    def by_coordinates(self, *, location_id: str, x: Any, y: Any, source_crs: str) -> LocationRef:
        if self.snapper is None:
            raise ValueError("coordinate mappings require a prepared-environment node snapper")
        parsed_x = parse_float(x)
        parsed_y = parse_float(y)
        try:
            crs = CRS.from_user_input(source_crs)
        except Exception as exc:
            raise ValueError("source_crs is not recognized") from exc
        if crs.is_geographic and not (-180 <= parsed_x <= 180 and -90 <= parsed_y <= 90):
            if -180 <= parsed_y <= 180 and -90 <= parsed_x <= 90:
                raise ValueError("geographic coordinates appear to have longitude/latitude swapped")
            raise ValueError("geographic coordinates are outside longitude/latitude bounds")
        location = self.snapper.resolve(
            location_id=location_id,
            x=parsed_x,
            y=parsed_y,
            source_crs=source_crs,
        )
        if location.resolution_status == ResolutionStatus.REJECTED_DISTANCE:
            raise ValueError("location exceeds the configured node snap tolerance")
        if location.resolution_status == ResolutionStatus.REJECTED_INVALID:
            raise ValueError("coordinate transformation produced an invalid location")
        return location

    def require_route(self, source: LocationRef, target: LocationRef) -> None:
        if source.node_id == target.node_id:
            return
        if self.routing is None or self.routing_profile_id is None:
            raise ValueError("multi-step tasks require an identified routing profile")
        assert source.node_id is not None and target.node_id is not None
        route = self.routing.route(self.routing_profile_id, source.node_id, target.node_id)
        if not route.reachable:
            raise ValueError(
                f"internal task leg is unreachable ({source.location_id!r} to "
                f"{target.location_id!r}): {route.reason}"
            )
