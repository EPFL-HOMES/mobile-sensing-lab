"""Self-contained project folders with private, relative artifact collection aliases."""

import json
import os
from pathlib import Path

LAYOUT = "project-folder@3"
COLLECTION_PATHS = {
    "inputs": "data/inputs",
    "raw_inputs": "data/raw_inputs",
    "raw_gtfs": "data/gtfs",
    "environment_sources": "data/environment_sources",
    "environments": "environment/prepared",
    "datasets": "results/datasets",
    "scenario_validations": "results/scenario_validations",
    "simulations": "results/simulations",
    "exposures": "results/exposures",
    "portfolios": "results/portfolios",
    "exports": "exports/packages",
}


def is_owned_store(root):
    marker = Path(root) / "ownership.json"
    return marker.is_file() and json.loads(marker.read_text()).get("layout") == LAYOUT


def project_directory(root, name=None):
    root = Path(root)
    return root.parent if is_owned_store(root) else root / name if name else root


def ownership_boundary(root):
    root = Path(root).resolve()
    return root.parent if is_owned_store(root) else root


def prepare_project_directory(directory):
    """Recreate internal aliases after a filesystem folder copy; never link externally."""
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    root = directory / ".system"
    root.mkdir(exist_ok=True)
    (root / "ownership.json").write_text(json.dumps({"layout": LAYOUT}) + "\n")
    for folder in (
        "data",
        "environment",
        "settings/revisions",
        "results/runs",
        "results/analyses",
        "exports",
        "notes",
    ):
        (directory / folder).mkdir(parents=True, exist_ok=True)
    for collection, relative in COLLECTION_PATHS.items():
        destination = directory / relative
        destination.mkdir(parents=True, exist_ok=True)
        alias = root / collection
        target = os.path.relpath(destination, root)
        if alias.is_symlink():
            if os.readlink(alias) != target:
                raise ValueError(f"Project collection alias is unsafe: {collection}")
        elif alias.exists():
            raise ValueError(f"Project collection conflicts with private storage: {collection}")
        else:
            alias.symlink_to(target, target_is_directory=True)
        if not alias.resolve().is_relative_to(directory):
            raise ValueError("Project collection escapes its owning folder")
    return root


def is_managed_alias(path, root):
    """Only exact, application-created collection aliases may be traversed."""
    path, root = Path(path), Path(root)
    return (
        is_owned_store(root)
        and path.parent == root
        and path.name in COLLECTION_PATHS
        and path.is_symlink()
        and path.resolve() == root.parent / COLLECTION_PATHS[path.name]
    )
