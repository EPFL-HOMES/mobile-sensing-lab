from __future__ import annotations

import time

import pytest

from mobile_sensing.jobs import (
    CoordinatorAlreadyRunning,
    IdempotencyConflict,
    JobStore,
    JobStoreLimits,
    LeaseFenceError,
    LocalCoordinator,
    RevisionConflict,
)


def test_projects_revisions_idempotency_cache_and_fencing(tmp_path) -> None:
    store = JobStore(tmp_path)
    project = store.create_project("experiment", "M09")
    first_revision = store.create_revision(project.project_id, None, {"value": 1})
    with pytest.raises(RevisionConflict):
        store.create_revision(project.project_id, None, {"value": 2})
    second_revision = store.create_revision(
        project.project_id, first_revision.revision_id, {"value": 1}
    )
    assert second_revision.revision_id != first_revision.revision_id

    first, hit = store.submit(
        kind="test_probe",
        operation="probe",
        payload={"steps": 1},
        project_id=project.project_id,
        idempotency_key="same",
    )
    assert not hit and first.status == "queued"
    repeated, hit = store.submit(
        kind="test_probe",
        operation="probe",
        payload={"steps": 1},
        project_id=project.project_id,
        idempotency_key="same",
    )
    assert repeated.job_id == first.job_id and not hit
    with pytest.raises(IdempotencyConflict):
        store.submit(
            kind="test_probe",
            operation="probe",
            payload={"steps": 2},
            project_id=project.project_id,
            idempotency_key="same",
        )

    claimed, token = store.claim_next("owner", 10.0)
    store.mark_running(claimed.job_id, token)
    store.record_progress(claimed.job_id, token, phase="probe", completed=1, total=1)
    store.mark_finalizing(claimed.job_id, token)
    completed = store.complete(claimed.job_id, token, {"resource_id": claimed.resource_id})
    assert completed.status == "completed"
    with pytest.raises(LeaseFenceError):
        store.complete(claimed.job_id, token, {"resource_id": claimed.resource_id})

    cached, hit = store.submit(
        kind="test_probe",
        operation="probe",
        payload={"steps": 1},
        project_id=project.project_id,
        idempotency_key="different",
    )
    assert hit and cached.status == "completed"
    assert cached.job_id != completed.job_id
    assert cached.cache_source_job_id == completed.job_id
    assert cached.resource_id == completed.resource_id


def test_cancel_replay_reset_recovery_and_reference_conflict(tmp_path) -> None:
    store = JobStore(tmp_path, JobStoreLimits(max_events_per_job=10))
    queued, _ = store.submit(kind="test_probe", operation="cancel", payload={"steps": 1})
    assert store.request_cancel(queued.job_id).status == "cancelled"

    active, _ = store.submit(kind="test_probe", operation="active", payload={"steps": 20})
    claimed, token = store.claim_next("owner", 0.01)
    store.mark_running(claimed.job_id, token)
    for index in range(15):
        store.record_progress(claimed.job_id, token, phase="probe", completed=index, total=20)
    events, reset = store.events_after(active.job_id, 1)
    assert reset and events[0].event_id > 1
    time.sleep(0.02)
    assert store.recover_expired() == (active.job_id,)
    assert store.get_job(active.job_id).error_code == "worker_lost"
    with pytest.raises(LeaseFenceError):
        store.complete(active.job_id, token, {"resource_id": active.resource_id})

    store.register_resource(
        resource_id="dataset_source",
        kind="upload",
        content_hash="0" * 64,
        artifact_kind=None,
        job_id=None,
        metadata={},
    )
    store.register_resource(
        resource_id="dataset_child",
        kind="import",
        content_hash="1" * 64,
        artifact_kind="dataset",
        job_id=None,
        metadata={},
        dependencies=({"artifact_id": "dataset_source", "role": "source"},),
    )
    with pytest.raises(RevisionConflict, match="dataset_child"):
        store.delete_resource("dataset_source")


def test_coordinator_spawn_completion_running_cancel_worker_loss_and_lock(tmp_path) -> None:
    store = JobStore(tmp_path)
    complete_job, _ = store.submit(
        kind="test_probe", operation="complete", payload={"steps": 3, "delay_s": 0.001}
    )
    with LocalCoordinator(tmp_path, max_workers=2) as coordinator:
        with pytest.raises(CoordinatorAlreadyRunning):
            LocalCoordinator(tmp_path).acquire()
        assert coordinator.run_once()
        assert store.get_job(complete_job.job_id).status == "completed"

        crash, _ = store.submit(
            kind="test_probe", operation="crash", payload={"steps": 1, "crash": True}
        )
        assert coordinator.run_once()
        failed = store.get_job(crash.job_id)
        assert failed.status == "failed" and failed.error_code == "worker_lost"
        assert not (tmp_path / "exports" / crash.resource_id).exists()

        after_crash, _ = store.submit(
            kind="test_probe", operation="after-crash", payload={"steps": 1}
        )
        assert coordinator.run_once()
        assert store.get_job(after_crash.job_id).status == "completed"


def test_restarted_coordinator_recovers_lease_that_expires_after_startup(tmp_path) -> None:
    store = JobStore(tmp_path)
    active, _ = store.submit(kind="test_probe", operation="orphaned", payload={"steps": 1})
    claimed, token = store.claim_next("dead-owner", 0.05)
    assert claimed.job_id == active.job_id
    store.mark_running(active.job_id, token)

    with LocalCoordinator(
        tmp_path,
        max_workers=1,
        lease_seconds=0.1,
        poll_interval_s=0.01,
    ) as restarted:
        assert store.get_job(active.job_id).status == "running"
        time.sleep(0.06)
        assert not restarted.run_once()

    recovered = store.get_job(active.job_id)
    assert recovered.status == "failed"
    assert recovered.error_code == "worker_lost"
    assert not (tmp_path / "exports" / active.resource_id).exists()
