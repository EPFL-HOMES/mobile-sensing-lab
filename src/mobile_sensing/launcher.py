"""Single-command local browser/API/coordinator launcher."""

from __future__ import annotations

import multiprocessing
import json
import os
import signal
import threading
import webbrowser
from multiprocessing.connection import Connection
from pathlib import Path


def _coordinator_process(artifact_root: str, max_workers: int, readiness: Connection) -> None:
    """Own the coordinator in a process distinct from the API event loop."""

    try:
        from mobile_sensing.jobs.workspace_coordinator import WorkspaceCoordinator

        with WorkspaceCoordinator(artifact_root, max_workers=max_workers) as coordinator:

            def request_stop(_signum, _frame) -> None:
                coordinator.request_stop()

            signal.signal(signal.SIGTERM, request_stop)
            signal.signal(signal.SIGINT, request_stop)
            readiness.send(("ready", None))
            readiness.close()
            coordinator.run_forever()
    except BaseException as exc:
        if not readiness.closed:
            readiness.send(("error", f"{type(exc).__name__}: {exc}"))
        raise
    finally:
        readiness.close()


def _await_coordinator(readiness: Connection, timeout_s: float = 30.0) -> None:
    """Wait for lock/pool acquisition, propagating startup errors before serving."""
    if not readiness.poll(timeout_s):
        raise RuntimeError("local coordinator readiness timed out")
    try:
        state, detail = readiness.recv()
    except EOFError as exc:
        raise RuntimeError("local coordinator exited before reporting readiness") from exc
    if state != "ready":
        raise RuntimeError(f"local coordinator failed to start: {detail}")


def launch_local_application(
    artifact_root: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    workers: int = 2,
    open_browser: bool = True,
) -> None:
    """Serve the installed UI/API and run one bounded coordinator process."""

    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
        raise ValueError("workers must be a positive integer")
    if workers > (os.cpu_count() or 1):
        raise ValueError("workers exceeds the detected logical CPU count")
    root = Path(artifact_root).resolve()
    root.mkdir(parents=True, exist_ok=True)

    # Optional web dependencies remain outside the scientific import boundary.
    import uvicorn

    from mobile_sensing.api.workspace import create_workspace_app
    from mobile_sensing.jobs.workspace import migrate_shared_workspace
    from mobile_sensing.api.frontend import validate_frontend_bundle

    validate_frontend_bundle()
    migrate_shared_workspace(root)
    context = multiprocessing.get_context("spawn")
    readiness, child_readiness = context.Pipe(duplex=False)
    coordinator = context.Process(
        target=_coordinator_process,
        args=(str(root), workers, child_readiness),
        name="mobile-sensing-coordinator",
    )
    coordinator.start()
    child_readiness.close()
    browser_timer: threading.Timer | None = None
    stopping = threading.Event()
    coordinator_lost = threading.Event()
    monitor: threading.Thread | None = None
    try:
        _await_coordinator(readiness)
        server = uvicorn.Server(
            uvicorn.Config(create_workspace_app(root, serve_frontend=True), host=host, port=port)
        )
        receipt = root.parent / ("." + root.name + "-workspace") / "server.json"
        receipt.write_text(json.dumps({"workspace": str(root), "port": port, "pid": os.getpid()}))

        def watch_coordinator() -> None:
            while not stopping.wait(0.05):
                if not coordinator.is_alive():
                    coordinator_lost.set()
                    server.should_exit = True
                    return

        monitor = threading.Thread(target=watch_coordinator, daemon=True)
        monitor.start()
        if open_browser:
            browser_timer = threading.Timer(0.75, webbrowser.open, args=(f"http://{host}:{port}/",))
            browser_timer.daemon = True
            browser_timer.start()
        server.run()
        if coordinator_lost.is_set():
            raise RuntimeError("local coordinator exited unexpectedly; local API stopped")
    except KeyboardInterrupt:
        # Ctrl+C is the documented normal shutdown path for the local launcher.
        pass
    finally:
        stopping.set()
        if monitor is not None:
            monitor.join(timeout=1.0)
        readiness.close()
        if browser_timer is not None:
            browser_timer.cancel()
        if coordinator.is_alive():
            coordinator.terminate()
            coordinator.join(timeout=10.0)
        if coordinator.is_alive():
            coordinator.kill()
            coordinator.join(timeout=2.0)
        coordinator.join(timeout=2.0)
