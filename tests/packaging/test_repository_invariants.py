from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FRAMEWORK_SHA256 = "bb13176affcfe6060747336e75f6345eb93f7e84c176db3b8c50dcf08030ad83"


def test_immutable_framework_hash() -> None:
    framework = ROOT / "docs" / "MOBILE_SENSING_FRAMEWORK.tex"
    assert hashlib.sha256(framework.read_bytes()).hexdigest() == FRAMEWORK_SHA256


def test_reproducible_python_and_dependency_boundary() -> None:
    with (ROOT / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)

    metadata = project["project"]
    dependencies = metadata["dependencies"]
    assert metadata["requires-python"] == ">=3.12,<3.13"

    forbidden = {
        "dash",
        "holoviews",
        "hvplot",
        "panel",
        "pydeck",
        "streamlit",
    }
    assert forbidden.isdisjoint(
        {requirement.split("[", 1)[0].split("=", 1)[0].casefold() for requirement in dependencies}
    )

    extras = metadata["optional-dependencies"]
    assert set(extras) == {"notebook", "web", "geography", "optimization"}
    assert [value.split(">", 1)[0].split("=", 1)[0] for value in extras["notebook"]] == [
        "matplotlib",
        "ipython",
        "ipykernel",
    ]
    assert extras["geography"] == ["osmnx==2.0.6"]
    assert extras["optimization"] == ["ortools==9.11.4210"]
    assert [value.split("[", 1)[0].split(">", 1)[0] for value in extras["web"]] == [
        "fastapi",
        "uvicorn",
        "python-multipart",
    ]
    assert project["tool"]["pytest"]["ini_options"]["testpaths"] == ["tests"]
