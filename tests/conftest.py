from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import pytest

from mobile_sensing.contracts import EnvironmentBuildConfig, EnvironmentProviderRequest
from mobile_sensing.environment import (
    LocalDatasetCatalog,
    LocalDatasetResource,
    LocalEnvironmentBuilder,
    LocalFileEnvironmentProvider,
    LocalRegionSelection,
    PreparedEnvironmentReader,
)
from tests.support.environment_fixtures import NoCancellation, RecordedProgress


@pytest.fixture(scope="session")
def lausanne_repaired_environment(tmp_path_factory):
    data = Path(__file__).resolve().parents[1] / "data" / "Lausanne"
    if not (data / "lausanne_roads_encoded.gpkg").is_file():
        pytest.skip("bundled Lausanne road data are unavailable")
    boundaries = gpd.read_file(data / "boundary_lausanne.gpkg")
    names = tuple(sorted(boundaries.name.astype(str)))
    catalog = LocalDatasetCatalog(
        (
            LocalDatasetResource(
                "boundary_lausanne",
                "boundary",
                data / "boundary_lausanne.gpkg",
                "EPSG:4326",
            ),
            LocalDatasetResource(
                "roads_encoded",
                "network",
                data / "lausanne_roads_encoded.gpkg",
                "EPSG:4326",
                "roads_encoded",
            ),
            LocalDatasetResource(
                "population_swiss_2024",
                "population",
                data / "population_swiss.csv",
                "EPSG:2056",
                population_cell_size_m=100.0,
            ),
        ),
        network_dataset_id="roads_encoded",
        feature_dataset_ids={"population": "population_swiss_2024"},
        region_selections={
            "routing_extent_lausanne": LocalRegionSelection("boundary_lausanne", names)
        },
    )
    request = EnvironmentProviderRequest.model_validate(
        {
            "schema_version": "2.0",
            "provider": "environment.local_files@1",
            "boundary": {
                "kind": "local_municipalities",
                "dataset_id": "boundary_lausanne",
                "municipality_names": ("Lausanne (suburban)", "Lausanne (urban)"),
            },
            "routing_extent_ref": "routing_extent_lausanne",
            "target_crs": "EPSG:2056",
            "network_mode": "drive",
            "requested_features": ("population",),
            "cache_policy": "reuse",
        }
    )
    config = EnvironmentBuildConfig.model_validate(
        {
            "schema_version": "2.0",
            "provider": "environment.local_files@1",
            "boundary": request.boundary.model_dump(),
            "routing_extent_ref": "routing_extent_lausanne",
            "network_source": {
                "kind": "supplied",
                "dataset_id": "roads_encoded",
                "layer": "roads_encoded",
                "topology_policy": "conservative_repair@1",
                "endpoint_tolerance_m": 0.05,
            },
            "network_mode": "drive",
            "working_crs": "EPSG:2056",
            "travel_time_profiles": (
                {
                    "profile_id": "road_static_30kph",
                    "source": {
                        "kind": "constant_speed",
                        "speed_mps": 8.333333333333334,
                    },
                },
            ),
            "snapping": {"max_distance_m": 250.0},
            "grid": {
                "kind": "regular",
                "cell_size_m": 100.0,
                "origin_easting_m": 2530000.0,
                "origin_northing_m": 1150000.0,
            },
            "population_features": {
                "dataset_id": "population_swiss_2024",
                "year": 2024,
                "missing_policy": "zero",
            },
        }
    )
    bundle = LocalFileEnvironmentProvider(catalog).acquire(
        request, cancellation=NoCancellation(), progress=RecordedProgress()
    )
    artifact_root = tmp_path_factory.mktemp("lausanne-repair-artifacts")
    reference = LocalEnvironmentBuilder(catalog, artifact_root).prepare(
        bundle,
        config,
        cancellation=NoCancellation(),
        progress=RecordedProgress(),
    )
    environment = PreparedEnvironmentReader(artifact_root).read(reference)
    return environment, artifact_root
