"""Standard-library bootstrap for the source-checkout macOS double-click entry point."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser


def running_workspace_url(root: Path, preferred_port: int) -> str | None:
    """Reuse only a server explicitly identifying this exact workspace."""
    ports = [preferred_port]
    receipt = root.parent / ("." + root.name + "-workspace") / "server.json"
    try:
        record = json.loads(receipt.read_text())
        if record["workspace"] == str(root.resolve()):
            ports.insert(0, int(record["port"]))
    except (OSError, ValueError, KeyError, TypeError):
        pass
    for port in dict.fromkeys(ports):
        url = f"http://127.0.0.1:{port}"
        try:
            with urllib.request.urlopen(url + "/api/v1/workspace-info", timeout=1) as response:
                value = json.load(response)
            if value == {"application": "mobile-sensing", "workspace": str(root.resolve())}:
                return url + "/"
        except (OSError, ValueError, urllib.error.URLError):
            continue
    return None


def available_port(preferred: int) -> int:
    for port in range(preferred, min(preferred + 20, 65536)):
        with socket.socket() as connection:
            try:
                connection.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("No free local port is available. Close an unused application and retry.")


def _fingerprint(paths) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def prepare_runtime(repository: Path, state: Path) -> Path:
    """Install into a private environment only on first use or dependency changes."""
    python = repository / ".app-venv/bin/python"
    created = not python.is_file()
    if created:
        print("First use: creating the local Python environment…", flush=True)
        subprocess.run([sys.executable, "-m", "venv", str(python.parent.parent)], check=True)
    dependency_key = _fingerprint([repository / "pyproject.toml", repository / "poetry.lock"])
    receipt = state / "dependencies.txt"
    if not created and receipt.is_file() and receipt.read_text() == dependency_key:
        return python
    # An existing installed delivery needs no reinstall when requirements still match.
    probe = subprocess.run(
        [
            str(python),
            "-c",
            "import fastapi, uvicorn, multipart, osmnx, ortools, geopandas, pyarrow; "
            "import importlib.metadata as m; from packaging.specifiers import SpecifierSet; "
            "import tomllib, sys; p=tomllib.load(open(sys.argv[1], 'rb')); "
            "deps=p['tool']['poetry']['dependencies']; "
            "assert sys.version_info[:2] == (3, 12); "
            "required={n: (v['version'] if isinstance(v,dict) else v) for n,v in deps.items() "
            "if n != 'python' and (not isinstance(v,dict) or not v.get('optional') or "
            "n in {'fastapi','uvicorn','python-multipart','osmnx','ortools'})}; "
            "assert all(m.version(n) in SpecifierSet(v if v[0] in '<>=!~' else '=='+v) "
            "for n,v in required.items())",
            str(repository / "pyproject.toml"),
        ],
        capture_output=True,
        text=True,
    )
    # Once a receipt exists, changed dependency declarations require installation.
    if probe.returncode or receipt.exists():
        print("Installing the application dependencies (internet required)…", flush=True)
        subprocess.run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "-e",
                str(repository) + "[web,optimization,geography]",
            ],
            check=True,
        )
    receipt.write_text(dependency_key)
    return python


def prepare_frontend(repository: Path, state: Path) -> None:
    inputs = [repository / "frontend/package.json", repository / "frontend/package-lock.json"]
    for name in ("src", "public"):
        inputs.extend(p for p in (repository / "frontend" / name).rglob("*") if p.is_file())
    inputs.extend((repository / "frontend").glob("*config*"))
    inputs = [p for p in inputs if p.is_file()]
    key = _fingerprint(inputs)
    receipt = state / "frontend.txt"
    index = repository / "src/mobile_sensing/_web/index.html"
    if index.is_file() and (
        (receipt.is_file() and receipt.read_text() == key)
        or (
            not receipt.exists() and index.stat().st_mtime >= max(p.stat().st_mtime for p in inputs)
        )
    ):
        receipt.write_text(key)
        return
    npm = shutil.which("npm")
    if npm is None:
        raise RuntimeError(
            "Building the source interface requires Node.js 22.12–22.x and npm. See README.md."
        )
    print("Building the browser interface…", flush=True)
    frontend = repository / "frontend"
    if not (frontend / "node_modules").is_dir():
        subprocess.run([npm, "ci"], cwd=frontend, check=True)
    subprocess.run([npm, "run", "build"], cwd=frontend, check=True)
    receipt.write_text(key)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8820)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    repository = args.repository.resolve()
    root = repository / "project"
    state = repository / ".mobile-sensing"
    state.mkdir(exist_ok=True)
    with (state / "startup.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Application setup is already running. Wait for its browser window.", flush=True)
            return 0
        url = running_workspace_url(root, args.port)
        if url:
            print(f"Opening the running workspace: {url}", flush=True)
            if not args.no_browser:
                webbrowser.open(url)
            return 0
        python = prepare_runtime(repository, state)
        prepare_frontend(repository, state)
        port = available_port(args.port)
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(repository / "src")
        command = [
            str(python),
            "-m",
            "mobile_sensing",
            "launch",
            "--artifact-root",
            str(root),
            "--port",
            str(port),
        ]
        if args.no_browser:
            command.append("--no-browser")
        print(
            f"Mobile Sensing Simulator: http://127.0.0.1:{port}/\nProjects: {root}\nKeep this window open. Press Ctrl+C to stop.",
            flush=True,
        )
        process = subprocess.Popen(command, cwd=repository, env=environment)
        try:
            deadline = time.monotonic() + 45
            while process.poll() is None and time.monotonic() < deadline:
                if running_workspace_url(root, port):
                    break
                time.sleep(0.1)
            # Keep setup exclusive until another click can verify the same server.
            fcntl.flock(lock, fcntl.LOCK_UN)
            return process.wait()
        except KeyboardInterrupt:
            process.wait(timeout=15)
            return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError, subprocess.CalledProcessError) as error:
        print(f"Mobile Sensing Simulator could not start: {error}", file=sys.stderr, flush=True)
        raise SystemExit(1)
