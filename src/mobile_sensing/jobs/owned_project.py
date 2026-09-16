"""Readable indexes for actual project-owned inputs, environment and results."""

import json

from mobile_sensing.jobs.project_files import write_json, write_text
from mobile_sensing.jobs.project_layout import LAYOUT, project_directory


def sync_owned_project(store, record, *, deleted_at=None):
    directory = project_directory(store.artifact_root)
    with store._connect() as connection:
        revisions = connection.execute(
            "SELECT * FROM revisions WHERE project_id=? ORDER BY created_at_utc,revision_id",
            (record.project_id,),
        ).fetchall()
        jobs = connection.execute(
            "SELECT job_id,kind,status,result_json,updated_at_utc FROM jobs WHERE project_id=? ORDER BY created_at_utc,job_id",
            (record.project_id,),
        ).fetchall()
    settings = {}
    for revision in revisions:
        payload = json.loads(revision["payload_json"])
        write_json(
            directory / "settings/revisions" / (revision["revision_id"] + ".json"),
            {
                "revision_id": revision["revision_id"],
                "base_revision_id": revision["base_revision_id"],
                "created_at_utc": revision["created_at_utc"],
                "payload": payload,
            },
        )
        if revision["revision_id"] == record.current_revision_id:
            settings = payload
    runs, analyses = set(settings.get("linked_run_ids", ())), set(
        settings.get("linked_analysis_ids", ())
    )
    states = []
    for job in jobs:
        result = json.loads(job["result_json"]) if job["result_json"] else {}
        if job["status"] == "completed":
            if job["kind"] == "studio_run" and result.get("run_id"):
                runs.add(result["run_id"])
            if job["kind"] == "studio_analysis" and result.get("analysis_id"):
                analyses.add(result["analysis_id"])
        states.append({key: job[key] for key in ("job_id", "kind", "status", "updated_at_utc")})
    with store._connect() as connection:
        deleted = {
            r["run_id"]: dict(r)
            for r in connection.execute(
                "SELECT * FROM deleted_runs WHERE project_id=?", (record.project_id,)
            )
        }
    write_json(directory / ".system/deleted_runs.json", deleted)
    runs.difference_update(deleted)
    status = (
        "running"
        if any(j["status"] not in {"completed", "failed", "cancelled"} for j in states)
        else "results" if runs or analyses else "configured" if settings else "new"
    )
    write_json(directory / "settings/current.json", settings)
    write_json(
        directory / "results/index.json",
        {
            "run_ids": sorted(runs),
            "analysis_ids": sorted(analyses),
            "jobs": states,
            "artifact_root": "../.system",
        },
    )
    write_json(
        directory / "project.json",
        {
            "schema_version": LAYOUT,
            **record.model_dump(mode="json"),
            "status": status,
            "deleted_at_utc": deleted_at,
            "settings": "settings/current.json",
            "results": "results/index.json",
        },
    )
    from mobile_sensing.jobs.project_inventory import write_inventory

    write_inventory(store, directory, record, settings, sorted(runs), sorted(analyses))
    write_text(
        directory / "README.md",
        "# "
        + record.name
        + "\n\nThis folder contains the complete project. Copy the whole folder into another application's project directory to load it independently.\n\n- `data/`: immutable imported source files and metadata.\n- `environment/`: prepared routing network, grid and geometry.\n- `settings/`: current saved configuration and revision history.\n- `results/`: runs, analyses and full sparse scientific records.\n- `exports/`: portable packages and exported outputs.\n- `notes/`: user-managed notes.\n- `.system/`: private database, job state and relative collection aliases within this folder.\n\nUse the app to edit configuration or register changed data as a new input. Retained artifacts are immutable; editing their bytes invalidates checksums.\n",
    )
    return record.model_copy(update={"status": status})
