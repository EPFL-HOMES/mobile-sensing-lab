from __future__ import annotations

import json

import pytest

from tests.support import compare_simulation_runs as comparator
from tests.support.build_acceptance_workspace import build


@pytest.mark.parametrize("role", list(comparator.COLLECTIONS))
def test_comparison_rejects_each_changed_artifact_role(tmp_path, monkeypatch, role):
    report = {"artifacts": {name: {"artifact_id": name} for name in comparator.COLLECTIONS}}
    first = tmp_path / "a.json"
    second = tmp_path / "b.json"
    first.write_text(json.dumps(report))
    second.write_text(json.dumps(report))

    def evidence(root, selected_role, reference):
        return {"content": 1 if root.name == "b" and selected_role == role else 0}

    monkeypatch.setattr(comparator, "artifact_evidence", evidence)
    with pytest.raises(AssertionError, match=f"content differs for {role}"):
        comparator.compare(tmp_path / "a", first, tmp_path / "b", second)


def test_comparison_requires_portfolio_evidence(tmp_path):
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"artifacts": {}}))
    with pytest.raises(AssertionError, match="all seven"):
        comparator.compare(tmp_path, report, tmp_path, report)


def test_portfolio_evidence_includes_all_draws_and_detects_corruption(tmp_path):
    workspace = build(tmp_path)
    root = tmp_path / "artifacts"
    directory = root / "portfolios" / workspace["portfolio_samples_id"]
    manifest = json.loads((directory / "manifest.json").read_text())
    reference = {
        "artifact_id": manifest["artifact_id"],
        "artifact_kind": "portfolio",
        "content_hash": manifest["content_fingerprint"],
    }
    evidence = comparator.artifact_evidence(root, "portfolio_samples", reference)
    assert len(evidence["tables"]) == 8
    assert evidence["tables"]["sampling_rounds"]["row_count"] == 8
    assert evidence["tables"]["portfolio_samples"]["row_count"] == 64
    path = next(directory.rglob("*.parquet"))
    path.write_bytes(b"corrupted test artifact")
    with pytest.raises(ValueError, match="checksum"):
        comparator.artifact_evidence(root, "portfolio_samples", reference)
