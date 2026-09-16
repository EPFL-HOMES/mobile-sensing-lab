"""Checksummed portable projects with transitive immutable dependencies."""

import json
import os
import shutil
import tempfile
import zipfile
from pathlib import Path

from mobile_sensing.application.example_build import digest_file
from mobile_sensing.application.project_reports import build_report
from mobile_sensing.contracts import canonical_json_text, stable_id
from mobile_sensing.jobs.project_files import write_json
from mobile_sensing.jobs.project_layout import (
    project_directory,
    ownership_boundary,
    is_managed_alias,
    is_owned_store,
)

COLLECTIONS = (
    "inputs",
    "raw_inputs",
    "raw_gtfs",
    "datasets",
    "environments",
    "simulations",
    "exposures",
    "portfolios",
)
PREFIXES = (
    "input_",
    "dataset_",
    "environment_",
    "simulation_",
    "exposure_",
    "portfolio_",
    "gtfs_source_",
)


def identities(value):
    found = set()
    if isinstance(value, str) and value.startswith(PREFIXES):
        found.add(value)
    elif isinstance(value, dict):
        for key, child in value.items():
            # A past mobility snapshot may retain an unrelated portfolio draft.
            # Actual analysis sources are declared dependencies and source_run_id.
            if key != "portfolio":
                found.update(identities(child))
    elif isinstance(value, (tuple, list)):
        for child in value:
            found.update(identities(child))
    return found


def reusable_cache_files(root, included):
    """Keep only cache entries whose complete immutable outputs travel with the project."""
    root = Path(root)
    for path in (root / "run_cache").glob("*.json"):
        cache = json.loads(path.read_text())
        if all(
            cache.get(key, {}).get("artifact_id") in included
            for key in ("simulation", "resolution")
        ):
            yield path
    for path in (root / "analysis_cache").glob("*.json"):
        value = json.loads(path.read_text())
        candidates = value if isinstance(value, list) else [value]
        if candidates and all(
            candidate.get("reference", {}).get("artifact_id") in included
            for candidate in candidates
        ):
            yield path


def dependency_files(root, snapshot, cancellation):
    root = Path(root)
    pending = identities(snapshot)
    directories = set()
    while pending:
        cancellation.raise_if_cancelled()
        identifier = pending.pop()
        if Path(identifier).name != identifier:
            raise ValueError("Unsafe scientific identity in project")
        matches = [root / c / identifier for c in COLLECTIONS if (root / c / identifier).is_dir()]
        if not matches:
            raise ValueError(f"Project dependency is missing: {identifier}")
        for folder in matches:
            if folder in directories:
                continue
            directories.add(folder)
            manifest = folder / "manifest.json"
            if manifest.is_file():
                value = json.loads(manifest.read_text())
                pending.update(
                    d["artifact_id"]
                    for d in value["dependencies"]
                    if d["artifact_id"].startswith(PREFIXES)
                )
                # Named runs retain authoring inputs and replay source records.
                pending.update(identities(value["scientific_identity"]["resolved_config"]))
    files = {p for folder in directories for p in folder.rglob("*") if p.is_file()}
    included = {d.name for d in directories}
    files.update(reusable_cache_files(root, included))
    for path in files:
        if path.is_symlink() or not path.resolve().is_relative_to(ownership_boundary(root)):
            raise ValueError("Portable projects cannot include external links")
    if len(files) > 50000 or sum(p.stat().st_size for p in files) > 8 * 1024**3:
        raise ValueError("Project exceeds the 50,000-file / 8 GiB portable-package limit")
    return sorted(files)


def project_snapshot(store, project_id):
    project = store.get_project(project_id)
    directory = project_directory(store.artifact_root, project.name)
    results = json.loads(
        (
            directory
            / ("results/index.json" if is_owned_store(store.artifact_root) else "results.json")
        ).read_text()
    )
    with store._connect() as connection:
        deleted = [
            dict(row)
            for row in connection.execute(
                "SELECT run_id,name,deleted_at_utc FROM deleted_runs WHERE project_id=? ORDER BY run_id",
                (project_id,),
            )
        ]
    return {
        "version": "portable-project@1",
        "project": project.model_dump(mode="json"),
        "settings": json.loads(
            (
                directory
                / (
                    "settings/current.json"
                    if is_owned_store(store.artifact_root)
                    else "settings.json"
                )
            ).read_text()
        ),
        "revisions": [r.model_dump(mode="json") for r in store.list_revisions(project_id)],
        "run_ids": results["run_ids"],
        "analysis_ids": results["analysis_ids"],
        "deleted_runs": deleted,
    }


def export_project(root, resource_id, snapshot, cancellation, progress, *, archive=True):
    root = Path(root)
    for kind, values in (
        ("studio_run", snapshot["run_ids"]),
        ("studio_analysis", snapshot["analysis_ids"]),
    ):
        for i, identifier in enumerate(values):
            progress.update(phase="project.summary_files", completed=i, total=len(values))
            build_report(root, identifier, kind, cancellation)
    if not archive:
        return {"project_id": snapshot["project"]["project_id"], "reports_complete": True}
    paths = dependency_files(root, snapshot, cancellation)
    inventory = [
        {"path": str(p.relative_to(root)), "size_bytes": p.stat().st_size, "sha256": digest_file(p)}
        for p in paths
    ]
    notes_directory = project_directory(root, snapshot["project"]["name"]) / "notes"
    notes = sorted(p for p in notes_directory.rglob("*") if p.is_file())
    if any(
        p.is_symlink() or not p.resolve().is_relative_to(notes_directory.resolve()) for p in notes
    ):
        raise ValueError("Project notes cannot contain external links")
    if len(notes) > 5000 or sum(p.stat().st_size for p in notes) > 256 * 1024**2:
        raise ValueError("Project notes exceed the portable 5,000-file / 256 MiB limit")
    manifest = {
        **snapshot,
        "files": inventory,
        "user_files": [
            {
                "path": str(p.relative_to(notes_directory)),
                "size_bytes": p.stat().st_size,
                "sha256": digest_file(p),
            }
            for p in notes
        ],
    }
    package_id = stable_id("export", manifest)
    target = root / "exports" / package_id
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        with tempfile.TemporaryDirectory(dir=target.parent) as temporary:
            stage = Path(temporary)
            with zipfile.ZipFile(
                stage / "project.zip", "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1
            ) as output:
                output.writestr("project.json", canonical_json_text(manifest))
                for path in notes:
                    cancellation.raise_if_cancelled()
                    output.write(path, "notes/" + str(path.relative_to(notes_directory)))
                for i, path in enumerate(paths):
                    with (
                        path.open("rb") as source,
                        output.open(
                            "workspace/" + str(path.relative_to(root)), "w", force_zip64=True
                        ) as destination,
                    ):
                        for chunk in iter(lambda: source.read(1024**2), b""):
                            cancellation.raise_if_cancelled()
                            destination.write(chunk)
                    progress.update(phase="project.archive", completed=i + 1, total=len(paths))
            metadata = {
                "sha256": digest_file(stage / "project.zip"),
                "format": "zip",
                "file": "project.zip",
            }
            write_json(stage / "export.json", metadata)
            os.replace(stage, target)
    metadata = json.loads((target / "export.json").read_text())
    project_folder = project_directory(root, snapshot["project"]["name"])
    export_index = project_folder / "exports" / "packages.json"
    names = json.loads(export_index.read_text()) if export_index.exists() else {}
    if package_id not in names:
        names[package_id] = f"project-{len(names)+1:03d}.zip"
    destination = project_folder / "exports" / names[package_id]
    if not destination.exists():
        shutil.copyfile(target / "project.zip", destination)
    write_json(export_index, names)
    return {
        "export": metadata,
        "download_path": str((target / "project.zip").relative_to(root)),
        "requested_resource_id": snapshot["project"]["project_id"],
    }


def read_package_header(path):
    try:
        with zipfile.ZipFile(path) as archive:
            info = archive.getinfo("project.json")
            if info.file_size > 16 * 1024**2:
                raise ValueError("Portable project metadata exceeds 16 MiB")
            value = json.loads(archive.read(info))
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError(
            "Select an exported portable project ZIP with valid project metadata"
        ) from error
    if not isinstance(value, dict) or value.get("version") != "portable-project@1":
        raise ValueError("Select an exported portable project ZIP")
    return value


def import_project(root, payload, cancellation, progress):
    root = Path(root)
    path = (root / payload["upload_path"]).resolve()
    if not path.is_relative_to(root / ".project_uploads"):
        raise ValueError("Project archive must be a managed upload")
    header = read_package_header(path)
    user_files = {row["path"]: row for row in header.get("user_files", [])}
    if (
        len(user_files) != len(header.get("user_files", []))
        or len(user_files) > 5000
        or sum(row["size_bytes"] for row in user_files.values()) > 256 * 1024**2
    ):
        raise ValueError("Invalid project notes inventory")
    for name in user_files:
        if (
            Path(name).is_absolute()
            or ".." in Path(name).parts
            or "\\" in name
            or not Path(name).parts
        ):
            raise ValueError("Unsafe project notes path")
    inventory = {row["path"]: row for row in header["files"]}
    if (
        len(inventory) != len(header["files"])
        or len(inventory) > 50000
        or sum(r["size_bytes"] for r in inventory.values()) > 8 * 1024**3
    ):
        raise ValueError("Invalid or oversized portable-project inventory")
    for name in inventory:
        parts = Path(name).parts
        if (
            not parts
            or parts[0] not in {*COLLECTIONS, "run_cache", "analysis_cache"}
            or Path(name).is_absolute()
            or any(p in {"..", "."} for p in parts)
            or "\\" in name
        ):
            raise ValueError("Unsafe portable-project path")
    staging = root / ".project_imports"
    staging.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=staging) as temporary, zipfile.ZipFile(path) as archive:
        stage = Path(temporary)
        expected = {
            "project.json",
            *("workspace/" + name for name in inventory),
            *("notes/" + name for name in user_files),
        }
        if set(archive.namelist()) != expected or len(archive.namelist()) != len(expected):
            raise ValueError("Portable project archive does not match its exact inventory")
        for index, (name, row) in enumerate(inventory.items()):
            info = archive.getinfo("workspace/" + name)
            if (
                info.file_size != row["size_bytes"]
                or (info.external_attr >> 16) & 0o170000 == 0o120000
            ):
                raise ValueError("Portable project file type or size mismatch")
            output = stage / name
            output.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, output.open("wb") as destination:
                size = 0
                for chunk in iter(lambda: source.read(1024**2), b""):
                    cancellation.raise_if_cancelled()
                    size += len(chunk)
                    if size > row["size_bytes"]:
                        raise ValueError("Archive member exceeds declared size")
                    destination.write(chunk)
            if digest_file(output) != row["sha256"]:
                raise ValueError("Portable project checksum mismatch")
            progress.update(
                phase="project.verify_and_import", completed=index + 1, total=len(inventory)
            )
        for name, row in inventory.items():
            destination = root / name
            if destination.exists() and (
                destination.is_symlink() or digest_file(destination) != row["sha256"]
            ):
                raise ValueError("Existing shared data conflicts with the portable project")
            if any(
                parent.is_symlink() and not is_managed_alias(parent, root)
                for parent in destination.parents
                if parent.is_relative_to(root)
            ):
                raise ValueError("Portable project destination cannot traverse symbolic links")
        for name, row in user_files.items():
            info = archive.getinfo("notes/" + name)
            if (
                info.file_size != row["size_bytes"]
                or (info.external_attr >> 16) & 0o170000 == 0o120000
            ):
                raise ValueError("Project notes file type or size mismatch")
            output = stage / "notes" / name
            output.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, output.open("wb") as destination:
                shutil.copyfileobj(source, destination, 1024**2)
            if digest_file(output) != row["sha256"]:
                raise ValueError("Project notes checksum mismatch")
        # Publish whole immutable directories after every member verifies.
        for parts in sorted({Path(name).parts[:2] for name in inventory}):
            cancellation.raise_if_cancelled()
            relative = Path(*parts)
            destination = root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists():
                os.replace(stage / relative, destination)
        for manifest in root.glob("*/*/manifest.json"):
            if str(manifest.relative_to(root)) in inventory:
                for table in json.loads(manifest.read_text()).get("tables", []):
                    relative = Path(table["relative_path"])
                    if relative.parts[0] == "tables" and ".." not in relative.parts:
                        (manifest.parent / relative).mkdir(parents=True, exist_ok=True)
        from mobile_sensing.jobs.project_names import normalize_name

        notes_target = project_directory(root, normalize_name(payload["project_name"])) / "notes"
        for name in user_files:
            cancellation.raise_if_cancelled()
            target = notes_target / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                os.replace(stage / "notes" / name, target)
    for kind, values in (
        ("studio_run", header["run_ids"]),
        ("studio_analysis", header["analysis_ids"]),
    ):
        for identifier in values:
            build_report(root, identifier, kind, cancellation)
    return {"imported_project": header, "project_id": payload["project_id"]}


def finalize_import(store, project_id, result):
    from mobile_sensing.application.migration import migrate_project
    from mobile_sensing.datasets.inputs import load_input
    from mobile_sensing.jobs.project_inventory import selected_inputs

    header = result["imported_project"]
    if not header["settings"]:
        if (
            header["revisions"]
            or header["run_ids"]
            or header["analysis_ids"]
            or header.get("deleted_runs")
        ):
            raise ValueError("A project with retained history must include saved settings")
        # A new, unsaved project is portable without inventing a configuration revision.
        return
    config = migrate_project(header["settings"], None).config.model_copy(
        update={
            "linked_run_ids": tuple(header["run_ids"]),
            "linked_analysis_ids": tuple(header["analysis_ids"]),
            "read_only": False,
        }
    )
    for identifier in selected_inputs(header):
        metadata, _ = load_input(store.artifact_root, identifier)
        store.register_resource(
            resource_id=identifier,
            kind="input",
            content_hash=metadata["content_hash"],
            artifact_kind=None,
            job_id=None,
            metadata=metadata,
        )
    project = store.get_project(project_id)
    write_json(
        project_directory(store.artifact_root, project.name)
        / ("settings/revisions" if is_owned_store(store.artifact_root) else "revisions")
        / "imported-history.json",
        header["revisions"],
    )
    if project.current_revision_id is None:
        from datetime import datetime
        from mobile_sensing.application.run_pipeline import read_named_record

        deleted = []
        for entry in header.get("deleted_runs", ()):
            run = read_named_record(store.artifact_root, entry["run_id"], "studio_run")
            datetime.fromisoformat(entry["deleted_at_utc"])
            deleted.append((project_id, run.run_id, run.name, entry["deleted_at_utc"]))
        with store._transaction() as connection:
            connection.executemany("INSERT OR IGNORE INTO deleted_runs VALUES (?,?,?,?)", deleted)
        store.create_revision(project_id, None, config.model_dump(mode="json"))
