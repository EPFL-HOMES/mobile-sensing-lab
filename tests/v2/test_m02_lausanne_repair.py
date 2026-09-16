from __future__ import annotations

import json
from pathlib import Path

import networkx as nx


ROOT = Path(__file__).resolve().parents[2]


def test_lausanne_conservative_repair_artifact_matches_evidence(
    lausanne_repaired_environment,
) -> None:
    environment, _ = lausanne_repaired_environment
    summary = environment.repair_summary.iloc[0].to_dict()
    assert summary["source_row_count"] == 54_517
    assert summary["retained_source_row_count"] == 54_473
    assert summary["split_source_row_count"] == 42
    assert summary["quarantined_source_row_count"] == 2
    assert summary["emitted_component_count"] == 54_699
    assert summary["split_source_node_count"] == 6
    assert summary["readiness_grade"] == "not_ready"
    assert set(environment.quarantined_edges.source_record_id) == {
        "18392/20653/0",
        "20653/18392/0",
    }
    assert environment.metadata.road_node_count == 20_679
    assert environment.metadata.directed_edge_count == 108_191
    assert environment.metadata.network_repair_hash == summary["repair_hash"]
    assert environment.metadata.network_repair_policy == "conservative_repair@1"

    graph = nx.Graph()
    graph.add_edges_from(zip(environment.road_edges.u_node_id, environment.road_edges.v_node_id))
    assert nx.number_connected_components(graph) == 1

    evidence = json.loads(
        (ROOT / "tests/v2/fixtures/release_baselines/m02_lausanne_repair.json").read_text()
    )
    assert evidence["environment"] == environment.reference.model_dump(mode="json")
    assert evidence["repair_summary"] == summary
    assert evidence["quarantined_source_record_ids"] == sorted(
        environment.quarantined_edges.source_record_id
    )
    assert evidence["post_repair_validation"]["weak_component_count"] == 1
