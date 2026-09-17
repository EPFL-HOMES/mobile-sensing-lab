from __future__ import annotations

import json
from pathlib import Path

from mobile_sensing.datasets import (
    GTFSReconstructionConfig,
    discover_gtfs_directory,
    reconstruct_gtfs,
)
from mobile_sensing.datasets.parsing import LocationResolver
from mobile_sensing.environment import validate_scenario_impact


ROOT = Path(__file__).resolve().parents[2]


def test_bounded_real_lausanne_routed_reconstruction(
    lausanne_repaired_environment,
) -> None:
    environment, artifact_root = lausanne_repaired_environment
    config = GTFSReconstructionConfig.model_validate_json(
        (ROOT / "docs" / "templates" / "gtfs" / "lausanne_route_13.json").read_text()
    )
    resolver = LocationResolver(
        environment=environment.reference,
        snapper=environment.snapping,
        routing=environment.routing,
        routing_profile_id="road_static_30kph",
    )
    result = reconstruct_gtfs(
        discover_gtfs_directory(ROOT / "data" / "Lausanne" / "gtfs"),
        config,
        artifact_root=artifact_root,
        resolver=resolver,
        sensing_boundary=environment.boundary.geometry.iloc[0],
    )
    assert result.diagnostics["selected_trips"] == 146
    assert result.diagnostics["accepted_trips"] == 146
    assert result.diagnostics["rejected_trips"] == 0
    assert result.diagnostics["route_failures"] == ()
    assert result.diagnostics["duties"] == 3
    assert result.diagnostics["service_tasks"] == 146
    assert result.diagnostics["reposition_tasks"] == 143
    assert len(result.tasks) == 289
    assert len(result.vehicles) == 3
    assert len(result.locations) == 14
    assert max(item.snap_distance_m for item in result.locations) < 45.077

    snapped_nodes = tuple(sorted({item.node_id for item in result.locations}))
    pairs = tuple(
        (source, target) for source in snapped_nodes for target in snapped_nodes if source != target
    )
    impact = validate_scenario_impact(
        environment.routing,
        profile_id="road_static_30kph",
        required_pairs=pairs,
        quarantined_source_count=environment.metadata.quarantined_source_rows,
    )
    assert impact.passed
    assert impact.readiness_grade == "scenario_ready_with_quarantine"
    assert impact.required_pair_count == 132
    touched = environment.road_edges.loc[environment.road_edges.edge_id.isin(impact.route_edge_ids)]
    assert set(touched.repair_action) == {"retained"}
    split_nodes = set(
        environment.node_lineage.loc[
            environment.node_lineage.node_kind == "split_conflict", "derived_node_id"
        ]
    )
    assert not (set(touched.u_node_id) | set(touched.v_node_id)) & split_nodes

    evidence = json.loads(
        (ROOT / "tests/fixtures/release_baselines/lausanne_reconstruction.json").read_text()
    )
    assert evidence["environment"] == environment.reference.model_dump(mode="json")
    assert evidence["reconstruction"] == result.reference.model_dump(mode="json")
    assert evidence["diagnostics"] == {
        **result.diagnostics,
        "route_failures": list(result.diagnostics["route_failures"]),
    }
    assert evidence["scenario_impact"]["required_pair_count"] == 132
    assert evidence["scenario_impact"]["unreachable_pair_count"] == 0
