from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
import geopandas as gpd
from pydantic import ValidationError
from shapely.geometry import Point

from mobile_sensing.contracts import EnvironmentArtifactRef, LocationRef, ResolutionStatus
from mobile_sensing.datasets import (
    AreaAssignmentMapping,
    DemandImportError,
    DemandImportMapping,
    RateImportMapping,
    SupplyImportError,
    VehicleImportMapping,
    normalize_demand,
    normalize_supply,
    read_saved_demand_mapping,
    read_saved_supply_mappings,
    register_tabular_source,
)
from mobile_sensing.datasets.models import ServiceClockTimeMapping
from mobile_sensing.datasets.parsing import LocationResolver, parse_time
from mobile_sensing.environment.snapping import NodeSnapper


def _location(location_id: str, x: float) -> LocationRef:
    return LocationRef(
        location_id=location_id,
        original_x=x,
        original_y=0.0,
        original_crs="EPSG:2056",
        node_id=f"node_{location_id}",
        snapped_x=x,
        snapped_y=0.0,
        snap_distance_m=0.0,
        resolution_status=ResolutionStatus.RESOLVED,
    )


ENVIRONMENT = EnvironmentArtifactRef(
    artifact_id="environment_test",
    artifact_kind="environment",
    content_hash="a" * 64,
)


class _ReachableRouting:
    def route(self, profile_id: str, source_node_id: str, target_node_id: str):
        assert profile_id == "profile"
        return SimpleNamespace(reachable=True, reason=None)


def _resolver(known_locations: dict[str, LocationRef]) -> LocationResolver:
    return LocationResolver(
        environment=ENVIRONMENT,
        known_locations=known_locations,
        routing=_ReachableRouting(),
        routing_profile_id="profile",
    )


@pytest.fixture
def known_locations() -> dict[str, LocationRef]:
    return {key: _location(key, float(index)) for index, key in enumerate(("L0", "L1", "L2"))}


def _ordered_mapping(error_policy: str = "quarantine_invalid_tasks") -> DemandImportMapping:
    return DemandImportMapping.model_validate(
        {
            "schema_version": "2.0",
            "adapter": "demand.upload_ordered@1",
            "structure": "ordered",
            "data_semantics": "observed_tasks",
            "fleet_id": "fleet_01",
            "columns": {
                "task_id": "task",
                "release_time": "release_s",
                "step_index": "sequence",
                "location_id": "location",
                "service_duration": "service_s",
                "quantity_delta": "delta",
            },
            "location_representation": "ids",
            "source_crs": None,
            "time": {"kind": "elapsed", "unit": "seconds"},
            "duration_unit": "seconds",
            "service_duration_default_s": None,
            "quantity_mode": "signed_delta",
            "quantity_unit": "passengers",
            "allow_generated_task_ids": False,
            "error_policy": error_policy,
        }
    )


def test_ordered_import_is_chunk_invariant_replayable_and_quarantines_whole_task(
    tmp_path: Path, known_locations: dict[str, LocationRef]
) -> None:
    frame = pd.DataFrame(
        {
            "task": ["001", "bad", "001", "bad"],
            "release_s": [0, 5, 0, 5],
            "sequence": [1, 1, 2, 3],
            "location": ["L0", "L1", "L2", "L2"],
            "service_s": [10, 0, 20, 0],
            "delta": [-2, 0, 2, 0],
        }
    )
    source_path = tmp_path / "ordered.csv"
    frame.to_csv(source_path, index=False)
    source = register_tabular_source(
        source_path, artifact_root=tmp_path / "artifacts", provenance="M03 diagnostic fixture"
    )
    resolver = _resolver(known_locations)
    first = normalize_demand(
        source,
        _ordered_mapping(),
        artifact_root=tmp_path / "artifacts",
        resolver=resolver,
        chunk_size=1,
    )
    second = normalize_demand(
        source,
        _ordered_mapping(),
        artifact_root=tmp_path / "artifacts",
        resolver=resolver,
        chunk_size=4,
    )

    assert first.reference == second.reference
    assert first.tasks == second.tasks
    assert [task.task_id for task in first.tasks] == ["001"]
    assert first.tasks[0].required_capacity == 2
    assert {issue.task_id for issue in first.issues} == {"bad"}
    assert {issue.code for issue in first.issues} == {"INVALID_STEP_SEQUENCE"}
    assert first.issues[0].source_row == 2
    mapping_table = pd.read_parquet(first.directory / "import_mapping.parquet")
    assert mapping_table.loc[0, "mapping_id"] == first.mapping_id
    assert json.loads(mapping_table.loc[0, "mapping_json"])["columns"]["task_id"] == "task"
    assert (
        read_saved_demand_mapping(first.reference, artifact_root=tmp_path / "artifacts")
        == _ordered_mapping()
    )
    manifest = json.loads((first.directory / "manifest.json").read_text())
    assert tuple(manifest["expected_table_names"]) == (
        "import_mapping",
        "locations",
        "od_rates",
        "task_steps",
        "tasks",
        "validation_issues",
    )
    assert {item["role"] for item in manifest["dependencies"]} == {
        "environment",
        "raw_demand",
    }

    with pytest.raises(DemandImportError) as caught:
        normalize_demand(
            source,
            _ordered_mapping("strict"),
            artifact_root=tmp_path / "strict-artifacts",
            resolver=resolver,
            chunk_size=2,
        )
    assert {item.task_id for item in caught.value.issues} == {"bad"}
    assert not (tmp_path / "strict-artifacts" / "datasets").exists()


def test_parquet_ids_rate_semantics_and_global_rate_validation(
    tmp_path: Path, known_locations: dict[str, LocationRef]
) -> None:
    rate_path = tmp_path / "rates.parquet"
    pd.DataFrame(
        {
            "begin": ["0", "3600"],
            "finish": ["3600", "7200"],
            "origin": ["L0", "L0"],
            "destination": ["L1", "L1"],
            "hourly_rate": ["2", "3"],
        }
    ).to_parquet(rate_path, index=False)
    source = register_tabular_source(
        rate_path, artifact_root=tmp_path / "artifacts", provenance="rate fixture"
    )
    mapping = RateImportMapping.model_validate(
        {
            "schema_version": "2.0",
            "adapter": "demand.upload_sparse_od_rate@1",
            "data_semantics": "rate_model",
            "fleet_id": "fleet_01",
            "columns": {
                "interval_start": "begin",
                "interval_end": "finish",
                "origin_location_id": "origin",
                "destination_location_id": "destination",
                "rate": "hourly_rate",
            },
            "time": {"kind": "elapsed", "unit": "seconds"},
            "rate_unit": "tasks_per_hour",
            "error_policy": "strict",
        }
    )
    result = normalize_demand(
        source,
        mapping,
        artifact_root=tmp_path / "artifacts",
        resolver=_resolver(known_locations),
        chunk_size=1,
    )
    assert result.data_semantics == "rate_model"
    assert result.tasks == ()
    assert result.rates[0]["rate_tasks_per_s"] == pytest.approx(2 / 3600)

    overlap_path = tmp_path / "overlap.csv"
    pd.DataFrame(
        {
            "begin": [0, 5],
            "finish": [10, 15],
            "origin": ["L0", "L0"],
            "destination": ["L1", "L1"],
            "hourly_rate": [1, 1],
        }
    ).to_csv(overlap_path, index=False)
    overlap = register_tabular_source(
        overlap_path, artifact_root=tmp_path / "artifacts", provenance="overlap fixture"
    )
    with pytest.raises(DemandImportError, match="failed") as caught:
        normalize_demand(
            overlap,
            mapping,
            artifact_root=tmp_path / "artifacts",
            resolver=_resolver(known_locations),
            chunk_size=1,
        )
    assert "OVERLAPPING_RATE_INTERVALS" in {item.code for item in caught.value.issues}


def test_mapping_defaults_identifier_types_and_time_failures_are_explicit(
    tmp_path: Path, known_locations: dict[str, LocationRef]
) -> None:
    base = {
        "schema_version": "2.0",
        "adapter": "demand.upload_location@1",
        "structure": "location",
        "data_semantics": "observed_tasks",
        "fleet_id": "fleet",
        "columns": {
            "task_id": "task",
            "release_time": "release",
            "location_id": "location",
        },
        "location_representation": "ids",
        "source_crs": None,
        "time": {"kind": "elapsed", "unit": "seconds"},
        "duration_unit": "seconds",
        "service_duration_default_s": None,
        "quantity_mode": "none",
        "quantity_unit": None,
        "allow_generated_task_ids": False,
        "error_policy": "strict",
    }
    with pytest.raises(ValidationError, match="explicit default"):
        DemandImportMapping.model_validate(base)
    coordinate = dict(base)
    coordinate["columns"] = {
        "task_id": "task",
        "release_time": "release",
        "x": "x",
        "y": "y",
    }
    coordinate["location_representation"] = "coordinates"
    coordinate["source_crs"] = "not-a-crs"
    coordinate["service_duration_default_s"] = 0.0
    with pytest.raises(ValidationError, match="source_crs is not recognized"):
        DemandImportMapping.model_validate(coordinate)

    invalid_clock = dict(base)
    invalid_clock["service_duration_default_s"] = 0.0
    invalid_clock["time"] = {
        "kind": "service_clock",
        "service_date": "2026-01-01",
        "timezone": "Invalid/Nowhere",
        "origin_utc": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }
    with pytest.raises(ValidationError, match="IANA timezone"):
        DemandImportMapping.model_validate(invalid_clock)
    ambiguous_clock = ServiceClockTimeMapping(
        kind="service_clock",
        service_date="2026-10-25",
        timezone="Europe/Zurich",
        origin_utc=datetime(2026, 10, 25, tzinfo=timezone.utc),
    )
    with pytest.raises(ValueError, match="ambiguous"):
        parse_time("02:30:00", ambiguous_clock)

    timestamp_mapping = dict(base)
    timestamp_mapping["service_duration_default_s"] = 0.0
    timestamp_mapping["time"] = {
        "kind": "timestamp",
        "origin_utc": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }
    mapping = DemandImportMapping.model_validate(timestamp_mapping)
    path = tmp_path / "naive.csv"
    pd.DataFrame({"task": ["001"], "release": ["2026-01-01 01:00:00"], "location": ["L0"]}).to_csv(
        path, index=False
    )
    source = register_tabular_source(
        path, artifact_root=tmp_path / "artifacts", provenance="time fixture"
    )
    with pytest.raises(DemandImportError) as caught:
        normalize_demand(
            source,
            mapping,
            artifact_root=tmp_path / "artifacts",
            resolver=_resolver(known_locations),
        )
    assert "explicit UTC offset" in caught.value.issues[0].message

    numeric_path = tmp_path / "numeric_ids.parquet"
    pd.DataFrame({"task": [1], "release": ["2026-01-01T00:00:00Z"], "location": ["L0"]}).to_parquet(
        numeric_path, index=False
    )
    numeric = register_tabular_source(
        numeric_path, artifact_root=tmp_path / "artifacts", provenance="numeric ID fixture"
    )
    with pytest.raises(DemandImportError) as numeric_error:
        normalize_demand(
            numeric,
            mapping,
            artifact_root=tmp_path / "artifacts",
            resolver=_resolver(known_locations),
        )
    assert "string column type" in numeric_error.value.issues[0].message


def test_supply_catalog_and_separate_assignments_validate_foreign_keys_across_chunks(
    tmp_path: Path, known_locations: dict[str, LocationRef]
) -> None:
    vehicles_path = tmp_path / "vehicles.csv"
    pd.DataFrame(
        {
            "vehicle": ["001", "002"],
            "start": [0, 10],
            "end": [100, 110],
            "initial": ["L0", "L1"],
            "capacity": [4, 5],
        }
    ).to_csv(vehicles_path, index=False)
    assignments_path = tmp_path / "areas.csv"
    pd.DataFrame({"vehicle": ["002", "001"], "area": ["A", "B"]}).to_csv(
        assignments_path, index=False
    )
    vehicle_source = register_tabular_source(
        vehicles_path, artifact_root=tmp_path / "artifacts", provenance="vehicle fixture"
    )
    assignment_source = register_tabular_source(
        assignments_path, artifact_root=tmp_path / "artifacts", provenance="assignment fixture"
    )
    vehicle_mapping = VehicleImportMapping.model_validate(
        {
            "schema_version": "2.0",
            "adapter": "supply.upload_vehicle_catalog@1",
            "fleet_id": "fleet_01",
            "columns": {
                "vehicle_id": "vehicle",
                "availability_start": "start",
                "availability_end": "end",
                "initial_location_id": "initial",
                "capacity": "capacity",
            },
            "location_representation": "ids",
            "source_crs": None,
            "time": {"kind": "elapsed", "unit": "seconds"},
            "capacity_mode": "occupancy",
            "quantity_unit": "passengers",
        }
    )
    assignment_mapping = AreaAssignmentMapping.model_validate(
        {
            "schema_version": "2.0",
            "adapter": "supply.upload_area_assignments@1",
            "columns": {"vehicle_id": "vehicle", "area_id": "area"},
            "fixed_fleet_id": "fleet_01",
        }
    )
    result = normalize_supply(
        vehicle_source,
        vehicle_mapping,
        artifact_root=tmp_path / "artifacts",
        resolver=_resolver(known_locations),
        area_assignment_source=assignment_source,
        area_assignment_mapping=assignment_mapping,
        known_area_ids={"A", "B"},
        chunk_size=1,
    )
    replay = normalize_supply(
        vehicle_source,
        vehicle_mapping,
        artifact_root=tmp_path / "artifacts",
        resolver=_resolver(known_locations),
        area_assignment_source=assignment_source,
        area_assignment_mapping=assignment_mapping,
        known_area_ids={"A", "B"},
        chunk_size=10,
    )
    assert replay.reference == result.reference
    assert replay.vehicles == result.vehicles
    assert [item.key.vehicle_id for item in result.vehicles] == ["001", "002"]
    assert result.vehicles[0].assigned_area_ids == ("B",)
    assert result.catalog.vehicle_keys == tuple(item.key for item in result.vehicles)
    assert read_saved_supply_mappings(result.reference, artifact_root=tmp_path / "artifacts") == (
        vehicle_mapping,
        assignment_mapping,
    )

    invalid_path = tmp_path / "invalid_areas.csv"
    pd.DataFrame({"vehicle": ["missing"], "area": ["A"]}).to_csv(invalid_path, index=False)
    invalid = register_tabular_source(
        invalid_path, artifact_root=tmp_path / "artifacts", provenance="invalid assignment"
    )
    with pytest.raises(SupplyImportError) as caught:
        normalize_supply(
            vehicle_source,
            vehicle_mapping,
            artifact_root=tmp_path / "invalid-artifacts",
            resolver=_resolver(known_locations),
            area_assignment_source=invalid,
            area_assignment_mapping=assignment_mapping,
            known_area_ids={"A"},
            chunk_size=1,
        )
    assert "unknown vehicle" in caught.value.issues[0].message
    assert not (tmp_path / "invalid-artifacts" / "datasets").exists()


def test_location_and_od_task_adapters_resolve_coordinates_and_reject_crs_errors(
    tmp_path: Path, known_locations: dict[str, LocationRef]
) -> None:
    nodes = gpd.GeoDataFrame(
        {
            "node_id": ["N0", "N1"],
            "x_m": [2_530_000.0, 2_530_010.0],
            "y_m": [1_150_000.0, 1_150_000.0],
        },
        geometry=[Point(2_530_000, 1_150_000), Point(2_530_010, 1_150_000)],
        crs="EPSG:2056",
    )
    coordinate_resolver = LocationResolver(
        environment=ENVIRONMENT, snapper=NodeSnapper(nodes, max_distance_m=2.0)
    )
    location_path = tmp_path / "location.csv"
    pd.DataFrame(
        {
            "task": ["0007"],
            "release_minutes": [2],
            "x": [2_530_000],
            "y": [1_150_000],
        }
    ).to_csv(location_path, index=False)
    location_source = register_tabular_source(
        location_path, artifact_root=tmp_path / "artifacts", provenance="location fixture"
    )
    location_mapping_data = {
        "schema_version": "2.0",
        "adapter": "demand.upload_location@1",
        "structure": "location",
        "data_semantics": "observed_tasks",
        "fleet_id": "fleet",
        "columns": {
            "task_id": "task",
            "release_time": "release_minutes",
            "x": "x",
            "y": "y",
        },
        "location_representation": "coordinates",
        "source_crs": "EPSG:2056",
        "time": {"kind": "elapsed", "unit": "minutes"},
        "duration_unit": "seconds",
        "service_duration_default_s": 0.0,
        "quantity_mode": "none",
        "quantity_unit": None,
        "allow_generated_task_ids": False,
        "error_policy": "strict",
    }
    location_mapping = DemandImportMapping.model_validate(location_mapping_data)
    location_result = normalize_demand(
        location_source,
        location_mapping,
        artifact_root=tmp_path / "artifacts",
        resolver=coordinate_resolver,
    )
    assert location_result.tasks[0].task_id == "0007"
    assert location_result.tasks[0].release_s == 120
    assert location_result.locations[0].node_id == "N0"

    od_path = tmp_path / "od.parquet"
    pd.DataFrame(
        {
            "task": pd.Series(["09"], dtype="string"),
            "release": [0.0],
            "origin": pd.Series(["L0"], dtype="string"),
            "destination": pd.Series(["L1"], dtype="string"),
            "quantity": [3.0],
        }
    ).to_parquet(od_path, index=False)
    od_source = register_tabular_source(
        od_path, artifact_root=tmp_path / "artifacts", provenance="OD fixture"
    )
    od_mapping = DemandImportMapping.model_validate(
        {
            "schema_version": "2.0",
            "adapter": "demand.upload_od@1",
            "structure": "od",
            "data_semantics": "observed_tasks",
            "fleet_id": "fleet",
            "columns": {
                "task_id": "task",
                "release_time": "release",
                "origin_location_id": "origin",
                "destination_location_id": "destination",
                "quantity": "quantity",
            },
            "location_representation": "ids",
            "source_crs": None,
            "time": {"kind": "elapsed", "unit": "seconds"},
            "duration_unit": "seconds",
            "service_duration_default_s": 5.0,
            "quantity_mode": "occupancy",
            "quantity_unit": "passengers",
            "allow_generated_task_ids": False,
            "error_policy": "strict",
        }
    )
    od_result = normalize_demand(
        od_source,
        od_mapping,
        artifact_root=tmp_path / "artifacts",
        resolver=_resolver(known_locations),
    )
    assert od_result.tasks[0].task_id == "09"
    assert tuple(step.quantity_delta for step in od_result.tasks[0].steps) == (-3.0, 3.0)

    class UnreachableRouting:
        def route(self, profile_id: str, source_node_id: str, target_node_id: str):
            return SimpleNamespace(reachable=False, reason="no_path")

    unreachable_resolver = LocationResolver(
        environment=ENVIRONMENT,
        known_locations=known_locations,
        routing=UnreachableRouting(),
        routing_profile_id="profile",
    )
    with pytest.raises(DemandImportError) as unreachable:
        normalize_demand(
            od_source,
            od_mapping,
            artifact_root=tmp_path / "unreachable-artifacts",
            resolver=unreachable_resolver,
        )
    assert {item.code for item in unreachable.value.issues} == {"UNREACHABLE_TASK_LEG"}

    bad_path = tmp_path / "bad_crs.csv"
    pd.DataFrame({"task": ["bad"], "release_minutes": [0], "x": [46.0], "y": [120.0]}).to_csv(
        bad_path, index=False
    )
    bad_source = register_tabular_source(
        bad_path, artifact_root=tmp_path / "artifacts", provenance="swapped coordinate fixture"
    )
    bad_mapping = DemandImportMapping.model_validate(
        {**location_mapping_data, "source_crs": "EPSG:4326"}
    )
    with pytest.raises(DemandImportError) as caught:
        normalize_demand(
            bad_source,
            bad_mapping,
            artifact_root=tmp_path / "artifacts",
            resolver=coordinate_resolver,
        )
    assert "swapped" in caught.value.issues[0].message
