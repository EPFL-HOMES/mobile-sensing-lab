from __future__ import annotations

import ast
import json
import tomllib
from pathlib import Path

from fastapi.testclient import TestClient

from mobile_sensing.api import create_app
from mobile_sensing.api.frontend import bundled_frontend_root, validate_frontend_bundle
from mobile_sensing.datasets import (
    AreaAssignmentMapping,
    DemandImportMapping,
    RateImportMapping,
    VehicleImportMapping,
)


ROOT = Path(__file__).resolve().parents[2]


def test_installed_frontend_bundle_serves_spa_assets_and_api(tmp_path: Path) -> None:
    frontend = validate_frontend_bundle()
    assert frontend == bundled_frontend_root()
    index = (frontend / "index.html").read_bytes()
    assets = sorted(path for path in (frontend / "assets").iterdir() if path.is_file())
    assert assets

    client = TestClient(create_app(tmp_path, serve_frontend=True))
    root = client.get("/")
    assert root.status_code == 200 and root.content == index
    assert root.headers["content-type"].startswith("text/html")
    assert client.get("/results/operations").content == index
    assert client.get(f"/assets/{assets[0].name}").status_code == 200
    assert client.get("/api/v1/health").json()["status"] == "ok"
    unknown_api = client.get("/api/v1/release-does-not-exist")
    assert unknown_api.status_code == 404
    assert unknown_api.headers["content-type"].startswith("application/json")


def test_release_mapping_templates_are_strict_contracts() -> None:
    template_root = ROOT / "docs" / "templates" / "uploads"
    models = {
        "location_tasks.mapping.json": DemandImportMapping,
        "od_tasks.mapping.json": DemandImportMapping,
        "ordered_tasks.mapping.json": DemandImportMapping,
        "sparse_od_rates.mapping.json": RateImportMapping,
        "vehicles.mapping.json": VehicleImportMapping,
        "vehicle_area_assignments.mapping.json": AreaAssignmentMapping,
    }
    for name, model in models.items():
        value = model.model_validate_json((template_root / name).read_bytes())
        assert json.loads(value.model_dump_json())


def test_release_metadata_and_documentation_define_the_current_entry_points() -> None:
    with (ROOT / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)
    assert project["project"]["scripts"]["mobile-sensing"] == "mobile_sensing.cli:main"
    includes = project["tool"]["poetry"]["include"]
    assert {"path": "src/mobile_sensing/_web", "format": ["sdist", "wheel"]} in includes

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for required in (
        "mobile-sensing launch",
        "Operational replications `R`",
        "Portfolio sampling rounds `J`",
        "docs/QUICKSTART.md",
        "CONTRIBUTING.md",
    ):
        assert required in readme
    assert "streamlit run" not in readme.casefold()

    assert "notebooks/lausanne_simulation_tutorial.ipynb" in readme


def test_production_namespace_does_not_import_reference_or_legacy_packages() -> None:
    forbidden = {"environment", "optimization", "ref", "simulation"}
    violations: list[tuple[str, str]] = []
    for path in (ROOT / "src" / "mobile_sensing").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = {alias.name.split(".", 1)[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots = {node.module.split(".", 1)[0]}
            else:
                continue
            for root in sorted(roots & forbidden):
                violations.append((str(path.relative_to(ROOT)), root))
    assert violations == []


def test_retired_legacy_has_auditable_replacement_coverage() -> None:
    record = json.loads((ROOT / "tests/fixtures/release_baselines/retirement.json").read_bytes())
    assert record["status"] == "removed_after_replacement_validation"
    retired_paths = {entry["path"] for entry in record["files"]}
    assert not (ROOT / "legacy").exists()
    assert all(not (ROOT / path).exists() for path in retired_paths)
    assert all((ROOT / path).is_file() for path in record["replacements"])
    for retired in (
        "legacy/tests/simulation/test_demo03_pipeline.py",
        "legacy/tests/test_demo_views.py",
    ):
        assert retired in retired_paths

    for package in ("environment", "simulation", "optimization"):
        assert not (ROOT / "src" / package).exists()
