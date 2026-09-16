"""Recoverable project-scoped run removal with immutable dependency preservation."""

from mobile_sensing.application.run_pipeline import read_named_record
from mobile_sensing.jobs.models import ApiModel
from mobile_sensing.jobs.store import _timestamp


class RunDeletionView(ApiModel):
    run_id: str
    name: str
    dependent_analyses: list[str]
    dependent_runs: list[str]
    deleted_at_utc: str | None = None
    retained_for_recovery: bool = True


def deleted_run_ids(store, project_id):
    with store._connect() as connection:
        return {
            r[0]
            for r in connection.execute(
                "SELECT run_id FROM deleted_runs WHERE project_id=?", (project_id,)
            )
        }


def project_results(store, project_id):
    project = store.get_project(project_id)
    config = (
        store.get_revision(project_id, project.current_revision_id).payload
        if project.current_revision_id
        else {}
    )
    runs, analyses = set(config.get("linked_run_ids", ())), set(
        config.get("linked_analysis_ids", ())
    )
    for job in store.list_jobs(project_id=project_id):
        if job.status == "completed" and job.result:
            if job.kind == "studio_run":
                runs.add(job.result["run_id"])
            elif job.kind == "studio_analysis":
                analyses.add(job.result["analysis_id"])
    runs.update(deleted_run_ids(store, project_id))
    return runs, analyses


def deletion_preview(store, project_id, run_id):
    runs, analyses = project_results(store, project_id)
    if run_id not in runs:
        raise KeyError("Run does not belong to this project")
    root = store.artifact_root
    run = read_named_record(root, run_id, "studio_run")
    dependents = [
        a.name
        for identifier in sorted(analyses)
        if (a := read_named_record(root, identifier, "studio_analysis")).source_run_id == run_id
    ]
    other_runs = [
        r.name
        for identifier in sorted(runs - {run_id})
        if (r := read_named_record(root, identifier, "studio_run")).realization_source_run_id
        == run_id
        or r.simulation == run.simulation
    ]
    with store._connect() as connection:
        row = connection.execute(
            "SELECT deleted_at_utc FROM deleted_runs WHERE project_id=? AND run_id=?",
            (project_id, run_id),
        ).fetchone()
    return RunDeletionView(
        run_id=run_id,
        name=run.name,
        dependent_analyses=dependents,
        dependent_runs=other_runs,
        deleted_at_utc=row[0] if row else None,
    )


def delete_run(store, project_id, run_id):
    preview = deletion_preview(store, project_id, run_id)
    with store._transaction() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO deleted_runs VALUES (?,?,?,?)",
            (project_id, run_id, preview.name, _timestamp()),
        )
    store.get_project(project_id)


def restore_run(store, project_id, run_id):
    deletion_preview(store, project_id, run_id)
    with store._transaction() as connection:
        connection.execute(
            "DELETE FROM deleted_runs WHERE project_id=? AND run_id=?", (project_id, run_id)
        )
    # If a newer draft removed the original link, preserve discoverability on restore.
    project = store.get_project(project_id)
    config = (
        store.get_revision(project_id, project.current_revision_id).payload
        if project.current_revision_id
        else {}
    )
    if run_id not in config.get("linked_run_ids", ()) and not any(
        j.kind == "studio_run" and j.result and j.result.get("run_id") == run_id
        for j in store.list_jobs(project_id=project_id)
    ):
        config["linked_run_ids"] = [*config.get("linked_run_ids", ()), run_id]
        store.create_revision(
            project_id,
            project.current_revision_id,
            config,
            reference_upgrade=bool(config.get("read_only")),
        )
    store.get_project(project_id)
