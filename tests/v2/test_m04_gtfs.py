from __future__ import annotations

import random
import zipfile
from pathlib import Path

import pandas as pd
import pytest
from shapely.geometry import box

from mobile_sensing.contracts import (
    EnvironmentArtifactRef,
    LocationRef,
    ResolutionStatus,
    RouteEdge,
    RouteResult,
    scientific_hash,
)
from mobile_sensing.datasets import (
    GTFSImportError,
    GTFSReconstructionConfig,
    discover_gtfs_directory,
    gtfs_service_origin,
    gtfs_time_to_utc,
    parse_gtfs_time,
    preflight_gtfs,
    reconstruct_gtfs,
    register_gtfs_source,
)
from mobile_sensing.datasets.parsing import LocationResolver
from mobile_sensing.datasets.storage import verified_dataset_directory


HASH = "0" * 64
PROFILE_HASH = "1" * 64


class FakeSnapper:
    def resolve(self, *, location_id, x, y, source_crs):
        value = float(x)
        return LocationRef(
            location_id=location_id,
            original_x=value,
            original_y=float(y),
            original_crs=source_crs,
            node_id=f"N{int(value)}",
            snapped_x=value * 10.0,
            snapped_y=0.0,
            snap_distance_m=0.0,
            resolution_status=ResolutionStatus.RESOLVED,
        )


class FakeRouting:
    def route(self, profile_id, source_node_id, target_node_id):
        assert profile_id == "bus"
        if source_node_id == target_node_id:
            edges = ()
        else:
            distance = abs(int(source_node_id[1:]) - int(target_node_id[1:])) * 10.0
            edges = (
                RouteEdge(
                    edge_id=f"{source_node_id}>{target_node_id}",
                    length_m=distance,
                    duration_s=distance,
                ),
            )
        return RouteResult(
            reachable=True,
            edges=edges,
            total_duration_s=sum(item.duration_s for item in edges),
            total_distance_m=sum(item.length_m for item in edges),
            network_hash=HASH,
            profile_hash=PROFILE_HASH,
        )


def _resolver():
    return LocationResolver(
        environment=EnvironmentArtifactRef(
            artifact_id="analytic_environment", artifact_kind="environment", content_hash=HASH
        ),
        snapper=FakeSnapper(),
        routing=FakeRouting(),
        routing_profile_id="bus",
    )


def _write(path: Path, name: str, rows: list[dict[str, str]]) -> None:
    pd.DataFrame(rows).to_csv(path / name, index=False)


def _feed(path: Path, *, shuffled: bool = False, block: bool = False) -> Path:
    path.mkdir()
    _write(path, "agency.csv", [{"agency_id": "a", "agency_timezone": "Europe/Zurich"}])
    _write(
        path,
        "buses.csv",
        [{"route_id": "001", "route_short_name": "1", "continuous_pickup": "1"}],
    )
    _write(
        path,
        "calendar.csv",
        [
            {
                "service_id": "weekday",
                "monday": "1",
                "tuesday": "1",
                "wednesday": "1",
                "thursday": "1",
                "friday": "1",
                "saturday": "0",
                "sunday": "0",
                "start_date": "20260101",
                "end_date": "20261231",
            },
            {
                "service_id": "removed",
                "monday": "1",
                "tuesday": "1",
                "wednesday": "1",
                "thursday": "1",
                "friday": "1",
                "saturday": "0",
                "sunday": "0",
                "start_date": "20260101",
                "end_date": "20261231",
            },
        ],
    )
    exceptions = [
        {"service_id": "removed", "date": "20260114", "exception_type": "2"},
        {"service_id": "added", "date": "20260114", "exception_type": "1"},
    ]
    trips = [
        {
            "route_id": "001",
            "service_id": "weekday",
            "trip_id": "0001",
            "block_id": "b" if block else "",
        },
        {
            "route_id": "001",
            "service_id": "weekday",
            "trip_id": "0002",
            "block_id": "b" if block else "",
        },
        {
            "route_id": "001",
            "service_id": "weekday",
            "trip_id": "0003",
            "block_id": "b" if block else "",
        },
        {"route_id": "001", "service_id": "added", "trip_id": "0025", "block_id": ""},
        {"route_id": "001", "service_id": "removed", "trip_id": "gone", "block_id": ""},
    ]
    stops = [
        {"stop_id": "A", "stop_name": "A", "stop_lat": "0", "stop_lon": "0"},
        {"stop_id": "B", "stop_name": "B", "stop_lat": "0", "stop_lon": "1"},
        {"stop_id": "C", "stop_name": "C", "stop_lat": "0", "stop_lon": "2"},
    ]
    stop_times = [
        {
            "trip_id": "0001",
            "arrival_time": "08:00:00",
            "departure_time": "08:00:00",
            "stop_id": "A",
            "stop_sequence": "10",
        },
        {
            "trip_id": "0001",
            "arrival_time": "08:00:00",
            "departure_time": "08:01:00",
            "stop_id": "B",
            "stop_sequence": "20",
        },
        {
            "trip_id": "0001",
            "arrival_time": "08:05:00",
            "departure_time": "08:06:00",
            "stop_id": "A",
            "stop_sequence": "30",
        },
        {
            "trip_id": "0002",
            "arrival_time": "08:20:00",
            "departure_time": "08:21:00",
            "stop_id": "C",
            "stop_sequence": "1",
        },
        {
            "trip_id": "0002",
            "arrival_time": "08:25:00",
            "departure_time": "08:26:00",
            "stop_id": "B",
            "stop_sequence": "2",
        },
        {
            "trip_id": "0003",
            "arrival_time": "08:03:00",
            "departure_time": "08:04:00",
            "stop_id": "A",
            "stop_sequence": "1",
        },
        {
            "trip_id": "0003",
            "arrival_time": "08:08:00",
            "departure_time": "08:09:00",
            "stop_id": "B",
            "stop_sequence": "2",
        },
        {
            "trip_id": "0025",
            "arrival_time": "25:00:00",
            "departure_time": "25:01:00",
            "stop_id": "A",
            "stop_sequence": "1",
        },
        {
            "trip_id": "0025",
            "arrival_time": "25:05:00",
            "departure_time": "25:06:00",
            "stop_id": "B",
            "stop_sequence": "2",
        },
        {
            "trip_id": "gone",
            "arrival_time": "09:00:00",
            "departure_time": "09:01:00",
            "stop_id": "A",
            "stop_sequence": "1",
        },
    ]
    if shuffled:
        random.Random(90210).shuffle(trips)
        random.Random(90211).shuffle(stops)
        random.Random(90212).shuffle(stop_times)
        random.Random(90213).shuffle(exceptions)
    _write(path, "calendar_dates.csv", exceptions)
    _write(path, "trips.csv", trips)
    _write(path, "stops.csv", stops)
    _write(path, "stop_times.csv", stop_times)
    return path


def _config(**updates):
    values = {
        "schema_version": "2.0",
        "fleet_id": "transit",
        "service_date": "2026-01-14",
        "route_ids": ("001",),
        "routing_profile_id": "bus",
        "turnaround_duration_s": 60.0,
    }
    values.update(updates)
    return GTFSReconstructionConfig.model_validate(values)


def test_calendar_time_dst_and_alias_registration(tmp_path):
    source_path = _feed(tmp_path / "feed")
    source = register_gtfs_source(source_path, artifact_root=tmp_path / "artifacts")
    preflight = preflight_gtfs(source, _config())
    assert preflight.route_ids == ("001",)
    assert preflight.selected_trip_ids == ("0001", "0002", "0003", "0025")
    assert "added" in preflight.active_service_ids and "removed" not in preflight.active_service_ids
    assert parse_gtfs_time("25:01:02") == 90_062
    assert (
        gtfs_service_origin("2026-03-29", "Europe/Zurich").isoformat()
        == "2026-03-28T22:00:00+00:00"
    )
    assert (
        gtfs_time_to_utc("2026-03-29", "Europe/Zurich", "25:00:00").isoformat()
        == "2026-03-29T23:00:00+00:00"
    )
    (source_path / "routes.txt").write_text((source_path / "buses.csv").read_text())
    with pytest.raises(GTFSImportError, match="ambiguous aliases"):
        discover_gtfs_directory(source_path)


def test_complete_chains_duties_deadheads_artifact_and_shuffle_stability(tmp_path):
    outputs = []
    for name, shuffled in (("ordered", False), ("shuffled", True)):
        source = discover_gtfs_directory(_feed(tmp_path / name, shuffled=shuffled))
        result = reconstruct_gtfs(
            source,
            _config(),
            artifact_root=tmp_path / f"artifacts-{name}",
            resolver=_resolver(),
            sensing_boundary=box(-1.0, -1.0, 5.0, 1.0),
        )
        assert result.diagnostics["selected_trips"] == 4
        assert result.diagnostics["accepted_trips"] == 4
        assert result.diagnostics["service_tasks"] == 4
        assert result.diagnostics["crossing_trips"] >= 1
        assert result.diagnostics["reposition_tasks"] >= 1
        assert result.diagnostics["selected_trip_counts_by_route"] == {"001": 4}
        assert len([task for task in result.tasks if task.kind == "service"]) == 4
        trip_one = next(task for task in result.tasks if task.source_record_refs == ("trip:0001",))
        assert len(trip_one.steps) == 3
        assert trip_one.steps[0].location_id == trip_one.steps[2].location_id
        assert trip_one.steps[0].scheduled_time_s == trip_one.steps[1].scheduled_time_s
        diagnostic = pd.read_parquet(result.directory / "trip_diagnostics.parquet")
        assert diagnostic.loc[diagnostic.trip_id == "0001", "maximum_lateness_s"].iloc[0] > 0
        assert all(vehicle.key.vehicle_id != "001" for vehicle in result.vehicles)
        directory, manifest = verified_dataset_directory(
            result.reference, artifact_root=tmp_path / f"artifacts-{name}"
        )
        assert (directory / "_SUCCESS").is_file()
        assert set(manifest.expected_table_names) == {
            "assignments",
            "catalog_identity",
            "deadheads",
            "diagnostics",
            "duties",
            "gtfs_metadata",
            "locations",
            "route_failures",
            "task_steps",
            "tasks",
            "trip_diagnostics",
            "trip_stops",
            "vehicle_availability",
            "vehicle_catalog",
        }
        for vehicle in result.vehicles:
            owned = [row for row in result.assignments.rows if row.vehicle == vehicle.key]
            assert all(
                next(task for task in result.tasks if task.task_id == row.task_id).release_s
                == vehicle.availability_start_s
                for row in owned
            )
        outputs.append(
            scientific_hash(
                {
                    "tasks": result.tasks,
                    "locations": result.locations,
                    "vehicles": result.vehicles,
                    "availability": result.availability,
                    "assignments": result.assignments,
                    "diagnostics": result.diagnostics,
                }
            )
        )
    assert outputs[0] == outputs[1]


def test_user_block_identity_and_impossible_chain(tmp_path):
    source = discover_gtfs_directory(_feed(tmp_path / "feed"))
    assigned = reconstruct_gtfs(
        source,
        _config(
            user_vehicle_assignments={
                "0001": "physical-7",
                "0002": "physical-8",
                "0003": "physical-9",
                "0025": "physical-10",
            }
        ),
        artifact_root=tmp_path / "assigned",
        resolver=_resolver(),
    )
    assert {item.key.vehicle_id for item in assigned.vehicles} == {
        "physical-7",
        "physical-8",
        "physical-9",
        "physical-10",
    }
    assert {item.identity_provenance for item in assigned.vehicles} == {"uploaded"}

    blocked = discover_gtfs_directory(_feed(tmp_path / "blocked", block=True))
    with pytest.raises(GTFSImportError, match="impossible gtfs_block chain"):
        reconstruct_gtfs(
            blocked,
            _config(),
            artifact_root=tmp_path / "blocked-out",
            resolver=_resolver(),
        )

    trips = pd.read_csv(blocked.tables["trips"], dtype=str, keep_default_na=False)
    trips.loc[trips.trip_id == "0003", "block_id"] = ""
    trips.to_csv(blocked.tables["trips"], index=False)
    feasible_block = reconstruct_gtfs(
        discover_gtfs_directory(blocked.directory),
        _config(),
        artifact_root=tmp_path / "feasible-block",
        resolver=_resolver(),
    )
    assert "gtfs_block" in {item.identity_provenance for item in feasible_block.vehicles}


def test_unsupported_selected_features_and_missing_time_policy(tmp_path):
    feed = _feed(tmp_path / "feed")
    _write(
        feed,
        "frequencies.csv",
        [
            {
                "trip_id": "0001",
                "start_time": "08:00:00",
                "end_time": "09:00:00",
                "headway_secs": "600",
            }
        ],
    )
    with pytest.raises(GTFSImportError, match="frequencies"):
        preflight_gtfs(discover_gtfs_directory(feed), _config())
    (feed / "frequencies.csv").unlink()
    stop_times = pd.read_csv(feed / "stop_times.csv", dtype=str, keep_default_na=False)
    stop_times.loc[
        (stop_times.trip_id == "0001") & (stop_times.stop_sequence == "20"), "arrival_time"
    ] = ""
    stop_times.to_csv(feed / "stop_times.csv", index=False)
    source = discover_gtfs_directory(feed)
    with pytest.raises(GTFSImportError, match="missing required stop timing"):
        reconstruct_gtfs(source, _config(), artifact_root=tmp_path / "strict", resolver=_resolver())
    accepted = reconstruct_gtfs(
        source,
        _config(allow_instantaneous_missing_time_copy=True),
        artifact_root=tmp_path / "copy",
        resolver=_resolver(),
    )
    assert accepted.diagnostics["accepted_trips"] == 4


def test_continuous_and_linked_trip_features_are_rejected(tmp_path):
    feed = _feed(tmp_path / "feed")
    routes = pd.read_csv(feed / "buses.csv", dtype=str, keep_default_na=False)
    routes["continuous_pickup"] = "0"
    routes.to_csv(feed / "buses.csv", index=False)
    with pytest.raises(GTFSImportError, match="continuous_pickup"):
        preflight_gtfs(discover_gtfs_directory(feed), _config())
    routes["continuous_pickup"] = "1"
    routes.to_csv(feed / "buses.csv", index=False)
    _write(
        feed,
        "transfers.csv",
        [{"from_stop_id": "A", "to_stop_id": "B", "from_trip_id": "0001"}],
    )
    with pytest.raises(GTFSImportError, match="linked-trip transfers"):
        preflight_gtfs(discover_gtfs_directory(feed), _config())


def test_zip_limits_and_civil_day_scope(tmp_path):
    feed = _feed(tmp_path / "feed")
    archive = tmp_path / "feed.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        for path in feed.iterdir():
            bundle.write(path, path.name)
    registered = register_gtfs_source(archive, artifact_root=tmp_path / "artifacts")
    assert preflight_gtfs(registered, _config()).counts["selected_trips"] == 4
    with pytest.raises(ValueError):
        _config(civil_day_complete_requested=True)
    with pytest.raises(GTFSImportError, match="exceeds configured extraction limits"):
        register_gtfs_source(archive, artifact_root=tmp_path / "too-small", max_files=1)


def test_discovered_source_is_revalidated_before_use(tmp_path):
    feed = _feed(tmp_path / "feed")
    source = discover_gtfs_directory(feed)
    agency = feed / "agency.csv"
    agency.write_text(agency.read_text() + "\n", encoding="utf-8")
    with pytest.raises(GTFSImportError, match="changed after discovery"):
        preflight_gtfs(source, _config())
