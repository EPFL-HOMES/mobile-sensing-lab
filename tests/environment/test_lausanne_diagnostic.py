from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import pytest

from mobile_sensing.contracts.configuration import TravelTimeProfileConfig
from mobile_sensing.environment import NetworkPreparationError, prepare_network
from mobile_sensing.environment.diagnostics import build_lausanne_diagnostic


ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(
    not (ROOT / "data" / "Lausanne").is_dir(),
    reason="local Lausanne source data are not distributed with the public repository",
)


def test_checked_in_lausanne_diagnostic_matches_immutable_inputs() -> None:
    observed = build_lausanne_diagnostic(ROOT / "data" / "Lausanne")
    expected = json.loads(
        (ROOT / "tests/fixtures/release_baselines/lausanne_diagnostic.json").read_text()
    )
    assert observed == expected
    assert observed["selected_municipalities"] == [
        "Lausanne (suburban)",
        "Lausanne (urban)",
    ]
    assert observed["external_network_access"] is False
    assert observed["capabilities"]["environment.osm@1"] is False
    assert observed["network"]["nonmergeable_multilinestring_count"] == 44
    assert observed["network"]["repairable_branching_multilinestring_count"] == 42
    assert observed["network"]["disconnected_multilinestring_count"] == 2
    assert observed["network"]["endpoint_conflict_node_count"] == 6
    assert observed["grid"]["boundary_area_coverage_fraction"] == pytest.approx(1.0)


def test_strict_lausanne_network_preparation_rejects_reported_topology() -> None:
    data = ROOT / "data" / "Lausanne"
    boundaries = gpd.read_file(data / "boundary_lausanne.gpkg").to_crs("EPSG:2056")
    routing_extent = boundaries.geometry.union_all()
    roads = gpd.read_file(data / "lausanne_roads_encoded.gpkg")
    profile = TravelTimeProfileConfig.model_validate(
        {
            "profile_id": "assumed_30kph",
            "source": {"kind": "constant_speed", "speed_mps": 30.0 / 3.6},
        }
    )
    with pytest.raises(NetworkPreparationError, match="44 disconnected MultiLineStrings"):
        prepare_network(
            roads,
            routing_extent,
            working_crs="EPSG:2056",
            profiles=(profile,),
            source_content_hash="6069a3432588425d6fb70a8874f7cc8895fb997a8f5eb6700632cde026d61276",
        )
