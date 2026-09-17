from __future__ import annotations

import geopandas as gpd
import pytest
from shapely.geometry import LineString, MultiLineString, box

from mobile_sensing.contracts.configuration import (
    EdgeTravelTimeSourceConfig,
    TravelTimeProfileConfig,
)
from mobile_sensing.environment import (
    NetworkPreparationError,
    PreparedRoutingService,
    diagnose_network,
    plan_network_repair,
    prepare_network,
    validate_scenario_impact,
)
from tests.support.environment_fixtures import X0, Y0


SOURCE_HASH = "a" * 64


def _roads() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {
            "u": ["E", "A", "D", "Q", "X", "X"],
            "v": ["A", "D", "F", "R", "Y", "Z"],
            "key": ["0", "0", "0", "0", "0", "0"],
            "one_way": [False, False, False, False, True, True],
            "edge_seconds": [10.0, 50.0, 10.0, 20.0, 5.0, 5.0],
        },
        geometry=[
            LineString([(X0 - 10, Y0), (X0, Y0)]),
            MultiLineString(
                [
                    [(X0, Y0), (X0 + 10, Y0)],
                    [(X0 + 10, Y0), (X0 + 10, Y0 + 10), (X0 + 10, Y0)],
                    [(X0 + 10, Y0), (X0 + 30, Y0)],
                ]
            ),
            LineString([(X0 + 30, Y0), (X0 + 40, Y0)]),
            MultiLineString(
                [
                    [(X0, Y0 + 30), (X0 + 5, Y0 + 30)],
                    [(X0 + 15, Y0 + 30), (X0 + 20, Y0 + 30)],
                ]
            ),
            LineString([(X0 + 100, Y0), (X0 + 110, Y0)]),
            LineString([(X0 + 200, Y0), (X0 + 210, Y0)]),
        ],
        crs="EPSG:2056",
    )


def _profile() -> TravelTimeProfileConfig:
    return TravelTimeProfileConfig(
        profile_id="direct",
        source=EdgeTravelTimeSourceConfig(kind="edge_travel_time", field="edge_seconds"),
    )


def test_conservative_repair_preserves_components_lineage_and_source_time() -> None:
    roads = _roads()
    diagnosis = diagnose_network(roads)
    assert diagnosis.summary["status_counts"] == {
        "disconnected": 1,
        "repairable_branch": 1,
        "valid": 4,
    }
    assert diagnosis.summary["endpoint_conflict_node_count"] == 1
    strict_plan = plan_network_repair(roads, policy="strict@1", source_content_hash=SOURCE_HASH)
    assert strict_plan.readiness_grade == "not_ready"
    assert (strict_plan.actions.action == "rejected").sum() == 2

    with pytest.raises(NetworkPreparationError, match="2 disconnected MultiLineStrings"):
        prepare_network(
            roads,
            box(X0 - 20, Y0 - 20, X0 + 230, Y0 + 50),
            working_crs="EPSG:2056",
            profiles=(_profile(),),
            source_content_hash=SOURCE_HASH,
        )

    prepared = prepare_network(
        roads,
        box(X0 - 20, Y0 - 20, X0 + 230, Y0 + 50),
        working_crs="EPSG:2056",
        profiles=(_profile(),),
        source_content_hash=SOURCE_HASH,
        repair_policy="conservative_repair@1",
    )
    summary = prepared.repair.repair_summary.iloc[0]
    assert summary.readiness_grade == "not_ready"
    assert summary.split_source_row_count == 1
    assert summary.quarantined_source_row_count == 1
    assert summary.split_source_node_count == 1
    assert prepared.repair.quarantined_edges.source_record_id.tolist() == ["Q/R/0"]
    assert "Q/R/0" not in set(prepared.edges.source_record_id)

    branch = prepared.edges.query("source_record_id == 'A/D/0' and direction == 'forward'")
    assert len(branch) == 3
    weights = prepared.routing_weights.loc[prepared.routing_weights.edge_id.isin(branch.edge_id)]
    assert weights.duration_s.sum() == pytest.approx(50.0)
    assert set(prepared.edge_lineage.edge_id) == set(prepared.edges.edge_id)

    routing = PreparedRoutingService(
        prepared.nodes,
        prepared.edges,
        prepared.routing_weights,
        network_hash=prepared.network_hash,
        profile_hashes=prepared.profile_hashes,
    )
    impact = validate_scenario_impact(
        routing,
        profile_id="direct",
        required_pairs=(("A", "D"), ("D", "A")),
        quarantined_source_count=1,
    )
    assert impact.passed and impact.reachable_pair_count == 2
    assert impact.readiness_grade == "scenario_ready_with_quarantine"


def test_repair_is_stable_under_source_row_shuffle() -> None:
    roads = _roads()
    extent = box(X0 - 20, Y0 - 20, X0 + 230, Y0 + 50)
    outputs = [
        prepare_network(
            frame,
            extent,
            working_crs="EPSG:2056",
            profiles=(_profile(),),
            source_content_hash=SOURCE_HASH,
            repair_policy="conservative_repair@1",
        )
        for frame in (roads, roads.sample(frac=1, random_state=17))
    ]
    assert outputs[0].repair.repair_hash == outputs[1].repair.repair_hash
    assert outputs[0].network_hash == outputs[1].network_hash
    assert outputs[0].profile_hashes == outputs[1].profile_hashes


def test_quarantine_policy_does_not_reinterpret_branching_geometry() -> None:
    prepared = prepare_network(
        _roads(),
        box(X0 - 20, Y0 - 20, X0 + 230, Y0 + 50),
        working_crs="EPSG:2056",
        profiles=(_profile(),),
        source_content_hash=SOURCE_HASH,
        repair_policy="quarantine_invalid@1",
    )
    assert set(prepared.repair.quarantined_edges.source_record_id) == {"A/D/0", "Q/R/0"}
    assert not (prepared.edges.source_record_id == "A/D/0").any()
