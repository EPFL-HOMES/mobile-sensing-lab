import json
import shutil

from fastapi.testclient import TestClient

from mobile_sensing.api import create_app
from mobile_sensing.jobs import JobStore


def test_project_files_follow_revisions_copy_delete_and_workspace_move(tmp_path):
    root = tmp_path / "project"
    client = TestClient(create_app(root))
    project = client.post("/api/v1/projects", json={"name": "Stored project"}).json()
    identifier = project["project_id"]
    defaults = client.get("/api/v1/workbench/project/defaults").json()
    saved = client.post(
        f"/api/v1/projects/{identifier}/revisions", json={"payload": defaults}
    ).json()
    directory = root / "Stored project"
    assert json.loads((directory / "settings.json").read_text()) == defaults
    assert (directory / "revisions" / (saved["revision_id"] + ".json")).is_file()
    index = json.loads((directory / "results.json").read_text())
    assert index["artifact_root"] == ".."
    assert not index["run_ids"]
    copied = client.post(
        f"/api/v1/workbench/projects/{identifier}/copy", json={"config": defaults}
    ).json()
    assert copied["project_id"] != identifier
    assert client.delete(f"/api/v1/projects/{identifier}").status_code == 204
    assert json.loads((root / ".trash" / identifier / "project.json").read_text())["deleted_at_utc"]
    moved = tmp_path / "moved"
    shutil.copytree(root, moved)
    reopened = TestClient(create_app(moved))
    assert [p["project_id"] for p in reopened.get("/api/v1/projects").json()] == [
        copied["project_id"]
    ]
    assert reopened.get("/api/v1/workbench/workspace").json()["directory"] == str(moved)


def test_completed_result_is_indexed_without_browser_refresh(tmp_path):
    store = JobStore(tmp_path)
    project = store.create_project("Results", "")
    job, _ = store.submit(
        kind="studio_run", operation="test", payload={}, project_id=project.project_id
    )
    _, token = store.claim_next("owner", 60)
    store.mark_running(job.job_id, token)
    store.mark_finalizing(job.job_id, token)
    store.complete(job.job_id, token, {"run_id": "dataset_retained_run"})
    index = tmp_path / project.name / "results.json"
    assert json.loads(index.read_text())["run_ids"] == ["dataset_retained_run"]
    assert store.get_project(project.project_id).status == "results"


def test_example_is_an_ordinary_project_and_deletion_survives_initialization(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from mobile_sensing.api import workbench
    from mobile_sensing.application.project_models import ProjectConfig

    bundle = SimpleNamespace(
        config=ProjectConfig(),
        bundle_id="example_test",
        saved_views={},
        name="Example project",
        description="Bundled",
    )
    monkeypatch.setattr(workbench, "bundle_manifest", lambda: bundle)
    monkeypatch.setattr(workbench, "bundle_installed", lambda *args: True)
    client = TestClient(create_app(tmp_path))
    first = client.post("/api/v1/workbench/workspace/initialize").json()
    second = client.post("/api/v1/workbench/workspace/initialize").json()
    assert first == second
    projects = client.get("/api/v1/projects").json()
    assert len(projects) == 1 and projects[0]["name"] == "Example project"
    assert client.delete(f"/api/v1/projects/{projects[0]['project_id']}").status_code == 204
    client.post("/api/v1/workbench/workspace/initialize")
    assert client.get("/api/v1/projects").json() == []


def test_new_example_distribution_recovers_an_old_cancelled_upgrade(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from mobile_sensing.api import workbench
    from mobile_sensing.application.project_models import ProjectConfig

    bundle = SimpleNamespace(
        config=ProjectConfig(),
        bundle_id="example_old",
        saved_views={},
        name="Example project",
        description="Bundled",
    )
    monkeypatch.setattr(workbench, "bundle_manifest", lambda: bundle)
    monkeypatch.setattr(workbench, "bundle_installed", lambda *args: True)
    client = TestClient(create_app(tmp_path))
    first = client.post("/api/v1/workbench/workspace/initialize").json()
    store = JobStore(tmp_path)
    project = store.get_project(first["example_project_id"])
    old_revision = project.current_revision_id
    job, _ = store.submit(
        kind="example_import",
        operation="example-import@1",
        payload={"bundle_id": bundle.bundle_id, "editable": False},
        project_id=project.project_id,
    )
    store.request_cancel(job.job_id)
    (tmp_path / "workspace.json").write_text(
        json.dumps({"example_project_id": project.project_id, "example_job_id": job.job_id})
    )
    unchanged = client.post("/api/v1/workbench/workspace/initialize").json()
    assert unchanged["example_job_id"] == job.job_id
    bundle.bundle_id = "example_new"
    recovered = client.post("/api/v1/workbench/workspace/initialize").json()
    assert recovered["example_job_id"] is None
    assert recovered["example_project_id"] == project.project_id
    assert len(store.list_projects()) == 1
    assert len(store.list_revisions(project.project_id)) == 2
    assert (
        store.get_revision(project.project_id, old_revision).payload["example_bundle_id"]
        == "example_old"
    )
