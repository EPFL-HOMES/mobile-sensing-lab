import json
import shutil
import sqlite3
import zipfile

import pytest
from fastapi.testclient import TestClient

from mobile_sensing.api import create_app
from mobile_sensing.application.project_package import import_project
from mobile_sensing.application.run_pipeline import run_project, run_analysis, read_named_record
from mobile_sensing.application.project_models import PortfolioEditor, PortfolioFleetEditor
from mobile_sensing.application.run_models import RunOptions
from mobile_sensing.jobs import JobStore, LocalCoordinator
from tests.support.environment_fixtures import RecordedProgress
from tests.environment.test_environment_editor import Cancellation
from tests.application.test_runs_analysis import config_fixture


def test_names_are_unique_portable_and_rename_preserves_notes_and_identity(tmp_path):
    client = TestClient(create_app(tmp_path))
    first = client.post("/api/v1/projects", json={"name": "  Café  "}).json()
    identifier = first["project_id"]
    assert first["name"] == "Café"
    assert (tmp_path / "Café" / "settings.json").is_file()
    assert not (tmp_path / "projects" / identifier).exists()
    assert client.post("/api/v1/projects", json={"name": "CAFE\u0301"}).status_code == 409
    for bad in ("../outside", ".system", "inputs", "bad/name", "CON"):
        assert client.post("/api/v1/projects", json={"name": bad}).status_code == 422
    (tmp_path / "Café" / "notes" / "experiment.txt").write_text("User-owned notes")
    renamed = client.patch(f"/api/v1/projects/{identifier}", json={"name": "Study A"}).json()
    assert renamed["project_id"] == identifier
    assert (tmp_path / "Study A" / "notes" / "experiment.txt").read_text() == "User-owned notes"
    assert not (tmp_path / "Café").exists()
    assert client.delete(f"/api/v1/projects/{identifier}").status_code == 204
    assert (tmp_path / ".trash" / identifier / "notes" / "experiment.txt").is_file()
    assert client.post("/api/v1/projects", json={"name": "Study A"}).status_code == 201


def test_legacy_duplicate_names_migrate_without_merging(tmp_path):
    store = JobStore(tmp_path)
    first = store.create_project("Original", "")
    second = store.create_project("Temporary", "")
    with sqlite3.connect(store.path) as connection:
        connection.execute("DROP TABLE project_names")
        connection.execute("UPDATE projects SET name='Original'")
    (tmp_path / "projects").mkdir()
    for record, folder in ((first, "Original"), (second, "Temporary")):
        shutil.move(tmp_path / folder, tmp_path / "projects" / record.project_id)
    migrated = JobStore(tmp_path).list_projects()
    assert [p.name for p in migrated] == ["Original", "Original 2"]
    assert [p.project_id for p in migrated] == [first.project_id, second.project_id]
    assert (
        json.loads((tmp_path / ".system" / "name-migration.json").read_text())[0]["previous_name"]
        == "Original"
    )


def test_portable_project_roundtrip_reuses_mobility_in_fresh_workspace(tmp_path, monkeypatch):
    root, config = config_fixture(tmp_path / "source")
    kwargs = dict(options=RunOptions(), cancellation=Cancellation(), progress=RecordedProgress())
    run = run_project(
        root, config, name="Recorded run", source_revision_id="source-revision", **kwargs
    )
    editor = PortfolioEditor(
        source_run_id=run.run_id,
        sampling_runs=3,
        fleets=(PortfolioFleetEditor(fleet_id="f", counts=(0, 1, 2), unit_cost=1),),
        budgets=(0, 1, 2),
    )
    analysis = run_analysis(
        root, editor, name="Recorded analysis", source_revision_id="source-revision", **kwargs
    )
    client = TestClient(create_app(root))
    project = client.post("/api/v1/projects", json={"name": "Portable study"}).json()
    config = config.model_copy(
        update={
            "linked_run_ids": (run.run_id,),
            "linked_analysis_ids": (analysis.analysis_id,),
            "portfolio": editor,
        }
    )
    saved = client.post(
        f"/api/v1/projects/{project['project_id']}/revisions",
        json={"payload": config.model_dump(mode="json")},
    )
    assert saved.status_code == 201, saved.text
    (root / "Portable study" / "notes" / "experiment.txt").write_text("Independent notes")
    package = client.post(
        f"/api/v1/projects/{project['project_id']}/package", json={"archive": True}
    )
    with LocalCoordinator(root, max_workers=1) as coordinator:
        coordinator.run_once()
    job = JobStore(root).get_job(package.json()["job_id"])
    assert job.status == "completed", job.error_message
    files = client.get(f"/api/v1/projects/{project['project_id']}/files").json()["files"]
    assert any("fleet_summary.csv" in f["name"] and not f["shared"] for f in files)
    assert any("mean_sensing.geoparquet" in f["name"] for f in files)
    assert any(f["category"] == "Complete records" for f in files)
    data = client.get(f"/api/v1/artifacts/{job.resource_id}/download")
    assert data.status_code == 200 and data.headers["content-type"] == "application/zip"
    destination = tmp_path / "fresh-workspace"
    reopened = TestClient(create_app(destination))
    submission = reopened.post(
        "/api/v1/project-imports",
        data={"name": "Imported study"},
        files={"file": ("project.zip", data.content, "application/zip")},
    )
    assert submission.status_code == 202, submission.text
    with LocalCoordinator(destination, max_workers=1) as coordinator:
        coordinator.run_once()
    imported = JobStore(destination).get_job(submission.json()["job_id"])
    assert imported.status == "completed", imported.error_message
    result = read_named_record(destination, run.run_id, "studio_run")
    assert result.simulation == run.simulation and result.config == run.config
    projects = reopened.get("/api/v1/projects").json()
    assert len(projects) == 1 and projects[0]["name"] == "Imported study"
    assert (
        destination / "Imported study" / "notes" / "experiment.txt"
    ).read_text() == "Independent notes"
    assert (destination / "Imported study" / "runs" / "run-001" / "fleet_summary.csv").exists()
    inputs = reopened.get("/api/v1/inputs").json()
    assert inputs

    def forbidden(*args, **kwargs):
        raise AssertionError("Portable rebin invoked mobility")

    from mobile_sensing.application import HeadlessApplication

    monkeypatch.setattr(HeadlessApplication, "run_simulation", forbidden)
    changed = config.model_copy(
        update={
            "simulation": config.simulation.model_copy(update={"temporal_resolution_minutes": 60.0})
        }
    )
    rebin = run_project(destination, changed, name="Hourly", source_revision_id=None, **kwargs)
    assert rebin.mobility_reused and rebin.simulation == run.simulation


def test_unsaved_project_portable_round_trip(tmp_path):
    client = TestClient(create_app(tmp_path))
    project = client.post("/api/v1/projects", json={"name": "New study"}).json()
    (tmp_path / "New study" / "notes" / "plan.txt").write_text("Future experiment")
    submitted = client.post(
        f"/api/v1/projects/{project['project_id']}/package", json={"archive": True}
    ).json()
    with LocalCoordinator(tmp_path, max_workers=1) as coordinator:
        coordinator.run_once()
    job = JobStore(tmp_path).get_job(submitted["job_id"])
    assert job.status == "completed", job.error_message
    archive = client.get(f"/api/v1/artifacts/{job.resource_id}/download")
    imported = client.post(
        "/api/v1/project-imports",
        data={"name": "New study imported"},
        files={"file": ("project.zip", archive.content, "application/zip")},
    ).json()
    with LocalCoordinator(tmp_path, max_workers=1) as coordinator:
        coordinator.run_once()
    job = JobStore(tmp_path).get_job(imported["job_id"])
    assert job.status == "completed", job.error_message
    restored = JobStore(tmp_path).get_project(job.project_id)
    assert restored.current_revision_id is None
    assert (tmp_path / restored.name / "notes" / "plan.txt").read_text() == "Future experiment"


def test_package_rejects_traversal_before_publication(tmp_path):
    folder = tmp_path / ".project_uploads"
    folder.mkdir()
    header = {
        "version": "portable-project@1",
        "files": [{"path": "../outside", "size_bytes": 1, "sha256": "0" * 64}],
    }
    with zipfile.ZipFile(folder / "bad.zip", "w") as archive:
        archive.writestr("project.json", json.dumps(header))
        archive.writestr("workspace/../outside", "x")
    with pytest.raises(ValueError, match="Unsafe"):
        import_project(
            tmp_path,
            {"upload_path": ".project_uploads/bad.zip"},
            Cancellation(),
            RecordedProgress(),
        )
    assert not (tmp_path.parent / "outside").exists()
