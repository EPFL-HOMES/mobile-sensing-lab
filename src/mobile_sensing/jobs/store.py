"""Short-transaction SQLite store for M09 metadata, leases, and durable events."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from mobile_sensing.contracts import canonical_json_text, scientific_hash, stable_id
from mobile_sensing.jobs.models import (
    JobEvent,
    JobKind,
    JobSnapshot,
    JobStoreLimits,
    ProjectRecord,
    ResourceRecord,
    RevisionRecord,
    TERMINAL_STATUSES,
)


SCHEMA_VERSION = 1


class IdempotencyConflict(ValueError):
    pass


class RevisionConflict(ValueError):
    pass


class LeaseFenceError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime | None = None) -> str:
    return (value or _now()).isoformat().replace("+00:00", "Z")


class JobStore:
    """Own only bounded metadata; analytical rows stay in immutable artifacts."""

    def __init__(self, artifact_root: str | Path, limits: JobStoreLimits | None = None) -> None:
        self.artifact_root = Path(artifact_root).resolve()
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self.path = self.artifact_root / "metadata.sqlite"
        self.limits = limits or JobStoreLimits()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata_version (
                    version INTEGER PRIMARY KEY
                );
                CREATE TABLE IF NOT EXISTS projects (
                    project_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL,
                    current_revision_id TEXT,
                    created_at_utc TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS revisions (
                    revision_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(project_id),
                    base_revision_id TEXT,
                    payload_json TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deleted_projects (
                    project_id TEXT PRIMARY KEY REFERENCES projects(project_id),
                    deleted_at_utc TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deleted_runs (
                    project_id TEXT NOT NULL REFERENCES projects(project_id),
                    run_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    deleted_at_utc TEXT NOT NULL,
                    PRIMARY KEY(project_id, run_id)
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    project_id TEXT REFERENCES projects(project_id),
                    resource_id TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    cancel_requested INTEGER NOT NULL,
                    counters_json TEXT NOT NULL,
                    result_json TEXT,
                    error_code TEXT,
                    error_message TEXT,
                    cache_source_job_id TEXT,
                    attempt_token TEXT,
                    lease_owner TEXT,
                    lease_expires_at_utc TEXT,
                    created_at_utc TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS jobs_queue
                    ON jobs(status, created_at_utc, job_id);
                CREATE INDEX IF NOT EXISTS jobs_fingerprint
                    ON jobs(kind, request_hash, status);
                CREATE TABLE IF NOT EXISTS idempotency_keys (
                    scope TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    job_id TEXT NOT NULL REFERENCES jobs(job_id),
                    PRIMARY KEY(scope, operation, idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS job_events (
                    job_id TEXT NOT NULL REFERENCES jobs(job_id),
                    event_id INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    timestamp_utc TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY(job_id, event_id)
                );
                CREATE TABLE IF NOT EXISTS resources (
                    resource_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    artifact_kind TEXT,
                    job_id TEXT,
                    metadata_json TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS resource_dependencies (
                    resource_id TEXT NOT NULL REFERENCES resources(resource_id),
                    depends_on_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    PRIMARY KEY(resource_id, depends_on_id, role)
                );
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO metadata_version(version) VALUES (?)", (SCHEMA_VERSION,)
            )
            versions = connection.execute("SELECT version FROM metadata_version").fetchall()
            if [row[0] for row in versions] != [SCHEMA_VERSION]:
                raise RuntimeError("unsupported metadata schema version")
        from mobile_sensing.jobs.project_names import migrate_names

        with self._transaction() as connection:
            migrate_names(self, connection)

    @staticmethod
    def _decode_job(row: sqlite3.Row) -> JobSnapshot:
        return JobSnapshot(
            job_id=row["job_id"],
            kind=row["kind"],
            project_id=row["project_id"],
            resource_id=row["resource_id"],
            request_hash=row["request_hash"],
            status=row["status"],
            phase=row["phase"],
            attempt=row["attempt"],
            cancel_requested=bool(row["cancel_requested"]),
            counters=json.loads(row["counters_json"]),
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            error_code=row["error_code"],
            error_message=row["error_message"],
            cache_source_job_id=row["cache_source_job_id"],
            created_at_utc=row["created_at_utc"],
            updated_at_utc=row["updated_at_utc"],
        )

    def _append_event(
        self,
        connection: sqlite3.Connection,
        job_id: str,
        event_type: str,
        phase: str,
        payload: dict[str, Any],
    ) -> int:
        payload_json = canonical_json_text(payload)
        if len(payload_json.encode("utf-8")) > self.limits.max_event_payload_bytes:
            raise ValueError("job event payload exceeds max_event_payload_bytes")
        event_id = connection.execute(
            "SELECT COALESCE(MAX(event_id), 0) + 1 FROM job_events WHERE job_id=?", (job_id,)
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO job_events VALUES (?, ?, ?, ?, ?, ?)",
            (job_id, event_id, event_type, _timestamp(), phase, payload_json),
        )
        cutoff = event_id - self.limits.max_events_per_job
        if cutoff > 0:
            connection.execute(
                "DELETE FROM job_events WHERE job_id=? AND event_id<=?", (job_id, cutoff)
            )
        return event_id

    def _check_project_name(self, connection, name, project_id=None):
        from mobile_sensing.jobs.project_names import name_key

        existing = connection.execute(
            "SELECT project_id FROM project_names WHERE name_key=?", (name_key(name),)
        ).fetchone()
        if existing and existing[0] != project_id:
            raise RevisionConflict("A project with this name already exists. Choose a unique name.")
        target = self.artifact_root / name
        if target.exists():
            marker = target / "project.json"
            if (
                target.is_symlink()
                or not marker.is_file()
                or json.loads(marker.read_text()).get("project_id") != project_id
            ):
                raise RevisionConflict("This name is already used by a workspace file or directory")

    def create_project(
        self, name: str, description: str, *, unique_suffix: str | None = None
    ) -> ProjectRecord:
        from mobile_sensing.jobs.project_names import normalize_name, name_key, unused_name

        name = normalize_name(name)
        created = _timestamp()
        project_id = stable_id("project", {"nonce": uuid.uuid4().hex})
        with self._transaction() as connection:
            if unique_suffix is not None:
                keys = {r[0] for r in connection.execute("SELECT name_key FROM project_names")}
                name = unused_name(name, keys, suffix=unique_suffix)
            self._check_project_name(connection, name)
            connection.execute(
                "INSERT INTO projects VALUES (?, ?, ?, NULL, ?, ?)",
                (project_id, name, description, created, created),
            )
            connection.execute(
                "INSERT INTO project_names VALUES (?,?,?)", (project_id, name_key(name), name)
            )
        return self.get_project(project_id)

    def rename_project(self, project_id: str, name: str, description: str) -> ProjectRecord:
        from mobile_sensing.jobs.project_names import normalize_name, name_key

        name = normalize_name(name)
        self.get_project(project_id)
        with self._transaction() as connection:
            self._check_project_name(connection, name, project_id)
            if connection.execute(
                "SELECT 1 FROM jobs WHERE project_id=? AND status NOT IN ('completed','failed','cancelled')",
                (project_id,),
            ).fetchone():
                raise RevisionConflict("Finish or cancel active project jobs before renaming")
            connection.execute(
                "UPDATE projects SET name=?,description=?,updated_at_utc=? WHERE project_id=?",
                (name, description, _timestamp(), project_id),
            )
            connection.execute(
                "UPDATE project_names SET name_key=? WHERE project_id=?",
                (name_key(name), project_id),
            )
        return self.get_project(project_id)

    def list_projects(self) -> list[ProjectRecord]:
        from mobile_sensing.jobs.project_files import sync_project

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM projects WHERE project_id NOT IN (SELECT project_id FROM deleted_projects) ORDER BY created_at_utc, project_id"
            )
            records = [ProjectRecord.model_validate(dict(row)) for row in rows]
        return [sync_project(self, record) for record in records]

    def get_project(self, project_id: str) -> ProjectRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM projects WHERE project_id=? AND project_id NOT IN (SELECT project_id FROM deleted_projects)",
                (project_id,),
            ).fetchone()
        if row is None:
            raise KeyError(project_id)
        from mobile_sensing.jobs.project_files import sync_project

        return sync_project(self, ProjectRecord.model_validate(dict(row)))

    def delete_project(self, project_id: str) -> None:
        record = self.get_project(project_id)
        deleted_at = _timestamp()
        with self._transaction() as connection:
            active = connection.execute(
                "SELECT 1 FROM jobs WHERE project_id=? AND status NOT IN ('completed','failed','cancelled') LIMIT 1",
                (project_id,),
            ).fetchone()
            if active:
                raise RevisionConflict("Cancel or finish active jobs before deleting this project")
            connection.execute(
                "INSERT INTO deleted_projects VALUES (?, ?)", (project_id, deleted_at)
            )
        from mobile_sensing.jobs.project_files import sync_project

        sync_project(self, record, deleted_at=deleted_at)
        with self._transaction() as connection:
            connection.execute("DELETE FROM project_names WHERE project_id=?", (project_id,))

    def create_revision(
        self,
        project_id: str,
        base_revision_id: str | None,
        payload: dict[str, Any],
        *,
        reference_upgrade: bool = False,
    ) -> RevisionRecord:
        self.get_project(project_id)
        payload_json = canonical_json_text(payload)
        revision_id = stable_id(
            "revision",
            {
                "project_id": project_id,
                "base_revision_id": base_revision_id,
                "payload_hash": scientific_hash(payload),
            },
        )
        created = _timestamp()
        with self._transaction() as connection:
            project = connection.execute(
                "SELECT current_revision_id FROM projects WHERE project_id=?", (project_id,)
            ).fetchone()
            if project is None:
                raise KeyError(project_id)
            if project["current_revision_id"] != base_revision_id:
                raise RevisionConflict("base_revision_id differs from the current revision")
            if base_revision_id is not None:
                previous = connection.execute(
                    "SELECT payload_json FROM revisions WHERE revision_id=?", (base_revision_id,)
                ).fetchone()
                if (
                    previous
                    and json.loads(previous["payload_json"]).get("read_only")
                    and not (
                        reference_upgrade
                        and payload.get("read_only")
                        and payload.get("example_bundle_id")
                        and json.loads(previous["payload_json"]).get("example_bundle_id")
                    )
                ):
                    raise RevisionConflict(
                        "Create an editable copy of the reference example before changing its configuration"
                    )
            connection.execute(
                "INSERT OR IGNORE INTO revisions VALUES (?, ?, ?, ?, ?)",
                (revision_id, project_id, base_revision_id, payload_json, created),
            )
            connection.execute(
                "UPDATE projects SET current_revision_id=?, updated_at_utc=? WHERE project_id=?",
                (revision_id, created, project_id),
            )
        self.get_project(project_id)
        return RevisionRecord(
            project_id=project_id,
            revision_id=revision_id,
            base_revision_id=base_revision_id,
            payload=payload,
            created_at_utc=created,
        )

    def list_revisions(self, project_id: str) -> list[RevisionRecord]:
        self.get_project(project_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM revisions WHERE project_id=? "
                "ORDER BY created_at_utc, revision_id",
                (project_id,),
            ).fetchall()
        return [self._decode_revision(row) for row in rows]

    def get_revision(self, project_id: str, revision_id: str) -> RevisionRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM revisions WHERE project_id=? AND revision_id=?",
                (project_id, revision_id),
            ).fetchone()
        if row is None:
            raise KeyError(revision_id)
        return self._decode_revision(row)

    @staticmethod
    def _decode_revision(row: sqlite3.Row) -> RevisionRecord:
        value = dict(row)
        value["payload"] = json.loads(value.pop("payload_json"))
        return RevisionRecord.model_validate(value)

    def submit(
        self,
        *,
        kind: JobKind,
        operation: str,
        payload: dict[str, Any],
        project_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[JobSnapshot, bool]:
        from mobile_sensing.jobs.project_layout import is_owned_store

        if project_id is None and is_owned_store(self.artifact_root):
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT project_id FROM projects WHERE project_id NOT IN (SELECT project_id FROM deleted_projects)"
                ).fetchall()
            if len(rows) != 1:
                raise ValueError("An owned project store requires exactly one active project")
            project_id = rows[0][0]
        if project_id is not None:
            self.get_project(project_id)
        payload_json = canonical_json_text(payload)
        if len(payload_json.encode("utf-8")) > self.limits.max_job_payload_bytes:
            raise ValueError("job payload exceeds max_job_payload_bytes")
        request_hash = scientific_hash({"operation": operation, "payload": payload})
        scope = project_id or "_global"
        created = _timestamp()
        with self._transaction() as connection:
            if (
                project_id is not None
                and connection.execute(
                    "SELECT 1 FROM projects WHERE project_id=?", (project_id,)
                ).fetchone()
                is None
            ):
                raise KeyError(project_id)
            if idempotency_key:
                existing = connection.execute(
                    "SELECT request_hash, job_id FROM idempotency_keys "
                    "WHERE scope=? AND operation=? AND idempotency_key=?",
                    (scope, operation, idempotency_key),
                ).fetchone()
                if existing is not None:
                    if existing["request_hash"] != request_hash:
                        raise IdempotencyConflict(
                            "idempotency key was already used with a different request digest"
                        )
                    row = connection.execute(
                        "SELECT * FROM jobs WHERE job_id=?", (existing["job_id"],)
                    ).fetchone()
                    return self._decode_job(row), row["status"] == "completed"

            cached = connection.execute(
                "SELECT * FROM jobs WHERE kind=? AND request_hash=? AND status='completed' "
                "ORDER BY updated_at_utc DESC LIMIT 1",
                (kind, request_hash),
            ).fetchone()
            job_id = stable_id("job", {"request_hash": request_hash, "nonce": uuid.uuid4().hex})
            resource_id = stable_id("resource", {"kind": kind, "request_hash": request_hash})
            if cached is not None:
                connection.execute(
                    "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, 'completed', 'completed', 0, 0, "
                    "'{}', ?, NULL, NULL, ?, NULL, NULL, NULL, ?, ?)",
                    (
                        job_id,
                        kind,
                        project_id,
                        cached["resource_id"],
                        request_hash,
                        payload_json,
                        cached["result_json"],
                        cached["job_id"],
                        created,
                        created,
                    ),
                )
                self._append_event(
                    connection,
                    job_id,
                    "completed",
                    "completed",
                    {"cache_hit": True, "source_job_id": cached["job_id"]},
                )
                cache_hit = True
            else:
                connection.execute(
                    "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, 'queued', 'queued', 0, 0, "
                    "'{}', NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?)",
                    (
                        job_id,
                        kind,
                        project_id,
                        resource_id,
                        request_hash,
                        payload_json,
                        created,
                        created,
                    ),
                )
                self._append_event(connection, job_id, "status", "queued", {})
                cache_hit = False
            if idempotency_key:
                connection.execute(
                    "INSERT INTO idempotency_keys VALUES (?, ?, ?, ?, ?)",
                    (scope, operation, idempotency_key, request_hash, job_id),
                )
            row = connection.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return self._decode_job(row), cache_hit

    def get_job(self, job_id: str) -> JobSnapshot:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._decode_job(row)

    def list_jobs(
        self, *, kind: JobKind | None = None, project_id: str | None = None
    ) -> list[JobSnapshot]:
        query = "SELECT * FROM jobs"
        clauses: list[str] = []
        parameters: list[str] = []
        if kind is not None:
            clauses.append("kind=?")
            parameters.append(kind)
        if project_id is not None:
            clauses.append("project_id=?")
            parameters.append(project_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at_utc, job_id"
        with self._connect() as connection:
            rows = connection.execute(query, tuple(parameters)).fetchall()
        return [self._decode_job(row) for row in rows]

    def claim_next(self, owner: str, lease_seconds: float) -> tuple[JobSnapshot, str] | None:
        now = _now()
        expires = _timestamp(now + timedelta(seconds=lease_seconds))
        with self._transaction() as connection:
            expired = connection.execute(
                "SELECT job_id FROM jobs WHERE status IN ('initializing','running','finalizing') "
                "AND lease_expires_at_utc < ?",
                (_timestamp(now),),
            ).fetchall()
            for row in expired:
                connection.execute(
                    "UPDATE jobs SET status='failed', phase='failed', error_code='worker_lost', "
                    "error_message='coordinator lease expired', attempt_token=NULL, lease_owner=NULL, "
                    "lease_expires_at_utc=NULL, updated_at_utc=? WHERE job_id=?",
                    (_timestamp(now), row["job_id"]),
                )
                self._append_event(
                    connection,
                    row["job_id"],
                    "failed",
                    "failed",
                    {"code": "worker_lost"},
                )
            row = connection.execute(
                "SELECT * FROM jobs WHERE status='queued' ORDER BY created_at_utc, job_id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            connection.execute(
                "UPDATE jobs SET status='initializing', phase='initializing', attempt=attempt+1, "
                "attempt_token=?, lease_owner=?, lease_expires_at_utc=?, updated_at_utc=? "
                "WHERE job_id=? AND status='queued'",
                (token, owner, expires, _timestamp(now), row["job_id"]),
            )
            self._append_event(connection, row["job_id"], "status", "initializing", {})
            claimed = connection.execute(
                "SELECT * FROM jobs WHERE job_id=?", (row["job_id"],)
            ).fetchone()
        return self._decode_job(claimed), token

    def retry(self, job_id: str) -> JobSnapshot:
        snapshot = self.get_job(job_id)
        if snapshot.project_id:
            self.get_project(snapshot.project_id)
        with self._transaction() as connection:
            row = connection.execute("SELECT status FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None or row["status"] not in {"failed", "cancelled"}:
                raise ValueError("Retry requires a failed or cancelled job")
            connection.execute(
                "UPDATE jobs SET status='queued', phase='queued', cancel_requested=0, result_json=NULL, error_code=NULL, error_message=NULL, attempt_token=NULL, lease_owner=NULL, lease_expires_at_utc=NULL, updated_at_utc=? WHERE job_id=?",
                (_timestamp(), job_id),
            )
            self._append_event(
                connection,
                job_id,
                "status",
                "queued",
                {"status": "queued", "reason": "explicit_retry_same_input"},
            )
        return self.get_job(job_id)

    def recover_expired(self) -> tuple[str, ...]:
        """Fail abandoned attempts before a coordinator begins accepting new work."""

        now = _timestamp()
        recovered: list[str] = []
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT job_id FROM jobs WHERE status IN ('initializing','running','finalizing') "
                "AND lease_expires_at_utc < ? ORDER BY job_id",
                (now,),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE jobs SET status='failed', phase='failed', error_code='worker_lost', "
                    "error_message='coordinator lease expired', attempt_token=NULL, lease_owner=NULL, "
                    "lease_expires_at_utc=NULL, updated_at_utc=? WHERE job_id=?",
                    (now, row["job_id"]),
                )
                self._append_event(
                    connection, row["job_id"], "failed", "failed", {"code": "worker_lost"}
                )
                recovered.append(row["job_id"])
        return tuple(recovered)

    def payload(self, job_id: str, token: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json, attempt_token FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
        if row is None or row["attempt_token"] != token:
            raise LeaseFenceError("attempt token is stale")
        return json.loads(row["payload_json"])

    def _fenced_update(
        self,
        job_id: str,
        token: str,
        *,
        status: str,
        phase: str,
        event_type: str,
        payload: dict[str, Any],
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        terminal: bool = False,
    ) -> JobSnapshot:
        now = _timestamp()
        with self._transaction() as connection:
            current = connection.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if current is None or current["attempt_token"] != token:
                raise LeaseFenceError("attempt token is stale")
            if current["status"] in TERMINAL_STATUSES:
                raise LeaseFenceError("job is already terminal")
            connection.execute(
                "UPDATE jobs SET status=?, phase=?, result_json=?, error_code=?, error_message=?, "
                "attempt_token=?, lease_owner=?, lease_expires_at_utc=?, updated_at_utc=? "
                "WHERE job_id=? AND attempt_token=?",
                (
                    status,
                    phase,
                    canonical_json_text(result) if result is not None else current["result_json"],
                    error_code,
                    error_message,
                    None if terminal else token,
                    None if terminal else current["lease_owner"],
                    None if terminal else current["lease_expires_at_utc"],
                    now,
                    job_id,
                    token,
                ),
            )
            self._append_event(connection, job_id, event_type, phase, payload)
            row = connection.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if terminal and row["project_id"]:
            self.get_project(row["project_id"])
        return self._decode_job(row)

    def mark_running(self, job_id: str, token: str) -> JobSnapshot:
        return self._fenced_update(
            job_id,
            token,
            status="running",
            phase="running",
            event_type="status",
            payload={},
        )

    def mark_finalizing(self, job_id: str, token: str) -> JobSnapshot:
        return self._fenced_update(
            job_id,
            token,
            status="finalizing",
            phase="finalizing",
            event_type="status",
            payload={},
        )

    def record_progress(
        self,
        job_id: str,
        token: str,
        *,
        phase: str,
        completed: int,
        total: int | None,
    ) -> JobSnapshot:
        if completed < 0 or total is not None and (total < 0 or completed > total):
            raise ValueError("invalid progress counters")
        with self._transaction() as connection:
            current = connection.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if current is None or current["attempt_token"] != token:
                raise LeaseFenceError("attempt token is stale")
            if current["status"] not in {"initializing", "running"}:
                raise LeaseFenceError("progress requires an active job")
            counters = {"completed": completed, "total": total}
            connection.execute(
                "UPDATE jobs SET phase=?, counters_json=?, updated_at_utc=? "
                "WHERE job_id=? AND attempt_token=?",
                (phase, canonical_json_text(counters), _timestamp(), job_id, token),
            )
            self._append_event(connection, job_id, "progress", phase, counters)
            row = connection.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return self._decode_job(row)

    def complete(self, job_id: str, token: str, result: dict[str, Any]) -> JobSnapshot:
        result = dict(result)
        result["resource_id"] = self.get_job(job_id).resource_id
        snapshot = self._fenced_update(
            job_id,
            token,
            status="completed",
            phase="completed",
            event_type="completed",
            payload={"resource_id": result.get("resource_id")},
            result=result,
            terminal=True,
        )
        artifact = result.get("artifact")
        if result.get("validation_level") == "configuration":
            self.register_resource(
                resource_id=snapshot.resource_id,
                kind=snapshot.kind,
                content_hash=scientific_hash(result),
                artifact_kind=None,
                job_id=job_id,
                metadata=result,
            )
        elif isinstance(artifact, dict):
            self.register_resource(
                resource_id=snapshot.resource_id,
                kind=snapshot.kind,
                content_hash=artifact["content_hash"],
                artifact_kind=artifact["artifact_kind"],
                job_id=job_id,
                metadata=result,
                dependencies=result.get("dependencies", []),
            )
        elif snapshot.kind == "region_search":
            self.register_resource(
                resource_id=snapshot.resource_id,
                kind="region_search",
                artifact_kind=None,
                content_hash=scientific_hash(result),
                job_id=job_id,
                metadata=result,
            )
        elif isinstance(result.get("export"), dict):
            self.register_resource(
                resource_id=snapshot.resource_id,
                kind="export",
                content_hash=result["export"]["sha256"],
                artifact_kind="export",
                job_id=job_id,
                metadata=result,
                dependencies=(
                    {
                        "artifact_id": result["requested_resource_id"],
                        "role": "export_source",
                    },
                ),
            )
        return snapshot

    def fail(self, job_id: str, token: str, code: str, message: str) -> JobSnapshot:
        return self._fenced_update(
            job_id,
            token,
            status="failed",
            phase="failed",
            event_type="failed",
            payload={"code": code},
            error_code=code,
            error_message=message[:2_000],
            terminal=True,
        )

    def mark_cancelled(self, job_id: str, token: str) -> JobSnapshot:
        return self._fenced_update(
            job_id,
            token,
            status="cancelled",
            phase="cancelled",
            event_type="cancelled",
            payload={},
            terminal=True,
        )

    def renew_lease(self, job_id: str, token: str, lease_seconds: float) -> bool:
        expires = _timestamp(_now() + timedelta(seconds=lease_seconds))
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE jobs SET lease_expires_at_utc=?, updated_at_utc=? WHERE job_id=? "
                "AND attempt_token=? AND status IN ('initializing','running','finalizing')",
                (expires, _timestamp(), job_id, token),
            )
            return cursor.rowcount == 1

    def request_cancel(self, job_id: str) -> JobSnapshot:
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            if row["status"] in TERMINAL_STATUSES:
                return self._decode_job(row)
            if row["status"] == "queued":
                connection.execute(
                    "UPDATE jobs SET status='cancelled', phase='cancelled', cancel_requested=1, "
                    "updated_at_utc=? WHERE job_id=?",
                    (_timestamp(), job_id),
                )
                self._append_event(connection, job_id, "cancelled", "cancelled", {})
            else:
                connection.execute(
                    "UPDATE jobs SET cancel_requested=1, phase='cancelling', updated_at_utc=? "
                    "WHERE job_id=?",
                    (_timestamp(), job_id),
                )
                self._append_event(connection, job_id, "status", "cancelling", {})
            updated = connection.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return self._decode_job(updated)

    def events_after(self, job_id: str, after: int) -> tuple[list[JobEvent], bool]:
        with self._connect() as connection:
            if (
                connection.execute("SELECT 1 FROM jobs WHERE job_id=?", (job_id,)).fetchone()
                is None
            ):
                raise KeyError(job_id)
            bounds = connection.execute(
                "SELECT MIN(event_id), MAX(event_id) FROM job_events WHERE job_id=?", (job_id,)
            ).fetchone()
            rows = connection.execute(
                "SELECT * FROM job_events WHERE job_id=? AND event_id>? ORDER BY event_id",
                (job_id, after),
            ).fetchall()
        reset = bounds[0] is not None and after > 0 and after < bounds[0] - 1
        return (
            [
                JobEvent(
                    event_id=row["event_id"],
                    job_id=row["job_id"],
                    type=row["type"],
                    timestamp_utc=row["timestamp_utc"],
                    phase=row["phase"],
                    payload=json.loads(row["payload_json"]),
                )
                for row in rows
            ],
            reset,
        )

    def register_resource(
        self,
        *,
        resource_id: str,
        kind: str,
        content_hash: str,
        artifact_kind: str | None,
        job_id: str | None,
        metadata: dict[str, Any],
        dependencies: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
    ) -> ResourceRecord:
        created = _timestamp()
        with self._transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO resources VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    resource_id,
                    kind,
                    content_hash,
                    artifact_kind,
                    job_id,
                    canonical_json_text(metadata),
                    created,
                ),
            )
            for dependency in dependencies:
                connection.execute(
                    "INSERT OR IGNORE INTO resource_dependencies VALUES (?, ?, ?)",
                    (resource_id, dependency["artifact_id"], dependency["role"]),
                )
        return self.get_resource(resource_id)

    def get_resource(self, resource_id: str) -> ResourceRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM resources WHERE resource_id=?", (resource_id,)
            ).fetchone()
        if row is None:
            raise KeyError(resource_id)
        return ResourceRecord(
            resource_id=row["resource_id"],
            kind=row["kind"],
            content_hash=row["content_hash"],
            artifact_kind=row["artifact_kind"],
            job_id=row["job_id"],
            metadata=json.loads(row["metadata_json"]),
            created_at_utc=row["created_at_utc"],
        )

    def resource_dependents(self, resource_id: str) -> list[str]:
        with self._connect() as connection:
            return [
                row[0]
                for row in connection.execute(
                    "SELECT resource_id FROM resource_dependencies WHERE depends_on_id=? "
                    "ORDER BY resource_id",
                    (resource_id,),
                )
            ]

    def list_resources(self, *, kind: str | None = None) -> list[ResourceRecord]:
        query = "SELECT resource_id FROM resources"
        parameters: tuple[str, ...] = ()
        if kind is not None:
            query += " WHERE kind=?"
            parameters = (kind,)
        query += " ORDER BY created_at_utc, resource_id"
        with self._connect() as connection:
            ids = [row[0] for row in connection.execute(query, parameters)]
        return [self.get_resource(resource_id) for resource_id in ids]

    def delete_resource(self, resource_id: str) -> ResourceRecord:
        record = self.get_resource(resource_id)
        dependents = self.resource_dependents(resource_id)
        if dependents:
            raise RevisionConflict("resource is referenced by: " + ", ".join(dependents))
        with self._transaction() as connection:
            connection.execute(
                "DELETE FROM resource_dependencies WHERE resource_id=?", (resource_id,)
            )
            connection.execute("DELETE FROM resources WHERE resource_id=?", (resource_id,))
        return record
