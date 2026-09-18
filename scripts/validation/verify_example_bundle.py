"""Explicit full-example fresh-workspace acceptance (outside the unit suite)."""

import argparse
import json
import time
from pathlib import Path
from fastapi.testclient import TestClient
from mobile_sensing.api import create_app
from mobile_sensing.application.example_build import feasible_portfolio_count
from mobile_sensing.application.project_models import PortfolioEditor
from mobile_sensing.jobs import JobStore, LocalCoordinator


def verify(root):
    root = Path(root).resolve()
    if (root / "metadata.sqlite").exists():
        raise ValueError("Use a fresh validation workspace")
    client, timings = TestClient(create_app(root)), {}
    store = JobStore(root)

    def finish(response, name):
        assert response.status_code in {200, 202}, response.text
        started = time.perf_counter()
        job_id = response.json()["job_id"]
        with LocalCoordinator(root, max_workers=1) as coordinator:
            coordinator.run_once()
        job = store.get_job(job_id)
        assert job.status == "completed", job.error_message
        timings[name] = time.perf_counter() - started
        print(
            json.dumps({"stage": name, "status": job.status, "seconds": timings[name]}), flush=True
        )
        return job

    installed = finish(
        client.post("/api/v1/examples/lausanne/open", json={"editable": False}), "first_import"
    )
    finalized = client.post(f"/api/v1/examples/lausanne/finalize/{installed.job_id}")
    assert finalized.status_code == 200, finalized.text
    project = finalized.json()
    config = store.get_revision(project["project_id"], project["current_revision_id"]).payload
    assert config["read_only"] and len(client.get("/api/v1/inputs").json()) == 5
    assert config["simulation"]["temporal_resolution_minutes"] == 60.0
    runs = client.get("/api/v1/workbench/runs", params={"project_id": project["project_id"]}).json()
    assert len(runs) == 1 and runs[0]["replications"] == 10
    source = runs[0]
    analyses = client.get(
        "/api/v1/workbench/analyses", params={"project_id": project["project_id"]}
    ).json()
    assert len(analyses) == 2
    assert {analysis["config"]["risk_metric"] for analysis in analyses} == {"p05", "std"}
    assert all(analysis["count_portfolios"] == 600 for analysis in analyses)
    started = time.perf_counter()
    reopened = client.post("/api/v1/examples/lausanne/open", json={"editable": False}).json()
    timings["reopen_metadata"] = time.perf_counter() - started
    assert reopened["project_id"] == project["project_id"] and reopened["job_id"] is None
    copy = client.post(
        f"/api/v1/workbench/projects/{project['project_id']}/copy", json={"config": config}
    )
    assert copy.status_code == 201, copy.text
    copied = copy.json()
    config = store.get_revision(copied["project_id"], copied["current_revision_id"]).payload
    assert not config["read_only"]
    config["simulation"]["temporal_resolution_minutes"] = 30.0
    headers = {"X-Project-ID": copied["project_id"]}
    rebinned = finish(
        client.post(
            "/api/v1/workbench/runs",
            headers=headers,
            json={
                "name": "Editable copy · 30-minute reporting",
                "config": config,
                "options": {"memory_limit_bytes": 8 * 1024**3},
            },
        ),
        "rebin",
    )
    assert rebinned.result["mobility_reused"]
    assert rebinned.result["simulation"] == source["simulation"]
    assert rebinned.result["exposure"] != source["exposure"]
    editor = client.get(f"/api/v1/workbench/runs/{source['run_id']}/portfolio-defaults").json()
    editor["spatial_weight"], editor["sampling_runs"] = "uniform", 3
    for fleet in editor["fleets"]:
        fleet["count_range"] = None
        fleet["counts"] = [0, max(fleet["counts"])]
    analyzed = finish(
        client.post(
            "/api/v1/workbench/analyses",
            headers=headers,
            json={"name": "Editable copy · uniform weights", "config": editor},
        ),
        "uniform_analysis",
    )
    assert (
        analyzed.result["source_run_id"] == source["run_id"]
        and analyzed.result["sampling_runs"] == 3
    )
    assert analyzed.result["count_portfolios"] == feasible_portfolio_count(
        PortfolioEditor.model_validate_json(json.dumps(editor))
    )
    assert client.delete(f"/api/v1/projects/{project['project_id']}").status_code == 204
    retained = client.get(
        "/api/v1/workbench/runs", params={"project_id": copied["project_id"]}
    ).json()
    assert len(retained) == 2 and any(run["run_id"] == source["run_id"] for run in retained)
    report = {
        "passed": True,
        "workspace": str(root),
        "input_count": 5,
        "copied_project": copied["project_id"],
        "retained_run_count": len(retained),
        "rebinned_run": rebinned.result,
        "new_analysis": analyzed.result,
        "elapsed_seconds": timings,
    }
    (root / "acceptance.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    print(json.dumps({"passed": verify(parser.parse_args().root)["passed"]}))
