from __future__ import annotations

import ast
import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

import mobile_sensing.contracts as contracts
from mobile_sensing.contracts import CapabilityRegistry, CapabilityUnavailableError
from mobile_sensing.contracts.configuration import EnvironmentProviderRequest


ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "src" / "mobile_sensing" / "contracts"
FIXTURES = Path(__file__).parent / "fixtures" / "contracts" / "examples"


def test_unavailable_and_unknown_capabilities_fail_explicitly() -> None:
    registry = CapabilityRegistry.model_validate_json(
        (FIXTURES / "capability_registry.json").read_bytes()
    )
    with pytest.raises(CapabilityUnavailableError, match="Live OSM acquisition is deferred"):
        registry.require("environment.osm@1")
    with pytest.raises(CapabilityUnavailableError, match="not installed"):
        registry.require("dispatch.tsp@1")
    assert registry.capabilities[
        0
    ].parameter_schema == EnvironmentProviderRequest.model_json_schema(mode="validation")


def test_public_exports_are_unique_and_include_extension_boundaries() -> None:
    assert len(contracts.__all__) == len(set(contracts.__all__))
    for name in (
        "EnvironmentProvider",
        "EnvironmentBuilder",
        "DemandAdapter",
        "DemandSource",
        "SupplySource",
        "DispatchPolicy",
        "RoutingService",
        "TaskExecutor",
        "ExposureAllocator",
        "ExposureReader",
        "AllocationSampler",
        "PortfolioEvaluator",
        "FrontierBuilder",
    ):
        assert getattr(contracts, name) is not None


def test_contract_modules_import_only_contract_and_standard_dependencies() -> None:
    allowed_roots = {
        "__future__",
        "collections",
        "datetime",
        "enum",
        "hashlib",
        "itertools",
        "json",
        "math",
        "pathlib",
        "re",
        "typing",
        "zoneinfo",
        "pydantic",
        "mobile_sensing",
    }
    forbidden = {"environment", "fastapi", "optimization", "simulation", "sqlalchemy", "uvicorn"}

    for path in CONTRACTS.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split(".", 1)[0])
        assert not roots & forbidden, (path, roots & forbidden)
        assert roots <= allowed_roots, (path, roots - allowed_roots)


def test_public_import_does_not_load_legacy_or_web_modules() -> None:
    script = """
import sys
sys.path.insert(0, %s)
import mobile_sensing
import mobile_sensing.contracts
for root in ('environment', 'simulation', 'optimization', 'fastapi', 'uvicorn'):
    assert root not in sys.modules, root
""" % json.dumps(
        str(ROOT / "src")
    )
    subprocess.run([sys.executable, "-c", script], check=True)


def test_distribution_packages_only_the_new_public_namespace() -> None:
    with (ROOT / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)
    assert project["project"]["name"] == "mobile-sensing"
    assert project["tool"]["poetry"]["packages"] == [{"include": "mobile_sensing", "from": "src"}]
