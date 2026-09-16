"""Small metric road/grid fixtures for M02 acceptance tests."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
from shapely.geometry import MultiLineString, box

from mobile_sensing.contracts import EnvironmentBuildConfig, EnvironmentProviderRequest
from mobile_sensing.environment import (
    LocalDatasetCatalog,
    LocalDatasetResource,
    LocalRegionSelection,
)


X0 = 2_530_000.0
Y0 = 1_150_000.0


@dataclass(frozen=True)
class NoCancellation:
    def is_cancelled(self) -> bool:
        return False


class RecordedProgress:
    def __init__(self) -> None:
        self.updates: list[tuple[str, int, int | None]] = []

    def update(self, *, phase: str, completed: int, total: int | None) -> None:
        self.updates.append((phase, completed, total))


def analytic_roads() -> gpd.GeoDataFrame:
    lines = [
        MultiLineString([[(X0, Y0), (X0 + 10, Y0)]]),
        MultiLineString([[(X0, Y0), (X0 + 5, Y0 + 1), (X0 + 10, Y0)]]),
        MultiLineString([[(X0 + 10, Y0), (X0 + 20, Y0)]]),
        MultiLineString([[(X0, Y0 + 20), (X0 + 10, Y0 + 20)]]),
    ]
    return gpd.GeoDataFrame(
        {
            "u": ["A", "A", "B", "D"],
            "v": ["B", "B", "C", "E"],
            "key": ["0", "1", "0", "0"],
            "one_way": [False, True, True, True],
            "edge_seconds": [5.0, 9.0, 5.0, 5.0],
            "speed_mps": [2.0, None, 2.0, 2.0],
            "road_class": ["local", "local", "arterial", None],
        },
        geometry=lines,
        crs="EPSG:2056",
    )


def write_analytic_sources(directory: Path) -> LocalDatasetCatalog:
    directory.mkdir(parents=True, exist_ok=True)
    boundary_path = directory / "boundary.gpkg"
    roads_path = directory / "roads.gpkg"
    gpd.GeoDataFrame(
        {"name": ["Testville"]},
        geometry=[box(X0 - 5, Y0 - 5, X0 + 35, Y0 + 25)],
        crs="EPSG:2056",
    ).to_file(boundary_path, layer="boundary", driver="GPKG")
    analytic_roads().to_file(roads_path, layer="roads", driver="GPKG")
    return LocalDatasetCatalog(
        (
            LocalDatasetResource(
                "analytic_boundary", "boundary", boundary_path, "EPSG:2056", "boundary"
            ),
            LocalDatasetResource("analytic_roads", "network", roads_path, "EPSG:2056", "roads"),
        ),
        network_dataset_id="analytic_roads",
        region_selections={
            "analytic_routing_extent": LocalRegionSelection("analytic_boundary", ("Testville",))
        },
    )


def provider_request() -> EnvironmentProviderRequest:
    return EnvironmentProviderRequest.model_validate(
        {
            "schema_version": "2.0",
            "provider": "environment.local_files@1",
            "boundary": {
                "kind": "local_municipalities",
                "dataset_id": "analytic_boundary",
                "municipality_names": ("Testville",),
            },
            "routing_extent_ref": "analytic_routing_extent",
            "target_crs": "EPSG:2056",
            "network_mode": "drive",
            "requested_features": (),
            "cache_policy": "reuse",
        }
    )


def build_config(*, cell_size_m: float = 10.0) -> EnvironmentBuildConfig:
    return EnvironmentBuildConfig.model_validate(
        {
            "schema_version": "2.0",
            "provider": "environment.local_files@1",
            "boundary": {
                "kind": "local_municipalities",
                "dataset_id": "analytic_boundary",
                "municipality_names": ("Testville",),
            },
            "routing_extent_ref": "analytic_routing_extent",
            "network_source": {
                "kind": "supplied",
                "dataset_id": "analytic_roads",
                "layer": "roads",
            },
            "network_mode": "drive",
            "working_crs": "EPSG:2056",
            "travel_time_profiles": (
                {
                    "profile_id": "constant_2mps",
                    "algorithm": "static_shortest_travel_time",
                    "source": {"kind": "constant_speed", "speed_mps": 2.0},
                },
            ),
            "snapping": {"max_distance_m": 5.0, "tie_break": "canonical_node_id"},
            "grid": {
                "kind": "regular",
                "cell_size_m": cell_size_m,
                "origin_easting_m": X0,
                "origin_northing_m": Y0,
            },
            "population_features": None,
        }
    )
