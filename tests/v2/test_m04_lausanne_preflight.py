import json
from pathlib import Path

import pytest

from mobile_sensing.datasets import (
    GTFSReconstructionConfig,
    discover_gtfs_directory,
    preflight_gtfs,
)


def test_bounded_real_lausanne_feed_preflight():
    directory = Path("data/Lausanne/gtfs")
    if not directory.is_dir():
        pytest.skip("bundled Lausanne GTFS directory is unavailable")
    source = discover_gtfs_directory(directory)
    config = GTFSReconstructionConfig.model_validate(
        {
            "schema_version": "2.0",
            "fleet_id": "lausanne_transit",
            "service_date": "2026-01-14",
            "route_ids": ("92-13-H-j26-1",),
            "routing_profile_id": "road_static_30kph",
            "turnaround_duration_s": 300.0,
            "max_selected_trips": 1_000,
        }
    )
    result = preflight_gtfs(source, config)
    assert result.agency_timezone == "Europe/Berlin"
    assert result.service_origin_utc.isoformat() == "2026-01-13T23:00:00+00:00"
    assert result.counts == {
        "source_routes": 46,
        "source_trips": 31_546,
        "active_services": 7_055,
        "active_trips_all_routes": 6_583,
        "selected_route_trips_all_services": 400,
        "selected_routes": 1,
        "selected_trips": 146,
        "selected_trip_counts_by_route": {"92-13-H-j26-1": 146},
    }
    evidence = json.loads(
        Path("tests/v2/fixtures/release_baselines/m04_lausanne_preflight.json").read_text()
    )
    assert evidence["service_date"] == result.service_date
    assert evidence["final_route_ids"] == list(result.route_ids)
    assert evidence["agency_timezone"] == result.agency_timezone
    assert evidence["service_origin_utc"] == result.service_origin_utc.isoformat()
    assert evidence["gtfs_source_content_hash"] == source.content_hash
    assert evidence["counts"] == result.counts
    assert evidence["routed_reconstruction_executed"] is True
