"""Portable unique display names, independent of stable project identity."""

import re
import unicodedata
from pathlib import Path

RESERVED = {
    "projects",
    "inputs",
    "raw_inputs",
    "raw_gtfs",
    "datasets",
    "environments",
    "environment_sources",
    "simulations",
    "exposures",
    "portfolios",
    "exports",
    "osm_cache",
    "run_cache",
    "example_evidence",
    "example_installations",
}


def normalize_name(name):
    value = unicodedata.normalize("NFKC", name).strip()
    if (
        not value
        or value.startswith(".")
        or value.endswith(".")
        or len(value.encode("utf-8")) > 200
        or re.search(r'[<>:"/\\|?*\x00-\x1f]', value)
        or value.casefold() in RESERVED
        or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?", value)
    ):
        raise ValueError(
            "Use a project name of at most 200 UTF-8 bytes without filesystem-reserved characters or names"
        )
    return value


def name_key(name):
    return normalize_name(name).casefold()


def unused_name(base, keys, *, suffix=""):
    base = normalize_name(base)
    for index in range(1, 100000):
        tail = suffix + (f" {index}" if index > 1 else "")
        head = base
        while len((head + tail).encode("utf-8")) > 200:
            head = head[:-1]
        candidate = normalize_name(head + tail)
        if name_key(candidate) not in keys:
            return candidate
    raise ValueError("No unused project name is available")


def migrate_names(store, connection):
    connection.execute(
        "CREATE TABLE IF NOT EXISTS project_names (project_id TEXT PRIMARY KEY REFERENCES projects(project_id), name_key TEXT NOT NULL UNIQUE, directory TEXT NOT NULL)"
    )
    rows = connection.execute(
        "SELECT * FROM projects WHERE project_id NOT IN (SELECT project_id FROM deleted_projects) ORDER BY created_at_utc, project_id"
    ).fetchall()
    keys = {r[0] for r in connection.execute("SELECT name_key FROM project_names")}
    receipts = []
    for row in rows:
        if connection.execute(
            "SELECT 1 FROM project_names WHERE project_id=?", (row["project_id"],)
        ).fetchone():
            continue
        try:
            base = normalize_name(row["name"])
        except ValueError:
            base = (
                re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", row["name"]).strip(" .")[:80]
                or "Imported project"
            )
            if base.casefold() in RESERVED:
                base += " project"
        name = unused_name(base, keys)
        while (store.artifact_root / name).exists():
            existing = store.artifact_root / name / "project.json"
            import json

            if (
                existing.is_file()
                and json.loads(existing.read_text()).get("project_id") == row["project_id"]
            ):
                break
            keys.add(name_key(name))
            name = unused_name(base, keys)
        keys.add(name_key(name))
        connection.execute(
            "UPDATE projects SET name=? WHERE project_id=?", (name, row["project_id"])
        )
        connection.execute(
            "INSERT INTO project_names VALUES (?,?,?)",
            (row["project_id"], name_key(name), str(Path("projects") / row["project_id"])),
        )
        if name != row["name"]:
            receipts.append(
                {"project_id": row["project_id"], "previous_name": row["name"], "name": name}
            )
    if receipts:
        from mobile_sensing.jobs.project_files import write_json

        write_json(store.artifact_root / ".system" / "name-migration.json", receipts)
