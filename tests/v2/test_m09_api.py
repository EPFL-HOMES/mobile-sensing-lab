from __future__ import annotations

import http.client
import socket
import threading
import time
from pathlib import Path

import uvicorn
from fastapi.testclient import TestClient
import pytest

from mobile_sensing.api import create_app
from mobile_sensing.api.generate_contracts import generate
from mobile_sensing.jobs import JobStore, JobStoreLimits, LocalCoordinator


def _environment_reference() -> dict[str, str]:
    return {
        "artifact_id": "environment_missing",
        "artifact_kind": "environment",
        "content_hash": "0" * 64,
    }


def test_http_submission_responsiveness_disconnect_and_idempotency(tmp_path) -> None:
    client = TestClient(create_app(tmp_path))
    project = client.post(
        "/api/v1/projects", json={"name": "M09", "description": "acceptance"}
    ).json()
    body = {
        "import_kind": "demand",
        "source_id": "dataset_missing",
        "environment": _environment_reference(),
        "mapping": {
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
            "time": {"kind": "elapsed", "unit": "seconds"},
            "duration_unit": "seconds",
            "service_duration_default_s": 0,
            "quantity_mode": "none",
        },
    }
    headers = {"X-Project-ID": project["project_id"], "Idempotency-Key": "request-1"}
    first = client.post("/api/v1/imports", json=body, headers=headers)
    assert first.status_code == 202
    assert client.get("/api/v1/health").status_code == 200
    assert client.post("/api/v1/imports", json=body, headers=headers).json() == first.json()
    changed = {**body, "source_id": "other"}
    assert client.post("/api/v1/imports", json=changed, headers=headers).status_code == 409
    client.post(f"/api/v1/jobs/{first.json()['job_id']}/cancel")

    store = JobStore(tmp_path)
    cpu_job, _ = store.submit(
        kind="test_probe",
        operation="responsive",
        payload={"steps": 100, "delay_s": 0.005},
    )
    coordinator = LocalCoordinator(tmp_path, max_workers=1)
    thread = threading.Thread(target=coordinator.run_once)
    thread.start()
    deadline = time.monotonic() + 5
    while store.get_job(cpu_job.job_id).status == "queued" and time.monotonic() < deadline:
        time.sleep(0.01)
    started = time.monotonic()
    assert client.get("/api/v1/health").status_code == 200
    assert time.monotonic() - started < 0.5
    # No event stream is retained here: disconnect has no cancellation side effect.
    thread.join(timeout=10)
    coordinator.close()
    assert not thread.is_alive()
    assert store.get_job(cpu_job.job_id).status == "completed"


def test_upload_limits_revisions_portfolio_job_isolation_and_common_errors(tmp_path) -> None:
    limits = JobStoreLimits(max_upload_bytes=1024, max_page_size=2)
    client = TestClient(create_app(tmp_path, limits=limits))
    oversized = client.post(
        "/api/v1/uploads",
        files={"file": ("large.csv", b"a\n" + b"1" * 1024, "text/csv")},
        data={"provenance": "test"},
    )
    assert oversized.status_code == 413
    assert set(oversized.json()) == {"code", "message", "issues", "request_id"}
    uploaded = client.post(
        "/api/v1/uploads",
        files={"file": ("observations.csv", b"a,b\n1,2\n3,4\n5,6\n", "text/csv")},
        data={"provenance": "test"},
    )
    assert uploaded.status_code == 201
    assert uploaded.json()["registration"]["original_filename"] == "observations.csv"
    dataset_id = uploaded.json()["registration"]["dataset_id"]
    first_page = client.get(f"/api/v1/datasets/{dataset_id}/preview?page_size=2").json()
    assert first_page["returned_count"] == 2 and not first_page["is_complete"]
    second_page = client.get(
        f"/api/v1/datasets/{dataset_id}/preview",
        params={"page_size": 2, "cursor": first_page["next_cursor"]},
    ).json()
    assert second_page["returned_count"] == 1 and second_page["is_complete"]
    store = JobStore(tmp_path)
    store.register_resource(
        resource_id="dependent",
        kind="import",
        content_hash="1" * 64,
        artifact_kind="dataset",
        job_id=None,
        metadata={},
        dependencies=({"artifact_id": dataset_id, "role": "source"},),
    )
    deletion = client.delete(f"/api/v1/datasets/{dataset_id}")
    assert deletion.status_code == 409 and "dependent" in deletion.json()["message"]

    invalid = client.post(
        "/api/v1/uploads",
        files={"file": ("invalid.csv", b"a\n\xff\n", "text/csv")},
        data={"provenance": "test"},
    )
    assert invalid.status_code == 422
    staging_id = invalid.json()["issues"][0]["dataset_id"]
    assert (tmp_path / "staging_uploads" / staging_id / "invalid.csv").is_file()

    project = client.post("/api/v1/projects", json={"name": "revision", "description": ""}).json()
    revision = client.post(
        f"/api/v1/projects/{project['project_id']}/revisions",
        json={"base_revision_id": None, "payload": {"R": 2, "J": 10}},
    )
    assert revision.status_code == 201
    conflict = client.post(
        f"/api/v1/projects/{project['project_id']}/revisions",
        json={"base_revision_id": None, "payload": {"R": 3, "J": 10}},
    )
    assert conflict.status_code == 409

    store.submit(
        kind="portfolio",
        operation="portfolio-analyses",
        payload={"change": "budget_only", "replications_R": 2, "sampling_rounds_J": 10},
    )
    assert len(store.list_jobs(kind="portfolio")) == 1
    assert store.list_jobs(kind="simulation") == []


def test_queued_and_running_cancellation_and_sse_replay(tmp_path) -> None:
    limits = JobStoreLimits(max_events_per_job=10)
    app = create_app(tmp_path, limits=limits)
    client = TestClient(app)
    store = JobStore(tmp_path, limits)
    queued, _ = store.submit(kind="test_probe", operation="queued", payload={"steps": 1})
    cancelled = client.post(f"/api/v1/jobs/{queued.job_id}/cancel")
    assert cancelled.json()["status"] == "cancelled"

    running, _ = store.submit(
        kind="test_probe", operation="running", payload={"steps": 200, "delay_s": 0.003}
    )
    coordinator = LocalCoordinator(tmp_path, max_workers=1, cancellation_grace_s=1.0)
    thread = threading.Thread(target=coordinator.run_once)
    thread.start()
    deadline = time.monotonic() + 5
    while (
        store.get_job(running.job_id).status not in {"running", "failed"}
        and time.monotonic() < deadline
    ):
        time.sleep(0.01)
    assert client.post(f"/api/v1/jobs/{running.job_id}/cancel").status_code == 200
    thread.join(timeout=10)
    coordinator.close()
    assert store.get_job(running.job_id).status == "cancelled"

    with client.stream("GET", f"/api/v1/jobs/{running.job_id}/events") as response:
        text = "".join(response.iter_text())
    assert "event: cancelled" in text
    assert "id:" in text

    replay, _ = store.submit(kind="test_probe", operation="replay", payload={"steps": 20})
    claimed, token = store.claim_next("replay-owner", 30)
    store.mark_running(claimed.job_id, token)
    for index in range(15):
        store.record_progress(replay.job_id, token, phase="replay", completed=index, total=20)
    store.mark_finalizing(replay.job_id, token)
    store.complete(replay.job_id, token, {"resource_id": replay.resource_id})
    with client.stream(
        "GET",
        f"/api/v1/jobs/{replay.job_id}/events",
        headers={"Last-Event-ID": "1"},
    ) as response:
        replay_text = "".join(response.iter_text())
    assert "event: reset" in replay_text


def test_openapi_and_frontend_contracts_regenerate_exactly(tmp_path) -> None:
    root = Path(__file__).resolve().parents[2]
    openapi = tmp_path / "openapi.json"
    typescript = tmp_path / "generated.ts"
    generate(openapi, typescript)
    assert (
        openapi.read_bytes()
        == (root / "tests/v2/fixtures/release_baselines/openapi.json").read_bytes()
    )
    assert typescript.read_bytes() == (root / "frontend/src/api/generated.ts").read_bytes()
    schema = create_app(tmp_path / "api").openapi()
    required = {
        "/api/v1/jobs/{job_id}/events",
        "/api/v1/matrix-queries",
        "/api/v1/portfolio-analyses",
        "/api/v1/results/{resource_id}/{table_name}",
    }
    assert required <= set(schema["paths"])


def test_real_sse_disconnect_does_not_cancel_active_job(tmp_path) -> None:
    app = create_app(tmp_path)
    with socket.socket() as reserved:
        try:
            reserved.bind(("127.0.0.1", 0))
        except PermissionError:
            pytest.skip("sandbox forbids loopback sockets")
        port = reserved.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical"))
    server_thread = threading.Thread(target=server.run)
    server_thread.start()
    deadline = time.monotonic() + 5
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started

    store = JobStore(tmp_path)
    job, _ = store.submit(
        kind="test_probe", operation="disconnect", payload={"steps": 100, "delay_s": 0.004}
    )
    coordinator = LocalCoordinator(tmp_path)
    coordinator_thread = threading.Thread(target=coordinator.run_once)
    coordinator_thread.start()
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request("GET", f"/api/v1/jobs/{job.job_id}/events")
    response = connection.getresponse()
    assert response.status == 200
    assert response.read(8)
    connection.close()

    health = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    started = time.monotonic()
    health.request("GET", "/api/v1/health")
    assert health.getresponse().status == 200
    assert time.monotonic() - started < 0.5
    health.close()
    coordinator_thread.join(timeout=10)
    coordinator.close()
    server.should_exit = True
    server_thread.join(timeout=10)
    assert store.get_job(job.job_id).status == "completed"
