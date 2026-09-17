"""Logical removal never changes a retained scientific source or resurrects from history."""

from fastapi.testclient import TestClient

from mobile_sensing.api.workspace import create_workspace_app
from mobile_sensing.application.run_pipeline import run_analysis, read_named_record
from mobile_sensing.application.run_models import RunOptions
from mobile_sensing.application.project_models import PortfolioEditor, PortfolioFleetEditor
from mobile_sensing.jobs.workspace import migrate_shared_workspace, ProjectWorkspace
from tests.jobs.test_owned_workspace import populated_flat_project
from tests.environment.test_environment_editor import Cancellation
from tests.support.environment_fixtures import RecordedProgress
from mobile_sensing.jobs import JobStore


def test_delete_refresh_copy_restore_and_analysis_dependencies(tmp_path):
    root, project, config, run = populated_flat_project(tmp_path)
    analysis = run_analysis(
        root,
        PortfolioEditor(
            source_run_id=run.run_id,
            sampling_runs=3,
            fleets=(PortfolioFleetEditor(fleet_id="f", counts=(0, 1, 2)),),
            budgets=(0, 1, 2),
        ),
        name="Retained comparison",
        source_revision_id=None,
        options=RunOptions(),
        cancellation=Cancellation(),
        progress=RecordedProgress(),
    )
    store = JobStore(root)
    current = store.get_project(project.project_id)
    config = config.model_copy(update={"linked_analysis_ids": (analysis.analysis_id,)})
    store.create_revision(
        project.project_id, current.current_revision_id, config.model_dump(mode="json")
    )
    migrate_shared_workspace(root)
    client = TestClient(create_workspace_app(root))
    suffix = f"?project_id={project.project_id}"
    preview = client.get(f"/api/v1/workbench/runs/{run.run_id}/deletion-preview{suffix}")
    assert preview.status_code == 200 and preview.json()["dependent_analyses"] == [
        "Retained comparison"
    ]
    assert client.delete(f"/api/v1/workbench/runs/{run.run_id}{suffix}").status_code == 204
    assert client.get(f"/api/v1/workbench/runs{suffix}").json() == []
    client = TestClient(create_workspace_app(root))
    assert client.get(f"/api/v1/workbench/runs{suffix}").json() == []
    assert len(client.get(f"/api/v1/workbench/analyses{suffix}").json()) == 1
    private = ProjectWorkspace(root).store(project.project_id).artifact_root
    assert (
        read_named_record(private, analysis.analysis_id, "studio_analysis").frontier
        == analysis.frontier
    )
    assert read_named_record(private, run.run_id, "studio_run").exposure == run.exposure
    # Saving an older configuration with the original links cannot undo deletion.
    store = JobStore(private)
    current = store.get_project(project.project_id)
    store.create_revision(
        project.project_id, current.current_revision_id, config.model_dump(mode="json")
    )
    assert client.get(f"/api/v1/workbench/runs{suffix}").json() == []
    copied = ProjectWorkspace(root).copy(project.project_id)
    assert client.get(f"/api/v1/workbench/runs?project_id={copied.project_id}").json() == []
    removed = client.get(f"/api/v1/workbench/deleted-runs{suffix}").json()
    assert removed[0]["name"] == run.name and removed[0]["deleted_at_utc"]
    assert client.post(f"/api/v1/workbench/runs/{run.run_id}/restore{suffix}").status_code == 204
    assert client.get(f"/api/v1/workbench/runs{suffix}").json()[0]["run_id"] == run.run_id
    assert client.get(f"/api/v1/workbench/runs?project_id={copied.project_id}").json() == []
    other = ProjectWorkspace(root).create("Other", "")
    assert (
        client.delete(
            f"/api/v1/workbench/runs/{run.run_id}?project_id={other.project_id}"
        ).status_code
        == 404
    )
