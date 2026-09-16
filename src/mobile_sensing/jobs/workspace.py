"""Project ownership, verified folder copies and migration from shared workspaces."""

import json
import os
import shutil
import sqlite3
import uuid
import threading
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

from mobile_sensing.contracts import stable_id
from mobile_sensing.jobs import JobStore, RevisionConflict
from mobile_sensing.jobs.project_layout import prepare_project_directory, project_directory, LAYOUT
from mobile_sensing.jobs.project_names import normalize_name, name_key, unused_name
from mobile_sensing.jobs.project_files import write_json

_MUTATION_LOCKS = {}
_LOCK_REGISTRY = threading.Lock()


def serialized_mutation(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self.mutation():
            return method(self, *args, **kwargs)

    return wrapped


class NeverCancelled:
    def raise_if_cancelled(self):
        pass


def copy_file_checked(source, destination):
    from mobile_sensing.application.example_build import digest_file

    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    if source.stat().st_size != destination.stat().st_size or digest_file(source) != digest_file(
        destination
    ):
        raise ValueError(f"Project copy checksum mismatch: {source.name}")


def clone_project(source_store, project_id, destination, *, new_identity=False, draft=None):
    """Copy the complete dependency closure before publishing the new folder."""
    from mobile_sensing.application.project_package import project_snapshot, dependency_files
    from mobile_sensing.application.project_models import ProjectConfig

    original = source_store.get_project(project_id)
    if any(
        j.status not in {"completed", "failed", "cancelled"}
        for j in source_store.list_jobs(project_id=project_id)
    ):
        raise RevisionConflict("Finish or cancel project jobs before copying or migrating")
    snapshot = project_snapshot(source_store, project_id)
    if draft is not None:
        snapshot["settings"] = draft
    snapshot["jobs"] = [
        j.model_dump(mode="json") for j in source_store.list_jobs(project_id=project_id)
    ]
    # Previously global registered inputs remain available in migrated projects.
    snapshot["registered_inputs"] = [
        r.resource_id for r in source_store.list_resources(kind="input")
    ]
    paths = dependency_files(source_store.artifact_root, snapshot, NeverCancelled())
    destination = Path(destination)
    target = prepare_project_directory(destination)
    for source in paths:
        copy_file_checked(source, target / source.relative_to(source_store.artifact_root))
    # Preserve empty manifest-declared table directories as well as file bytes.
    for collection in ("datasets", "environments", "simulations", "exposures", "portfolios"):
        for manifest in (target / collection).glob("*/manifest.json"):
            for table in json.loads(manifest.read_text()).get("tables", ()):
                relative = Path(table["relative_path"])
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError("Unsafe table path in copied project")
                if relative.parts[0] == "tables":
                    (manifest.parent / relative).mkdir(parents=True, exist_ok=True)
    with source_store._connect() as source, sqlite3.connect(target / "metadata.sqlite") as database:
        source.backup(database)
    with sqlite3.connect(target / "metadata.sqlite") as database:
        database.execute("PRAGMA foreign_keys=OFF")
        database.execute(
            "DELETE FROM jobs WHERE project_id IS NULL OR project_id != ?", (project_id,)
        )
        database.execute("DELETE FROM job_events WHERE job_id NOT IN (SELECT job_id FROM jobs)")
        database.execute(
            "DELETE FROM idempotency_keys WHERE job_id NOT IN (SELECT job_id FROM jobs)"
        )
        database.execute("DELETE FROM revisions WHERE project_id != ?", (project_id,))
        database.execute("DELETE FROM deleted_projects")
        database.execute("DELETE FROM deleted_runs WHERE project_id != ?", (project_id,))
        database.execute("DELETE FROM project_names WHERE project_id != ?", (project_id,))
        database.execute("DELETE FROM projects WHERE project_id != ?", (project_id,))
        for identifier, kind, job_id, metadata in database.execute(
            "SELECT resource_id,kind,job_id,metadata_json FROM resources"
        ).fetchall():
            value = json.loads(metadata)
            linked = value.get("artifact", {}).get("artifact_id", identifier)
            present = any(
                (target / c / linked).exists()
                for c in (
                    "inputs",
                    "raw_inputs",
                    "raw_gtfs",
                    "datasets",
                    "environments",
                    "simulations",
                    "exposures",
                    "portfolios",
                )
            )
            owned_job = database.execute("SELECT 1 FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if not present and not owned_job:
                database.execute(
                    "DELETE FROM resource_dependencies WHERE resource_id=?", (identifier,)
                )
                database.execute("DELETE FROM resources WHERE resource_id=?", (identifier,))
        if new_identity:
            replacement = stable_id("project", {"nonce": uuid.uuid4().hex})
            for table in ("projects", "project_names", "revisions", "jobs", "deleted_runs"):
                database.execute(
                    f"UPDATE {table} SET project_id=? WHERE project_id=?", (replacement, project_id)
                )
            database.execute("DELETE FROM idempotency_keys")
            project_id = replacement
        database.execute(
            "UPDATE projects SET name=? WHERE project_id=?", (destination.name, project_id)
        )
        database.execute(
            "UPDATE project_names SET name_key=?,directory=? WHERE project_id=?",
            (name_key(destination.name), destination.name, project_id),
        )
    old_directory = project_directory(source_store.artifact_root, original.name)
    for name in ("notes", "exports"):
        source = old_directory / name
        if source.is_dir():
            for path in source.rglob("*"):
                if path.is_file():
                    copy_file_checked(path, destination / name / path.relative_to(source))
    source_reports = source_store.artifact_root / ".system/reports"
    if source_reports.is_dir():
        for identifier in snapshot["run_ids"] + snapshot["analysis_ids"]:
            source = source_reports / identifier
            if source.is_dir():
                shutil.copytree(source, target / ".system/reports" / identifier, dirs_exist_ok=True)
    copied = JobStore(target)
    record = copied.get_project(project_id)
    if new_identity and record.current_revision_id:
        config = ProjectConfig.model_validate_json(json.dumps(snapshot["settings"]))
        config = config.model_copy(
            update={
                "linked_run_ids": tuple(snapshot["run_ids"]),
                "linked_analysis_ids": tuple(snapshot["analysis_ids"]),
                "source_revision": original.current_revision_id,
                "read_only": False,
            }
        )
        with copied._transaction() as connection:
            connection.execute(
                "UPDATE projects SET current_revision_id=NULL WHERE project_id=?", (project_id,)
            )
        copied.create_revision(project_id, None, config.model_dump(mode="json"))
    write_json(
        target / "copy-receipt.json",
        {
            "source_project_id": original.project_id,
            "project_id": project_id,
            "verified_files": len(paths),
            "source_directory": str(old_directory),
            "independent": True,
        },
    )
    return copied.get_project(project_id)


class ProjectWorkspace:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        with _LOCK_REGISTRY:
            self._mutation_lock = _MUTATION_LOCKS.setdefault(str(self.root), threading.RLock())
        self._projects = {}
        self.refresh()
        for folder, _ in self._projects.values():
            prepare_project_directory(folder)

    @contextmanager
    def mutation(self):
        """Serialize folder name reservation/publication across API threads and processes."""
        import fcntl

        control = self.root.parent / ("." + self.root.name + "-workspace")
        control.mkdir(exist_ok=True)
        with self._mutation_lock, (control / "projects.lock").open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    @serialized_mutation
    def reserve_directory(self, name):
        name = self.available_name(name)
        return prepare_project_directory(self.root / name)

    def refresh(self):
        found = {}
        keys = set()
        for folder in sorted(self.root.iterdir()):
            marker = folder / "project.json"
            if folder.is_symlink() or not marker.is_file():
                continue
            value = json.loads(marker.read_text())
            if value.get("schema_version") != LAYOUT or value.get("deleted_at_utc"):
                continue
            name = normalize_name(folder.name)
            if name_key(name) in keys:
                raise RevisionConflict("Project folder names must be unique ignoring case")
            keys.add(name_key(name))
            identifier = value["project_id"]
            private = folder / ".system"
            if identifier in found:
                raise RevisionConflict(
                    "Two folders contain the same project identity; use Create copy in the app before loading both"
                )
            found[identifier] = (folder, private)
        self._projects = found
        return found

    def store(self, identifier):
        self.refresh()
        if identifier not in self._projects:
            raise KeyError(identifier)
        folder, root = self._projects[identifier]
        if not root.is_dir():
            raise ValueError("Project private metadata is missing")
        return JobStore(root)

    def records(self):
        self.refresh()
        return [
            JobStore(root).get_project(identifier)
            for identifier, (_, root) in self._projects.items()
        ]

    def available_name(self, name, *, suffix=None, exclude=None):
        name = normalize_name(name)
        self.refresh()
        keys = {
            name_key(folder.name)
            for identifier, (folder, _) in self._projects.items()
            if identifier != exclude
        }
        if suffix is not None:
            name = unused_name(name, keys, suffix=suffix)
        if name_key(name) in keys or (
            (self.root / name).exists()
            and self._projects.get(exclude, (None,))[0] != self.root / name
        ):
            raise RevisionConflict("A project with this name already exists")
        return name

    @serialized_mutation
    def create(self, name, description):
        name = self.available_name(name)
        private = prepare_project_directory(self.root / name)
        return JobStore(private).create_project(name, description)

    @serialized_mutation
    def copy(self, identifier, draft=None):
        source = self.store(identifier)
        record = source.get_project(identifier)
        name = self.available_name(record.name, suffix=" copy")
        staging = (
            self.root.parent
            / ("." + self.root.name + "-workspace")
            / "copy-staging"
            / uuid.uuid4().hex
        )
        staging.mkdir(parents=True)
        record = clone_project(source, identifier, staging / name, new_identity=True, draft=draft)
        os.replace(staging / name, self.root / name)
        staging.rmdir()
        return record

    @serialized_mutation
    def rename(self, identifier, name, description):
        name = self.available_name(name, exclude=identifier)
        store = self.store(identifier)
        previous = store.artifact_root.parent
        record = store.rename_project(identifier, name, description)
        target = self.root / name
        if target != previous:
            os.replace(previous, target)
        self.refresh()
        return record

    @serialized_mutation
    def delete(self, identifier):
        store = self.store(identifier)
        store.delete_project(identifier)
        self.refresh()


def migrate_shared_workspace(root):
    """Startup migration is staged and keeps the full previous workspace as a backup."""
    root = Path(root).resolve()
    if not (root / "metadata.sqlite").is_file():
        return None
    import fcntl
    from mobile_sensing.jobs.coordinator import CoordinatorAlreadyRunning

    lock = (root / ".coordinator.lock").open("a+")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        lock.close()
        raise CoordinatorAlreadyRunning(
            "CoordinatorAlreadyRunning: stop the old workspace service before migration"
        ) from error
    source = JobStore(root)
    projects = source.list_projects()
    stage = root.with_name("." + root.name + "-migration-" + uuid.uuid4().hex[:10])
    stage.mkdir()
    try:
        for project in projects:
            clone_project(source, project.project_id, stage / project.name)
        backup = root.with_name("." + root.name + "-before-ownership-" + uuid.uuid4().hex[:10])
        os.replace(root, backup)
        os.replace(stage, root)
        receipt = {
            "backup_directory": str(backup),
            "project_ids": [p.project_id for p in projects],
            "verified": True,
        }
        for project in projects:
            write_json(root / project.name / ".system/migration-receipt.json", receipt)
        return receipt
    except BaseException:
        # Failed stages retain their evidence; the original workspace is untouched until publication.
        raise
    finally:
        lock.close()
