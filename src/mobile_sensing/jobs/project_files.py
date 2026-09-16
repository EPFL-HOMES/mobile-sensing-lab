"""Atomic, portable project indexes over the transactional workspace metadata."""

import json
import os
from pathlib import Path
import uuid
import html
from urllib.parse import quote

from mobile_sensing.contracts import canonical_json_text


def write_json(path, value):
    path = Path(path)
    content = (
        json.dumps(json.loads(canonical_json_text(value)), indent=2, ensure_ascii=False) + "\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("Project index paths must not be symbolic links")
    if path.exists() and path.read_text() == content:
        return
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def sync_project(store, record, *, deleted_at=None):
    from mobile_sensing.jobs.project_layout import is_owned_store

    if is_owned_store(store.artifact_root):
        from mobile_sensing.jobs.owned_project import sync_owned_project

        return sync_owned_project(store, record, deleted_at=deleted_at)
    identifier = record.project_id
    if Path(identifier).name != identifier or not identifier.startswith("project_"):
        raise ValueError("Invalid project directory identifier")
    with store._transaction() as connection:
        row = connection.execute(
            "SELECT p.name,n.directory FROM projects p JOIN project_names n USING(project_id) WHERE project_id=?",
            (identifier,),
        ).fetchone()
        if row is None:
            raise ValueError("Project has no active directory registration")
        record = record.model_copy(update={"name": row["name"]})
        directory = store.artifact_root / (
            str(Path(".trash") / identifier) if deleted_at else record.name
        )
        previous = store.artifact_root / row["directory"]
        if previous != directory and previous.exists():
            if previous.is_symlink() or directory.is_symlink():
                raise ValueError("Project directories must not be symbolic links")
            directory.parent.mkdir(parents=True, exist_ok=True)
            if directory.exists() and not previous.samefile(directory):
                raise ValueError("Project directory destination already exists")
            os.replace(previous, directory)
        connection.execute(
            "UPDATE project_names SET directory=? WHERE project_id=?",
            (str(directory.relative_to(store.artifact_root)), identifier),
        )
    if (
        directory.is_symlink()
        or (directory / "revisions").is_symlink()
        or (store.artifact_root / "projects").is_symlink()
    ):
        raise ValueError("Project directories must not be symbolic links")
    with store._connect() as connection:
        revisions = connection.execute(
            "SELECT * FROM revisions WHERE project_id=? ORDER BY created_at_utc, revision_id",
            (identifier,),
        ).fetchall()
        jobs = connection.execute(
            "SELECT job_id,kind,status,result_json,updated_at_utc FROM jobs WHERE project_id=? ORDER BY created_at_utc,job_id",
            (identifier,),
        ).fetchall()
    settings = {}
    for revision in revisions:
        payload = json.loads(revision["payload_json"])
        write_json(
            directory / "revisions" / (revision["revision_id"] + ".json"),
            {
                "revision_id": revision["revision_id"],
                "base_revision_id": revision["base_revision_id"],
                "created_at_utc": revision["created_at_utc"],
                "payload": payload,
            },
        )
        if revision["revision_id"] == record.current_revision_id:
            settings = payload
    runs = set(settings.get("linked_run_ids", []))
    analyses = set(settings.get("linked_analysis_ids", []))
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
        runs.difference_update(
            r[0]
            for r in connection.execute(
                "SELECT run_id FROM deleted_runs WHERE project_id=?", (identifier,)
            )
        )
    running = any(row["status"] not in ("completed", "failed", "cancelled") for row in states)
    status = (
        "running"
        if running
        else "results" if runs or analyses else "configured" if settings else "new"
    )
    write_json(directory / "settings.json", settings)
    write_json(
        directory / "results.json",
        {
            "run_ids": sorted(runs),
            "analysis_ids": sorted(analyses),
            "artifact_root": os.path.relpath(store.artifact_root, directory),
            "jobs": states,
        },
    )
    write_json(
        directory / "project.json",
        {
            "schema_version": "project-folder@2",
            **record.model_dump(mode="json"),
            "status": status,
            "deleted_at_utc": deleted_at,
            "settings": "settings.json",
            "results": "results.json",
        },
    )
    for name in ("inputs", "environment", "runs", "analyses", "exports", "notes"):
        (directory / name).mkdir(exist_ok=True)
    # A lightweight inventory never scans scientific result rows in an API request.
    from mobile_sensing.jobs.project_inventory import write_inventory

    write_inventory(store, directory, record, settings, sorted(runs), sorted(analyses))
    return record.model_copy(update={"status": status})


def write_text(path, value):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Managed project files must not be symbolic links")
    if path.is_file() and path.read_text() == value:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(value)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def html_index(name, files):
    rows = "".join(
        f'<tr><td>{html.escape(row["category"])}</td><td><a href="{quote(row["path"], safe="/.")}">{html.escape(row["name"])}</a></td><td>{row["size_bytes"]:,}</td></tr>'
        for row in files
    )
    return f"""<!doctype html><html lang="en"><meta charset="utf-8"><title>{html.escape(name)}</title>
<style>body{{font:15px system-ui;color:#26384b;max-width:1100px;margin:48px auto;padding:0 24px}}table{{border-collapse:collapse;width:100%}}td,th{{padding:10px;text-align:left;border-bottom:1px solid #dce4ec}}a{{color:#087f8c}}p{{color:#627487}}</style>
<h1>{html.escape(name)}</h1><p>Saved settings, inputs and results. Scientific files are immutable. The file inventory records their exact local locations.</p>
<table><thead><tr><th>Category</th><th>File</th><th>Bytes</th></tr></thead><tbody>{rows}</tbody></table></html>"""
