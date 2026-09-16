"""One heavy worker job across independent project queues, outside the API process."""

import fcntl
import time

from mobile_sensing.jobs.coordinator import LocalCoordinator, CoordinatorAlreadyRunning
from mobile_sensing.jobs.workspace import ProjectWorkspace


class WorkspaceCoordinator:
    def __init__(self, root, *, max_workers=1):
        self.workspace = ProjectWorkspace(root)
        self.max_workers = max_workers
        self.stop = False
        self.current = None
        self.lock = None

    def __enter__(self):
        control = self.workspace.root.parent / ("." + self.workspace.root.name + "-workspace")
        control.mkdir(exist_ok=True)
        self.lock = (control / "coordinator.lock").open("a+")
        try:
            fcntl.flock(self.lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            self.lock.close()
            raise CoordinatorAlreadyRunning(
                "Another coordinator owns this project workspace"
            ) from error
        return self

    def __exit__(self, *args):
        if self.lock:
            self.lock.close()

    def request_stop(self):
        self.stop = True
        if self.current:
            self.current.request_stop()

    def run_once(self):
        self.workspace.refresh()
        candidates = []
        for identifier, (_, private) in self.workspace._projects.items():
            store = self.workspace.store(identifier)
            store.recover_expired()
            with store._connect() as connection:
                job = connection.execute(
                    "SELECT created_at_utc,job_id FROM jobs WHERE status='queued' ORDER BY created_at_utc,job_id LIMIT 1"
                ).fetchone()
            if job:
                candidates.append((job[0], job[1], private))
        if not candidates:
            return False
        private = min(candidates)[2]
        with LocalCoordinator(private, max_workers=self.max_workers) as coordinator:
            self.current = coordinator
            try:
                return coordinator.run_once()
            finally:
                self.current = None

    def run_forever(self):
        while not self.stop:
            if not self.run_once():
                time.sleep(0.25)
