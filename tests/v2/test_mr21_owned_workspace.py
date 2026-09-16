"""Project-owned byte storage, independent API scopes and portable migration."""

import json
import shutil
from pathlib import Path

from fastapi.testclient import TestClient

from mobile_sensing.api.workspace import create_workspace_app
from mobile_sensing.jobs import JobStore
from mobile_sensing.jobs.workspace import ProjectWorkspace, migrate_shared_workspace
from mobile_sensing.application.project_package import (
    export_project,
    import_project,
    project_snapshot,
    finalize_import,
)
from tests.v2.test_mr05_runs_analysis import config_fixture
from tests.v2.test_mr02_environment_editor import Cancellation
from tests.v2.environment_fixtures import RecordedProgress


def populated_flat_project(tmp_path):
    from mobile_sensing.application.run_pipeline import run_project
    from mobile_sensing.application.run_models import RunOptions

    root, config = config_fixture(tmp_path)
    run = run_project(
        root,
        config,
        name="First day",
        source_revision_id=None,
        options=RunOptions(),
        cancellation=Cancellation(),
        progress=RecordedProgress(),
    )
    config = config.model_copy(update={"linked_run_ids": (run.run_id,)})
    store = JobStore(root)
    project = store.create_project("Research day", "Fixture")
    store.create_revision(project.project_id, None, config.model_dump(mode="json"))
    return root, project, config, run


def test_workspace_scopes_inputs_names_and_private_storage(tmp_path):
    root = tmp_path / "project"
    client = TestClient(create_workspace_app(root))
    one = client.post("/api/v1/projects", json={"name": "One", "description": ""}).json()
    two = client.post("/api/v1/projects", json={"name": "Two", "description": ""}).json()
    assert (
        client.post("/api/v1/projects", json={"name": "one", "description": ""}).status_code == 409
    )
    file = tmp_path / "values.csv"
    file.write_text("longitude,latitude,value\n6.6,46.5,2\n")
    first = {"X-Project-ID": one["project_id"]}
    second = {"X-Project-ID": two["project_id"]}
    response = client.post(
        "/api/v1/inputs/register",
        headers=first,
        json={
            "path": str(file),
            "name": "Spatial values",
            "role": "feature",
            "source_crs": "EPSG:4326",
        },
    )
    assert response.status_code == 201, response.text
    assert len(client.get("/api/v1/inputs", headers=first).json()) == 1
    assert client.get("/api/v1/inputs", headers=second).json() == []
    assert client.get("/api/v1/inputs").status_code == 422
    assert {p.name for p in root.iterdir()} == {"One", "Two"}
    assert (root / "One/.system/metadata.sqlite").is_file()
    assert len(list((root / "One/data/inputs").glob("*/input.json"))) == 1
    assert (
        client.patch(
            f"/api/v1/projects/{one['project_id']}", json={"name": "Renamed", "description": ""}
        ).status_code
        == 200
    )
    assert not (root / "One").exists()
    assert len(client.get("/api/v1/inputs", headers=first).json()) == 1
    assert client.delete(f"/api/v1/projects/{two['project_id']}").status_code == 204
    assert {p.name for p in root.iterdir()} == {"Renamed"}
    assert list((tmp_path / ".project-workspace/trash").glob("Two-*"))


def test_migration_copy_and_folder_move_are_independent(tmp_path):
    from mobile_sensing.application.run_pipeline import read_named_record

    root, project, config, run = populated_flat_project(tmp_path)
    receipt = migrate_shared_workspace(root)
    assert Path(receipt["backup_directory"]).is_dir()
    workspace = ProjectWorkspace(root)
    copied = workspace.copy(project.project_id, config.model_dump(mode="json"))
    assert copied.project_id != project.project_id
    assert copied.name == "Research day copy"
    one, two = workspace.store(project.project_id), workspace.store(copied.project_id)
    original = one.artifact_root / "simulations" / run.simulation.artifact_id / "manifest.json"
    duplicate = two.artifact_root / "simulations" / run.simulation.artifact_id / "manifest.json"
    assert original.read_bytes() == duplicate.read_bytes()
    assert original.stat().st_ino != duplicate.stat().st_ino
    inventory = json.loads((two.artifact_root.parent / "files.json").read_text())
    assert inventory["portable"] and all(not f["shared"] for f in inventory["files"])
    moved = tmp_path / "fresh-projects" / copied.name
    shutil.copytree(two.artifact_root.parent, moved, symlinks=True)
    source_hidden = root.with_name("source-hidden")
    root.rename(source_hidden)
    fresh = ProjectWorkspace(moved.parent).store(copied.project_id)
    assert (
        read_named_record(fresh.artifact_root, run.run_id, "studio_run").simulation
        == run.simulation
    )
    assert (moved / "settings/current.json").is_file()
    assert (moved / "results/index.json").is_file()
    assert (moved / "environment/prepared").is_dir()


def test_owned_export_import_retains_dependency_bytes(tmp_path):
    root, project, config, run = populated_flat_project(tmp_path)
    migrate_shared_workspace(root)
    store = ProjectWorkspace(root).store(project.project_id)
    result = export_project(
        store.artifact_root,
        "export-job",
        project_snapshot(store, project.project_id),
        Cancellation(),
        RecordedProgress(),
    )
    archive = store.artifact_root / result["download_path"]
    destination = ProjectWorkspace(tmp_path / "imported-projects")
    imported = destination.create("Imported", "")
    target = destination.store(imported.project_id).artifact_root
    upload = target / ".project_uploads/input.zip"
    upload.parent.mkdir()
    shutil.copyfile(archive, upload)
    value = import_project(
        target,
        {
            "upload_path": ".project_uploads/input.zip",
            "project_id": imported.project_id,
            "project_name": imported.name,
        },
        Cancellation(),
        RecordedProgress(),
    )
    finalize_import(destination.store(imported.project_id), imported.project_id, value)
    from mobile_sensing.application.run_pipeline import read_named_record

    assert read_named_record(target, run.run_id, "studio_run").exposure == run.exposure


def test_one_coordinator_services_two_owned_queues_and_keeps_fencing(tmp_path):
    from mobile_sensing.jobs.workspace_coordinator import WorkspaceCoordinator
    from mobile_sensing.jobs.coordinator import CoordinatorAlreadyRunning
    import pytest

    workspace = ProjectWorkspace(tmp_path / "projects")
    a, b = workspace.create("A", ""), workspace.create("B", "")
    left, right = workspace.store(a.project_id), workspace.store(b.project_id)
    j1, _ = left.submit(
        kind="test_probe", operation="probe@1", payload={"steps": 1}, project_id=a.project_id
    )
    j2, _ = right.submit(
        kind="test_probe", operation="probe@1", payload={"steps": 2}, project_id=b.project_id
    )
    with WorkspaceCoordinator(workspace.root) as coordinator:
        with pytest.raises(CoordinatorAlreadyRunning):
            with WorkspaceCoordinator(workspace.root):
                pass
        assert coordinator.run_once()
        assert left.get_job(j1.job_id).status == "completed"
        assert right.get_job(j2.job_id).status == "queued"
        assert coordinator.run_once()
        assert right.get_job(j2.job_id).status == "completed"
        assert not coordinator.run_once()
    with WorkspaceCoordinator(workspace.root):
        pass


def test_migration_does_not_move_an_active_flat_workspace(tmp_path):
    from mobile_sensing.jobs import LocalCoordinator
    from mobile_sensing.jobs.coordinator import CoordinatorAlreadyRunning
    import pytest

    with LocalCoordinator(tmp_path):
        with pytest.raises(CoordinatorAlreadyRunning):
            migrate_shared_workspace(tmp_path)
    assert (tmp_path / "metadata.sqlite").is_file()


def test_hub_example_install_initialization_and_editable_copy(tmp_path, monkeypatch):
    import zipfile
    from mobile_sensing.application.project_package import dependency_files
    from mobile_sensing.application.example_bundle import ExampleBundle, BundleFile
    from mobile_sensing.application.example_build import digest_file
    from mobile_sensing.jobs.workspace_coordinator import WorkspaceCoordinator

    root, project, config, run = populated_flat_project(tmp_path / "source")
    files = dependency_files(
        root, project_snapshot(JobStore(root), project.project_id), Cancellation()
    )
    bundle = ExampleBundle(
        bundle_id="example_owned_fixture",
        name="Owned example",
        config=config,
        files=tuple(
            BundleFile(
                path=str(p.relative_to(root)), sha256=digest_file(p), size_bytes=p.stat().st_size
            )
            for p in files
        ),
        saved_views={"Fleet results": "/results?view=fleet"},
        description="Fixture",
    )
    folder = tmp_path / "bundle"
    folder.mkdir()
    (folder / "lausanne.json").write_text(bundle.model_dump_json())
    with zipfile.ZipFile(folder / "lausanne.zip", "w") as archive:
        for path in files:
            archive.write(path, str(path.relative_to(root)))
    monkeypatch.setenv("MOBILE_SENSING_EXAMPLE_DIRECTORY", str(folder))
    destination = tmp_path / "projects"
    client = TestClient(create_workspace_app(destination))
    assert client.get("/api/v1/examples/lausanne").status_code == 200
    started = client.post("/api/v1/workbench/workspace/initialize")
    assert started.status_code == 200, started.text
    job = started.json()["example_job_id"]
    with WorkspaceCoordinator(destination) as coordinator:
        assert coordinator.run_once()
    completed = client.post("/api/v1/workbench/workspace/initialize")
    assert completed.status_code == 200, completed.text
    assert completed.json()["example_job_id"] is None
    assert completed.json()["directory"] == str(destination)
    copied = client.post("/api/v1/examples/lausanne/open", json={"editable": True})
    assert copied.status_code == 200, copied.text
    assert len(client.get("/api/v1/projects").json()) == 2
    assert {p.name for p in destination.iterdir()} == {"Owned example", "Owned example copy"}
    store = ProjectWorkspace(destination).store(copied.json()["project_id"])
    current = store.get_project(copied.json()["project_id"])
    assert not store.get_revision(current.project_id, current.current_revision_id).payload[
        "read_only"
    ]
    assert store.get_job(job).status == "completed"


def test_missing_example_bundle_keeps_empty_workspace_usable(tmp_path, monkeypatch):
    monkeypatch.setenv("MOBILE_SENSING_EXAMPLE_DIRECTORY", str(tmp_path / "absent"))
    root = tmp_path / "projects"
    client = TestClient(create_workspace_app(root))
    assert not client.get("/api/v1/examples/lausanne").json()["available"]
    initialized = client.post("/api/v1/workbench/workspace/initialize")
    assert initialized.status_code == 200
    assert initialized.json()["example_project_id"] is None
    assert client.post("/api/v1/examples/lausanne/open", json={}).status_code == 404
    assert list(root.iterdir()) == []
    assert (
        client.post("/api/v1/projects", json={"name": "Research", "description": ""}).status_code
        == 201
    )


def test_concurrent_project_names_are_reserved_once(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from mobile_sensing.jobs import RevisionConflict

    first = ProjectWorkspace(tmp_path / "projects")
    second = ProjectWorkspace(first.root)
    ready = Barrier(2)

    def create(workspace):
        ready.wait()
        try:
            workspace.create("Same name", "")
            return "created"
        except RevisionConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(create, (first, second))) == ["conflict", "created"]
    assert len(first.records()) == 1


def test_copy_ignores_unrelated_draft_but_requires_declared_sources(tmp_path):
    import pytest
    from mobile_sensing.application.project_package import dependency_files

    root, project, config, run = populated_flat_project(tmp_path)
    snapshot = project_snapshot(JobStore(root), project.project_id)
    snapshot["settings"]["portfolio"]["source_run_id"] = "dataset_unbundled_draft"
    paths = dependency_files(root, snapshot, Cancellation())
    assert any(run.run_id in path.parts for path in paths)
    snapshot["analysis_source"] = {"source_run_id": "dataset_missing_required_source"}
    with pytest.raises(ValueError, match="dataset_missing_required_source"):
        dependency_files(root, snapshot, Cancellation())


def test_workspace_documents_delegated_scientific_contracts(tmp_path):
    client = TestClient(create_workspace_app(tmp_path))
    schema = client.get("/openapi.json").json()
    assert "/api/v1/simulations" in schema["paths"]
    assert "/api/v1/workbench/runs/{run_id}/restore" in schema["paths"]
    assert "/api/v1/projects/{project_id}/files" in schema["paths"]


def test_invalid_project_upload_releases_its_unpublished_folder(tmp_path):
    client = TestClient(create_workspace_app(tmp_path))
    response = client.post(
        "/api/v1/project-imports",
        data={"name": "Retry import"},
        files={"file": ("invalid.zip", b"not a zip", "application/zip")},
    )
    assert response.status_code == 422
    assert not (tmp_path / "Retry import").exists()
    assert (
        client.post(
            "/api/v1/projects", json={"name": "Retry import", "description": ""}
        ).status_code
        == 201
    )


def test_portable_zip_retains_deleted_run_recovery(tmp_path):
    from mobile_sensing.application.run_management import delete_run, deleted_run_ids, restore_run

    root, project, config, run = populated_flat_project(tmp_path / "source")
    source = JobStore(root)
    delete_run(source, project.project_id, run.run_id)
    snapshot = project_snapshot(source, project.project_id)
    assert snapshot["run_ids"] == []
    assert snapshot["deleted_runs"][0]["run_id"] == run.run_id
    exported = export_project(
        root, "recoverable-export", snapshot, Cancellation(), RecordedProgress()
    )
    workspace = ProjectWorkspace(tmp_path / "target")
    copied = workspace.create("Recovered import", "")
    target = workspace.store(copied.project_id)
    upload = target.artifact_root / ".project_uploads/input.zip"
    upload.parent.mkdir()
    shutil.copyfile(root / exported["download_path"], upload)
    result = import_project(
        target.artifact_root,
        {
            "upload_path": ".project_uploads/input.zip",
            "project_id": copied.project_id,
            "project_name": copied.name,
        },
        Cancellation(),
        RecordedProgress(),
    )
    finalize_import(target, copied.project_id, result)
    assert deleted_run_ids(target, copied.project_id) == {run.run_id}
    assert (
        run.run_id
        not in json.loads((target.artifact_root.parent / "results/index.json").read_text())[
            "run_ids"
        ]
    )
    restore_run(target, copied.project_id, run.run_id)
    assert not deleted_run_ids(target, copied.project_id)
    assert (
        run.run_id
        in json.loads((target.artifact_root.parent / "results/index.json").read_text())["run_ids"]
    )
