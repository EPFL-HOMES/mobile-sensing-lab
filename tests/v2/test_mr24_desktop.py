"""Double-click startup does not reuse other workspaces or reinstall every launch."""

import io
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from mobile_sensing import desktop
from mobile_sensing.api.workspace import create_workspace_app


def test_workspace_handshake_is_exact_and_read_only(tmp_path):
    client = TestClient(create_workspace_app(tmp_path))
    assert client.get("/api/v1/workspace-info").json() == {
        "application": "mobile-sensing",
        "workspace": str(tmp_path.resolve()),
    }
    assert client.get("/api/v1/projects").json() == []


@pytest.mark.parametrize("same", [False, True])
def test_only_matching_workspace_server_is_reused(tmp_path, monkeypatch, same):
    root = tmp_path / "project"
    value = {
        "application": "mobile-sensing",
        "workspace": str(root if same else tmp_path / "other"),
    }
    monkeypatch.setattr(
        desktop.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(json.dumps(value).encode())
    )
    assert desktop.running_workspace_url(root, 8820) == ("http://127.0.0.1:8820/" if same else None)


def test_existing_server_never_installs_or_launches_again(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop, "running_workspace_url", lambda *a: "http://127.0.0.1:8820/")
    monkeypatch.setattr(desktop, "prepare_runtime", lambda *a: pytest.fail("unexpected install"))
    opened = []
    monkeypatch.setattr(desktop.webbrowser, "open", opened.append)
    assert desktop.main(["--repository", str(tmp_path)]) == 0
    assert opened == ["http://127.0.0.1:8820/"]


def test_first_setup_reuses_valid_delivery_and_dependency_change_installs_once(
    tmp_path, monkeypatch
):
    python = tmp_path / ".app-venv/bin/python"
    python.parent.mkdir(parents=True)
    python.touch()
    state = tmp_path / "state"
    state.mkdir()
    (tmp_path / "pyproject.toml").write_text("original")
    (tmp_path / "poetry.lock").write_text("locked")
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(desktop.subprocess, "run", run)
    assert desktop.prepare_runtime(tmp_path, state) == python
    assert len(commands) == 1 and commands[0][1] == "-c"
    desktop.prepare_runtime(tmp_path, state)
    assert len(commands) == 1
    (tmp_path / "pyproject.toml").write_text("updated")
    desktop.prepare_runtime(tmp_path, state)
    assert commands[-1][1:5] == ["-m", "pip", "install", "-e"]
    desktop.prepare_runtime(tmp_path, state)
    assert len(commands) == 3


def test_failed_setup_does_not_record_success(tmp_path, monkeypatch):
    python = tmp_path / ".app-venv/bin/python"
    python.parent.mkdir(parents=True)
    python.touch()
    state = tmp_path / "state"
    state.mkdir()
    (tmp_path / "pyproject.toml").write_text("original")
    (tmp_path / "poetry.lock").write_text("locked")

    def run(command, **kwargs):
        if command[1] == "-c":
            return SimpleNamespace(returncode=1)
        raise desktop.subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(desktop.subprocess, "run", run)
    with pytest.raises(desktop.subprocess.CalledProcessError):
        desktop.prepare_runtime(tmp_path, state)
    assert not (state / "dependencies.txt").exists()
