from __future__ import annotations

from fastapi.testclient import TestClient

from mobile_sensing.api import create_app
from mobile_sensing.jobs import JobStore
from tests.application.test_headless_application import _bundle, _prepared, _upload_inputs


def test_revision_payload_and_project_job_history_support_reload(tmp_path) -> None:
    client = TestClient(create_app(tmp_path))
    project = client.post("/api/v1/projects", json={"name": "M10", "description": "reload"}).json()
    revision = client.post(
        f"/api/v1/projects/{project['project_id']}/revisions",
        json={
            "base_revision_id": None,
            "payload": {
                "schema_version": "m10-ui-draft@1",
                "simulation": {"operationalReplicationsR": 2},
                "portfolio": {"samplingRoundsJ": 17},
            },
        },
    ).json()
    listed = client.get(f"/api/v1/projects/{project['project_id']}/revisions")
    assert listed.status_code == 200
    assert [item["revision_id"] for item in listed.json()] == [revision["revision_id"]]
    loaded = client.get(
        f"/api/v1/projects/{project['project_id']}/revisions/{revision['revision_id']}"
    )
    assert loaded.status_code == 200
    assert loaded.json()["payload"]["simulation"]["operationalReplicationsR"] == 2
    assert loaded.json()["payload"]["portfolio"]["samplingRoundsJ"] == 17

    other = client.post("/api/v1/projects", json={"name": "other", "description": ""}).json()
    store = JobStore(tmp_path)
    expected, _ = store.submit(
        kind="test_probe",
        operation="m10-project-job",
        payload={"steps": 1},
        project_id=project["project_id"],
    )
    store.submit(
        kind="test_probe",
        operation="m10-other-job",
        payload={"steps": 2},
        project_id=other["project_id"],
    )
    history = client.get("/api/v1/jobs", params={"project_id": project["project_id"]})
    assert history.status_code == 200
    assert [item["job_id"] for item in history.json()] == [expected.job_id]


def test_m10_openapi_contains_reload_endpoints(tmp_path) -> None:
    paths = create_app(tmp_path).openapi()["paths"]
    assert "/api/v1/jobs" in paths
    assert "/api/v1/projects/{project_id}/revisions" in paths
    assert "/api/v1/projects/{project_id}/revisions/{revision_id}" in paths


def test_browser_json_scenario_arrays_and_datetimes_cross_http_boundary(tmp_path) -> None:
    application, environment = _prepared(tmp_path)
    demand, supply, locations = _upload_inputs(application, environment, tmp_path)
    client = TestClient(create_app(application.artifact_root))
    project = client.post(
        "/api/v1/projects", json={"name": "browser JSON", "description": "M10"}
    ).json()
    response = client.post(
        "/api/v1/scenarios/validate",
        headers={"X-Project-ID": project["project_id"]},
        json={
            "environment": environment.reference.model_dump(mode="json"),
            "resources": _bundle(environment, demand, supply, locations).model_dump(mode="json"),
        },
    )
    assert response.status_code == 202
