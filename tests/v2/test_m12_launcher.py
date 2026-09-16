from __future__ import annotations

import multiprocessing
import os
import time

import pytest
import uvicorn

from mobile_sensing.jobs.workspace_coordinator import WorkspaceCoordinator
from mobile_sensing.launcher import _await_coordinator, launch_local_application


@pytest.mark.parametrize("workers", [0, -1, True, 1.5, (os.cpu_count() or 1) + 1])
def test_invalid_workers_fail_before_creating_workspace(tmp_path, workers):
    root = tmp_path / "workspace"
    with pytest.raises(ValueError, match="workers"):
        launch_local_application(root, workers=workers, open_browser=False)
    assert not root.exists()


def test_occupied_lock_never_starts_api_or_browser(tmp_path, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("API/browser started before coordinator readiness")

    monkeypatch.setattr(uvicorn.Server, "run", unexpected)
    monkeypatch.setattr("mobile_sensing.launcher.webbrowser.open", unexpected)
    with WorkspaceCoordinator(tmp_path):
        with pytest.raises(RuntimeError, match="CoordinatorAlreadyRunning"):
            launch_local_application(tmp_path)
    assert not [
        p for p in multiprocessing.active_children() if p.name == "mobile-sensing-coordinator"
    ]


def test_ready_coordinator_is_exclusive_and_shutdown_allows_restart(tmp_path, monkeypatch):
    calls = []

    def serve(server):
        with pytest.raises(RuntimeError, match="Another coordinator owns"):
            with WorkspaceCoordinator(tmp_path):
                pass
        calls.append(True)

    monkeypatch.setattr(uvicorn.Server, "run", serve)
    for _ in range(2):
        launch_local_application(tmp_path, open_browser=False)
    assert len(calls) == 2
    with WorkspaceCoordinator(tmp_path):
        pass


def test_keyboard_interrupt_is_a_clean_launcher_shutdown(tmp_path, monkeypatch):
    def interrupt(_server):
        raise KeyboardInterrupt

    monkeypatch.setattr(uvicorn.Server, "run", interrupt)
    launch_local_application(tmp_path, open_browser=False)
    with WorkspaceCoordinator(tmp_path):
        pass


def test_coordinator_loss_stops_api_and_reports_failure(tmp_path, monkeypatch):
    def serve(server):
        child = next(
            p for p in multiprocessing.active_children() if p.name == "mobile-sensing-coordinator"
        )
        child.kill()
        deadline = time.monotonic() + 5
        while not server.should_exit and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.should_exit

    monkeypatch.setattr(uvicorn.Server, "run", serve)
    with pytest.raises(RuntimeError, match="exited unexpectedly"):
        launch_local_application(tmp_path, open_browser=False)


def test_readiness_timeout_and_early_exit_are_explicit():
    reader, writer = multiprocessing.Pipe(duplex=False)
    try:
        with pytest.raises(RuntimeError, match="timed out"):
            _await_coordinator(reader, timeout_s=0.01)
        writer.close()
        with pytest.raises(RuntimeError, match="before reporting readiness"):
            _await_coordinator(reader, timeout_s=0.01)
    finally:
        reader.close()
        writer.close()
