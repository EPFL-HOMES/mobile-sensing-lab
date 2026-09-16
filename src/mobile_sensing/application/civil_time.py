"""Civil-day authoring with explicit ambiguous/nonexistent local-time rejection."""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
import re
from mobile_sensing.contracts import ClockConfig


def local_instant(service_date: date, text: str, zone: str) -> datetime:
    match = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", text)
    if not match:
        raise ValueError(f"Invalid clock time {text!r}; use HH:MM or HH:MM:SS")
    hour, minute, second = int(match[1]), int(match[2]), int(match[3] or 0)
    if hour > 24 or minute >= 60 or second >= 60 or (hour == 24 and (minute or second)):
        raise ValueError("Clock times must lie between 00:00 and 24:00")
    naive = datetime.combine(service_date, time()) + timedelta(
        hours=hour, minutes=minute, seconds=second
    )
    tz = ZoneInfo(zone)
    first, second_fold = naive.replace(tzinfo=tz, fold=0), naive.replace(tzinfo=tz, fold=1)
    if first.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None) != naive:
        raise ValueError(
            f"Nonexistent local time {text} on {service_date}; use an explicit-offset datetime input"
        )
    if first.utcoffset() != second_fold.utcoffset():
        raise ValueError(
            f"Ambiguous local time {text} on {service_date}; use an explicit-offset datetime input"
        )
    return first.astimezone(timezone.utc)


def civil_clock(config, zone):
    zone = "UTC" if config.time_mode == "relative" else config.timezone or zone
    day = date(2001, 1, 1) if config.time_mode == "relative" else config.service_date
    try:
        ZoneInfo(zone)
    except (KeyError, ValueError) as exc:
        raise ValueError(f"Unknown timezone {zone!r}; select an IANA timezone") from exc
    origin = local_instant(day, "00:00", zone)
    start = (local_instant(day, config.start_time, zone) - origin).total_seconds()
    end = (local_instant(day, config.end_time, zone) - origin).total_seconds()
    return ClockConfig(
        origin_utc=origin,
        display_timezone=zone,
        simulation_start_s=start - config.warmup_hours * 3600,
        observation_start_s=start,
        end_s=end,
    )


def clock_seconds(text, clock):
    day = clock.origin_utc.astimezone(ZoneInfo(clock.display_timezone)).date()
    return (local_instant(day, text, clock.display_timezone) - clock.origin_utc).total_seconds()


def input_seconds(value, unit, clock):
    if unit == "clock":
        return clock_seconds(str(value), clock)
    if unit == "datetime":
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("Datetime inputs require an explicit UTC offset")
        return (parsed.astimezone(timezone.utc) - clock.origin_utc).total_seconds()
    return float(value) * {"seconds": 1, "minutes": 60, "hours": 3600}[unit]
