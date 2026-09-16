"""Single-owner local coordinator with a spawn-based bounded process pool."""

from __future__ import annotations

import fcntl
import json
import os
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from contextlib import suppress
from multiprocessing import get_context
from pathlib import Path

from mobile_sensing.application import HeadlessApplication, ScenarioResourceBundle
from mobile_sensing.application.services import _execute_replication_spawn
from mobile_sensing.contracts import ArtifactManifest, canonical_json_text
from mobile_sensing.contracts import EnvironmentArtifactRef, ExecutionOptions
from mobile_sensing.environment import PreparedEnvironmentReader
from mobile_sensing.jobs.store import JobStore, LeaseFenceError
from mobile_sensing.jobs.worker import JobCancelled, execute_job, initialize_worker_limits


class CoordinatorAlreadyRunning(RuntimeError):
    pass


class LocalCoordinator:
    """Coordinate one heavy job; pool width is bounded for later replication fan-out."""

    def __init__(
        self,
        artifact_root: str | Path,
        *,
        max_workers: int = 1,
        lease_seconds: float = 10.0,
        poll_interval_s: float = 0.05,
        cancellation_grace_s: float = 2.0,
    ) -> None:
        if isinstance(max_workers, bool) or max_workers < 1:
            raise ValueError("max_workers must be positive")
        if max_workers > (os.cpu_count() or 1):
            raise ValueError("max_workers exceeds the detected logical CPU count")
        if lease_seconds <= poll_interval_s:
            raise ValueError("lease_seconds must exceed poll_interval_s")
        self.root = Path(artifact_root).resolve()
        self.store = JobStore(self.root)
        self.max_workers = max_workers
        self.lease_seconds = lease_seconds
        self.poll_interval_s = poll_interval_s
        self.cancellation_grace_s = cancellation_grace_s
        self.owner = f"coordinator-{uuid.uuid4().hex}"
        self._stop_requested = False
        self._lock_file = None
        self._pool: ProcessPoolExecutor | None = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, traceback_value):
        self.close()

    def acquire(self) -> None:
        if self._lock_file is not None:
            return
        path = self.root / ".coordinator.lock"
        self._lock_file = path.open("a+")
        try:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lock_file.close()
            self._lock_file = None
            raise CoordinatorAlreadyRunning("another coordinator owns this workspace") from exc
        self._lock_file.seek(0)
        self._lock_file.truncate()
        self._lock_file.write(self.owner)
        self._lock_file.flush()
        self.store.recover_expired()
        self._pool = ProcessPoolExecutor(
            max_workers=self.max_workers,
            mp_context=get_context("spawn"),
            initializer=initialize_worker_limits,
        )

    def request_stop(self) -> None:
        self._stop_requested = True

    def run_forever(self) -> None:
        self.acquire()
        while not self._stop_requested:
            if not self.run_once():
                time.sleep(self.poll_interval_s)

    def run_once(self) -> bool:
        self.acquire()
        self.store.recover_expired()
        claimed = self.store.claim_next(self.owner, self.lease_seconds)
        if claimed is None:
            return False
        snapshot, token = claimed
        work = self.root / ".job-control" / snapshot.job_id / token
        work.mkdir(parents=True, exist_ok=True)
        cancellation_path = work / "cancel"
        progress_path = work / "progress.json"
        payload = self.store.payload(snapshot.job_id, token)
        self.store.mark_running(snapshot.job_id, token)
        if snapshot.kind == "simulation":
            try:
                return self._run_simulation(snapshot, token, payload, cancellation_path)
            except LeaseFenceError:
                raise
            except BaseException as exc:
                with suppress(LeaseFenceError):
                    self.store.fail(
                        snapshot.job_id,
                        token,
                        "job_failed",
                        str(exc) or type(exc).__name__,
                    )
                return True
            finally:
                cancellation_path.unlink(missing_ok=True)
                progress_path.unlink(missing_ok=True)
                with suppress(OSError):
                    work.rmdir()
        assert self._pool is not None
        future = self._pool.submit(
            execute_job,
            str(self.root),
            snapshot.resource_id,
            snapshot.kind,
            payload,
            str(cancellation_path),
            str(progress_path),
        )
        last_progress: tuple[str, int, int | None] | None = None
        cancellation_started: float | None = None
        timeout_s = payload.get("options", {}).get("job_timeout_s")
        deadline = time.monotonic() + timeout_s if timeout_s else None
        try:
            while not future.done():
                if deadline is not None and time.monotonic() >= deadline:
                    cancellation_path.touch(exist_ok=True)
                    self._terminate_pool()
                    self.store.fail(
                        snapshot.job_id,
                        token,
                        "job_timeout",
                        "The configured job duration limit was reached; completed immutable stages remain reusable",
                    )
                    return True
                current = self.store.get_job(snapshot.job_id)
                if self._stop_requested and not current.cancel_requested:
                    current = self.store.request_cancel(snapshot.job_id)
                if current.cancel_requested:
                    cancellation_path.touch(exist_ok=True)
                    cancellation_started = cancellation_started or time.monotonic()
                if progress_path.exists():
                    with suppress(OSError, json.JSONDecodeError):
                        value = json.loads(progress_path.read_text(encoding="utf-8"))
                        item = (value["phase"], int(value["completed"]), value.get("total"))
                        if item != last_progress:
                            self.store.record_progress(
                                snapshot.job_id,
                                token,
                                phase=item[0],
                                completed=item[1],
                                total=item[2],
                            )
                            last_progress = item
                if cancellation_started is not None and (
                    time.monotonic() - cancellation_started > self.cancellation_grace_s
                ):
                    self._terminate_pool()
                    self.store.mark_cancelled(snapshot.job_id, token)
                    return True
                if not self.store.renew_lease(snapshot.job_id, token, self.lease_seconds):
                    raise LeaseFenceError("coordinator lost its attempt lease")
                time.sleep(self.poll_interval_s)
            result = future.result()
            if self.store.get_job(snapshot.job_id).cancel_requested:
                self.store.mark_cancelled(snapshot.job_id, token)
            else:
                if snapshot.kind == "project_import":
                    from mobile_sensing.application.project_package import finalize_import

                    finalize_import(self.store, snapshot.project_id, result)
                self._attach_dependencies(result)
                self.store.mark_finalizing(snapshot.job_id, token)
                self.store.complete(snapshot.job_id, token, result)
        except KeyboardInterrupt:
            cancellation_path.touch(exist_ok=True)
            self._terminate_pool()
            with suppress(LeaseFenceError):
                self.store.mark_cancelled(snapshot.job_id, token)
            raise
        except JobCancelled:
            self.store.mark_cancelled(snapshot.job_id, token)
        except LeaseFenceError:
            raise
        except BrokenProcessPool as exc:
            with suppress(LeaseFenceError):
                self.store.fail(snapshot.job_id, token, "worker_lost", str(exc))
            self._terminate_pool()
        except BaseException as exc:
            with suppress(LeaseFenceError):
                from mobile_sensing.environment.acquisition import OSMConnectionError
                from mobile_sensing.simulation.one_shot import PlanningTimeout, PlanningFailure

                if isinstance(exc, PlanningTimeout):
                    error_code = "planning_timeout"
                elif isinstance(exc, PlanningFailure):
                    error_code = "planning_failed"
                elif isinstance(exc, OSMConnectionError):
                    error_code = "osm_connection_failed"
                else:
                    error_code = "job_failed"
                self.store.fail(
                    snapshot.job_id,
                    token,
                    error_code,
                    str(exc) or type(exc).__name__,
                )
        finally:
            with suppress(OSError):
                progress_path.unlink()
            with suppress(OSError):
                cancellation_path.unlink()
            with suppress(OSError):
                work.rmdir()
        return True

    def _run_simulation(self, snapshot, token: str, payload, cancellation_path: Path) -> bool:
        """Parallelize replications directly; no worker process creates a nested pool."""

        environment = PreparedEnvironmentReader(self.root).read(
            EnvironmentArtifactRef.model_validate_json(canonical_json_text(payload["environment"]))
        )
        application = HeadlessApplication(self.root)
        validated = application.validate_scenario(
            environment,
            ScenarioResourceBundle.model_validate_json(canonical_json_text(payload["resources"])),
        )
        options = ExecutionOptions.model_validate_json(canonical_json_text(payload["options"]))
        if options.workers > self.max_workers:
            self.store.fail(
                snapshot.job_id,
                token,
                "resource_limit",
                "requested workers exceed the coordinator worker bound",
            )
            return True
        if options.memory_limit_bytes < validated.estimated_working_bytes * options.workers:
            self.store.fail(
                snapshot.job_id,
                token,
                "resource_limit",
                "memory limit is below the conservative worker-scaled estimate",
            )
            return True
        started = time.perf_counter()
        timeout_at = started + options.job_timeout_s if options.job_timeout_s is not None else None
        scenario = validated.bundle.scenario
        arguments = [
            (
                str(self.root),
                validated.environment.reference,
                (
                    index,
                    replication,
                    scenario,
                    validated.vehicle_specs,
                    validated.locations,
                    validated.assignment_plans,
                    validated.location_area_ids,
                    options.progress_frequency_events,
                ),
                str(cancellation_path),
            )
            for index, replication in enumerate(validated.replications)
        ]
        pool = ProcessPoolExecutor(
            max_workers=options.workers,
            mp_context=get_context("spawn"),
            initializer=initialize_worker_limits,
        )
        futures = {pool.submit(_execute_replication_spawn, item) for item in arguments}
        completed_results = []
        cancellation_started = None
        timed_out = False
        try:
            while futures:
                done, futures = wait(
                    futures, timeout=self.poll_interval_s, return_when=FIRST_COMPLETED
                )
                for future in done:
                    completed_results.append(future.result())
                if done:
                    self.store.record_progress(
                        snapshot.job_id,
                        token,
                        phase="simulation.replications",
                        completed=len(completed_results),
                        total=len(arguments),
                    )
                current = self.store.get_job(snapshot.job_id)
                if self._stop_requested and not current.cancel_requested:
                    current = self.store.request_cancel(snapshot.job_id)
                if current.cancel_requested:
                    cancellation_path.touch(exist_ok=True)
                    cancellation_started = cancellation_started or time.monotonic()
                if timeout_at is not None and time.monotonic() >= timeout_at:
                    cancellation_path.touch(exist_ok=True)
                    cancellation_started = cancellation_started or time.monotonic()
                    timed_out = True
                if cancellation_started is not None and (
                    time.monotonic() - cancellation_started > self.cancellation_grace_s
                ):
                    for process in pool._processes.values():
                        process.terminate()
                    pool.shutdown(wait=True, cancel_futures=True)
                    if timed_out:
                        self.store.fail(
                            snapshot.job_id,
                            token,
                            "timeout",
                            "configured job timeout elapsed",
                        )
                    else:
                        self.store.mark_cancelled(snapshot.job_id, token)
                    return True
                if not self.store.renew_lease(snapshot.job_id, token, self.lease_seconds):
                    raise LeaseFenceError("coordinator lost its simulation lease")
            pool.shutdown(wait=True)
            if timed_out:
                self.store.fail(
                    snapshot.job_id,
                    token,
                    "timeout",
                    "configured job timeout elapsed",
                )
                return True
            if self.store.get_job(snapshot.job_id).cancel_requested:
                self.store.mark_cancelled(snapshot.job_id, token)
                return True
            completed_results.sort(key=lambda item: item[0])
            results = tuple(item[1] for item in completed_results)
            manifests = {item[1].replication_id: item[2] for item in completed_results}
            published = application._publish_completed_simulation(
                validated,
                results,
                manifests,
                started=started,
                worker_count=options.workers,
            )
            result = {
                "resource_id": snapshot.resource_id,
                "artifact": published.reference.model_dump(mode="json"),
                "replications_R": len(results),
            }
            self._attach_dependencies(result)
            self.store.mark_finalizing(snapshot.job_id, token)
            self.store.complete(snapshot.job_id, token, result)
        except KeyboardInterrupt:
            cancellation_path.touch(exist_ok=True)
            for process in pool._processes.values():
                process.terminate()
            pool.shutdown(wait=True, cancel_futures=True)
            with suppress(LeaseFenceError):
                self.store.mark_cancelled(snapshot.job_id, token)
            raise
        except BrokenProcessPool as exc:
            pool.shutdown(wait=True, cancel_futures=True)
            self.store.fail(snapshot.job_id, token, "worker_lost", str(exc))
        except BaseException as exc:
            pool.shutdown(wait=True, cancel_futures=True)
            if timed_out:
                self.store.fail(
                    snapshot.job_id,
                    token,
                    "timeout",
                    "configured job timeout elapsed",
                )
            elif self.store.get_job(snapshot.job_id).cancel_requested:
                self.store.mark_cancelled(snapshot.job_id, token)
            else:
                self.store.fail(snapshot.job_id, token, "job_failed", str(exc))
        finally:
            cancellation_path.unlink(missing_ok=True)
            with suppress(OSError):
                cancellation_path.parent.rmdir()
        return True

    def _attach_dependencies(self, result: dict) -> None:
        artifact = result.get("artifact")
        if not isinstance(artifact, dict):
            return
        for collection in (
            "datasets",
            "environments",
            "simulations",
            "exposures",
            "portfolios",
        ):
            manifest_path = self.root / collection / artifact["artifact_id"] / "manifest.json"
            if manifest_path.is_file():
                manifest = ArtifactManifest.model_validate_json(manifest_path.read_bytes())
                result["dependencies"] = [
                    item.model_dump(mode="json") for item in manifest.dependencies
                ]
                return

    def _terminate_pool(self) -> None:
        if self._pool is None:
            return
        for process in self._pool._processes.values():  # owned children only
            process.terminate()
        self._pool.shutdown(wait=True, cancel_futures=True)
        self._pool = ProcessPoolExecutor(
            max_workers=self.max_workers,
            mp_context=get_context("spawn"),
            initializer=initialize_worker_limits,
        )

    def close(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=True, cancel_futures=True)
            self._pool = None
        if self._lock_file is not None:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
            self._lock_file.close()
            self._lock_file = None
