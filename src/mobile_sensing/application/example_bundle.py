"""Checksummed offline example bundles and cancellable first-time installation."""

import json
import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Literal
from pydantic import Field

from mobile_sensing.application.example_build import (
    EXAMPLE_NAME,
    digest_file,
    feasible_portfolio_count,
)
from mobile_sensing.application.project_models import ProjectConfig
from mobile_sensing.application.run_pipeline import read_named_record
from mobile_sensing.contracts import ContractModel, canonical_json_text, stable_id
from mobile_sensing.jobs.models import ApiModel


class BundleFile(ContractModel):
    path: str
    sha256: str
    size_bytes: int = Field(ge=0)


class ExampleBundle(ContractModel):
    version: Literal["lausanne-example@1", "city-example@1"] = "lausanne-example@1"
    example_key: Literal["lausanne", "san-francisco"] = "lausanne"
    bundle_id: str
    name: str
    config: ProjectConfig
    files: tuple[BundleFile, ...]
    saved_views: dict[str, str]
    description: str


class ExampleInfo(ApiModel):
    name: str = EXAMPLE_NAME
    available: bool
    installed: bool = False
    bundle_id: str | None = None
    archive_bytes: int = 0
    description: str
    results_path: str = "/results?view=operations"


class ExampleOpenRequest(ApiModel):
    editable: bool = False


class ExampleOpenResponse(ApiModel):
    project_id: str
    job_id: str | None = None


def bundle_directory():
    return Path(
        os.environ.get(
            "MOBILE_SENSING_EXAMPLE_DIRECTORY", Path(__file__).resolve().parents[1] / "_examples"
        )
    )


def bundle_manifest(directory=None, example_key="lausanne"):
    if example_key not in {"lausanne", "san-francisco"}:
        raise ValueError("Unknown bundled example")
    folder = Path(directory) if directory else bundle_directory()
    return ExampleBundle.model_validate_json((folder / f"{example_key}.json").read_bytes())


def bundle_installed(root, bundle):
    return (Path(root) / "example_installations" / f"{bundle.bundle_id}.json").is_file()


def example_info(root, example_key="lausanne"):
    if example_key not in {"lausanne", "san-francisco"}:
        raise ValueError("Unknown example key")
    folder = bundle_directory()
    if (
        not (folder / f"{example_key}.json").is_file()
        or not (folder / f"{example_key}.zip").is_file()
    ):
        return ExampleInfo(
            available=False,
            description="The complete offline example bundle is not installed with this distribution.",
        )
    bundle = bundle_manifest(folder, example_key=example_key)
    return ExampleInfo(
        name=bundle.name,
        available=True,
        installed=bundle_installed(root, bundle),
        bundle_id=bundle.bundle_id,
        archive_bytes=(folder / f"{example_key}.zip").stat().st_size,
        description=bundle.description,
        results_path=bundle.saved_views.get(
            "Fleet results", bundle.saved_views.get("Operations", "/results?view=fleet")
        ),
    )


def _valid_relative(path):
    value = Path(path)
    if value.is_absolute() or not value.parts or any(part in {"..", "."} for part in value.parts):
        raise ValueError("Example inventory contains an unsafe path")
    if value.parts[0] not in {
        "inputs",
        "environments",
        "datasets",
        "simulations",
        "exposures",
        "portfolios",
        "raw_gtfs",
        "run_cache",
        "analysis_cache",
        "example_evidence",
    }:
        raise ValueError("Example inventory contains an unsupported collection")
    return value


def install_example(
    root, *, cancellation, progress, directory=None, generate_reports=True, example_key="lausanne"
):
    """Worker-only byte installation; project metadata is finalized by the API."""
    root = Path(root)
    folder = Path(directory) if directory else bundle_directory()
    bundle = bundle_manifest(folder, example_key=example_key)
    if not bundle_installed(root, bundle):
        inventory = {item.path: item for item in bundle.files}
        if (
            len(inventory) != len(bundle.files)
            or len(inventory) > 50000
            or sum(item.size_bytes for item in bundle.files) > 8 * 1024**3
        ):
            raise ValueError("Example inventory exceeds supported bounds")
        for path in inventory:
            _valid_relative(path)
        stage_root = root / ".example_staging"
        stage_root.mkdir(parents=True, exist_ok=True)
        with (
            tempfile.TemporaryDirectory(dir=stage_root) as temporary,
            zipfile.ZipFile(folder / f"{example_key}.zip") as archive,
        ):
            if set(archive.namelist()) != set(inventory) or len(archive.namelist()) != len(
                inventory
            ):
                raise ValueError("Example archive disagrees with its exact inventory")
            stage = Path(temporary)
            for index, (name, entry) in enumerate(sorted(inventory.items())):
                cancellation.raise_if_cancelled()
                info = archive.getinfo(name)
                if (
                    info.file_size != entry.size_bytes
                    or (info.external_attr >> 16) & 0o170000 == 0o120000
                ):
                    raise ValueError("Example file size/type mismatch")
                target = stage / name
                target.parent.mkdir(parents=True, exist_ok=True)
                count = 0
                with archive.open(info) as source, target.open("wb") as output:
                    for chunk in iter(lambda: source.read(1024**2), b""):
                        cancellation.raise_if_cancelled()
                        count += len(chunk)
                        if count > entry.size_bytes:
                            raise ValueError("Example file exceeds its declared size")
                        output.write(chunk)
                if count != entry.size_bytes or digest_file(target) != entry.sha256:
                    raise ValueError(f"Example checksum mismatch: {name}")
                progress.update(
                    phase="example.verify_and_unpack", completed=index + 1, total=len(inventory)
                )
            # Publish complete immutable directories. Existing shared resources
            # must match the bundle byte inventory and are never overwritten.
            for manifest_path in stage.glob("*/*/manifest.json"):
                for table in json.loads(manifest_path.read_text())["tables"]:
                    if table["relative_path"].startswith("tables/"):
                        relative = manifest_path.parent.relative_to(stage) / table["relative_path"]
                        _valid_relative(str(relative))
                        (stage / relative).mkdir(parents=True, exist_ok=True)
            groups = sorted({Path(name).parts[:2] for name in inventory})
            for parts in groups:
                cancellation.raise_if_cancelled()
                relative = Path(*parts)
                source, destination = stage / relative, root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    names = [name for name in inventory if Path(name).parts[:2] == parts]
                    for name in names:
                        existing = root / name
                        matches = (
                            existing.is_file() and digest_file(existing) == inventory[name].sha256
                        )
                        if (
                            not matches
                            and existing.is_file()
                            and Path(name).name == "manifest.json"
                        ):
                            # Creation time is provenance, excluded from scientific identity.
                            # All other manifest fields and every payload byte must still match.
                            old = json.loads(existing.read_bytes())
                            new = json.loads((stage / name).read_bytes())
                            if "scientific_identity" in old and "scientific_identity" in new:
                                old.pop("created_at_utc", None)
                                new.pop("created_at_utc", None)
                                matches = old == new
                        if not matches:
                            raise ValueError(
                                f"Existing immutable resource conflicts with example: {name}"
                            )
                    if source.is_dir():
                        for empty in source.rglob("*"):
                            if empty.is_dir():
                                (destination / empty.relative_to(source)).mkdir(
                                    parents=True, exist_ok=True
                                )
                else:
                    os.replace(source, destination)
        for identifier in bundle.config.linked_run_ids:
            read_named_record(root, identifier, "studio_run")
        for identifier in bundle.config.linked_analysis_ids:
            read_named_record(root, identifier, "studio_analysis")
        receipt = root / "example_installations" / f"{bundle.bundle_id}.json"
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(
            canonical_json_text(
                {
                    "bundle_id": bundle.bundle_id,
                    "verified_files": len(bundle.files),
                    "complete": True,
                }
            )
        )
    if not generate_reports:
        return bundle
    from mobile_sensing.application.project_reports import build_report

    for kind, identifiers in (
        ("studio_run", bundle.config.linked_run_ids),
        ("studio_analysis", bundle.config.linked_analysis_ids),
    ):
        for index, identifier in enumerate(identifiers):
            progress.update(phase="example.summary_files", completed=index, total=len(identifiers))
            build_report(root, identifier, kind, cancellation)
    return bundle


def write_bundle(root, destination, *, example_key="lausanne", name=EXAMPLE_NAME, description=None):
    bundle_name = name
    """Package only real completed runs, their dependency closure and input files."""
    root, destination = Path(root), Path(destination)
    from mobile_sensing.application.run_models import RunView, AnalysisView

    run = RunView.model_validate_json((root / "example-run.json").read_bytes())
    analysis_paths = [root / "example-analysis.json"]
    optional_analysis = root / "example-analysis-std.json"
    if optional_analysis.is_file():
        analysis_paths.append(optional_analysis)
    analyses = tuple(AnalysisView.model_validate_json(path.read_bytes()) for path in analysis_paths)
    analysis = analyses[0]
    read_named_record(root, run.run_id, "studio_run")
    for item in analyses:
        read_named_record(root, item.analysis_id, "studio_analysis")
    expected_replications = 50 if example_key == "lausanne" else 10
    expected_sampling_runs = 200 if example_key == "lausanne" else 100
    if (
        run.replications != expected_replications
        or any(item.sampling_runs != expected_sampling_runs for item in analyses)
        or any(item.count_portfolios != feasible_portfolio_count(item.config) for item in analyses)
    ):
        raise ValueError("Example bundle has not completed the required R/J/count design")
    config = run.config.model_copy(
        update={
            "schema_version": "3.4",
            "portfolio": analysis.config,
            "linked_run_ids": (run.run_id,),
            "linked_analysis_ids": tuple(item.analysis_id for item in analyses),
        }
    )
    directories = set()
    pending = [
        run.artifact.artifact_id,
        *(item.artifact.artifact_id for item in analyses),
        config.prepared_environment.artifact.artifact_id,
        config.prepared_environment.features.artifact_id,
    ]
    collections = (
        "inputs",
        "environments",
        "datasets",
        "simulations",
        "exposures",
        "portfolios",
        "raw_gtfs",
    )
    while pending:
        identifier = pending.pop()
        matches = [
            root / collection / identifier
            for collection in collections
            if (root / collection / identifier).is_dir()
        ]
        if not matches:
            if identifier.startswith(
                (
                    "input_",
                    "dataset_",
                    "environment_",
                    "simulation_",
                    "exposure_",
                    "portfolio_",
                    "gtfs_source_",
                )
            ):
                raise ValueError(f"Example dependency missing: {identifier}")
            continue
        for directory in matches:
            if directory in directories:
                continue
            directories.add(directory)
            manifest = directory / "manifest.json"
            if manifest.is_file():
                pending.extend(
                    item["artifact_id"] for item in json.loads(manifest.read_text())["dependencies"]
                )
    # Inputs selected by the authoring editor are needed for editable copies,
    # including sources represented by local aliases in prepared artifacts.
    serialized = config.model_dump(mode="json")

    def inputs(value):
        if isinstance(value, str) and value.startswith("input_"):
            directories.add(root / "inputs" / value)
        elif isinstance(value, dict):
            for item in value.values():
                inputs(item)
        elif isinstance(value, list):
            for item in value:
                inputs(item)

    inputs(serialized)
    # Rebinning an editable copy must find the original completed movements.
    # Include only cache entries whose simulation and realization are bundled.
    included_ids = {directory.name for directory in directories}
    from mobile_sensing.application.project_package import reusable_cache_files

    cache_files = list(reusable_cache_files(root, included_ids))
    evidence_names = [
        "input-provenance.json",
        "example-run.json",
        "example-analysis.json",
        "example-audit.json",
    ]
    if optional_analysis.is_file():
        evidence_names.insert(3, "example-analysis-std.json")
    evidence_names = tuple(evidence_names)
    for name in evidence_names:
        if not (root / name).is_file():
            raise ValueError(f"Required example evidence missing: {name}")
    evidence_key = stable_id(
        "evidence", {name: digest_file(root / name) for name in evidence_names}
    )
    evidence = root / "example_evidence" / evidence_key
    evidence.mkdir(parents=True, exist_ok=True)
    for name in evidence_names:
        target = evidence / name
        if target.exists():
            if digest_file(target) != digest_file(root / name):
                raise ValueError(f"Existing example evidence was modified: {name}")
        else:
            shutil.copyfile(root / name, target)
    directories.add(evidence)
    files = tuple(
        BundleFile(
            path=str(path.relative_to(root)),
            sha256=digest_file(path),
            size_bytes=path.stat().st_size,
        )
        for path in sorted(
            {path for directory in directories for path in directory.rglob("*")} | set(cache_files)
        )
        if path.is_file()
    )
    import pyarrow.parquet as pq

    analysis_folder = root / "portfolios" / analysis.frontier.artifact_id / "tables"
    budgets = pq.read_table(analysis_folder / "budget_levels").to_pylist()
    statistics = pq.read_table(analysis_folder / "portfolio_statistics").to_pylist()
    default_budget = max(budgets, key=lambda row: row["budget_minor"])
    default_point = min(
        (row for row in statistics if row["total_cost_minor"] <= default_budget["budget_minor"]),
        key=lambda row: (-row["utility_mean"], row["portfolio_id"]),
    )
    budget_ids = ",".join(
        row["budget_id"] for row in sorted(budgets, key=lambda row: row["budget_minor"])
    )
    views = {
        "Fleet results": f"/results?view=fleet&fleet_run={run.run_id}",
        "Portfolio": f"/results?view=portfolio&resource={analysis.frontier.artifact_id}&budgets={budget_ids}&portfolio={default_point['portfolio_id']}",
    }
    for item in analyses[1:]:
        views["Portfolio — standard deviation"] = (
            f"/results?view=portfolio&resource={item.frontier.artifact_id}"
        )
    identity = stable_id(
        "example",
        {
            "config": config,
            "files": files,
            "saved_views": views,
            "version": "city-example@1",
            "example_key": example_key,
        },
    )
    bundle = ExampleBundle(
        bundle_id=identity,
        name=bundle_name,
        example_key=example_key,
        version="lausanne-example@1" if example_key == "lausanne" else "city-example@1",
        config=config,
        files=files,
        saved_views=views,
        description=description
        or f"Full Lausanne region · 14 January 2026 · 00:00–24:00 · {config.simulation.temporal_resolution_minutes:g}-minute reporting · {(config.portfolio.utility_temporal_resolution_minutes or config.simulation.temporal_resolution_minutes):g}-minute utility interval · Bus, Postal and Taxi · {run.replications} joint replications · {analysis.sampling_runs} fleet sampling runs. Demand, duties, depot, speeds and costs carry explicit demonstration assumptions.",
    )
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        destination / f"{example_key}.zip", "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1
    ) as archive:
        for item in files:
            archive.write(root / item.path, item.path)
    (destination / f"{example_key}.json").write_text(bundle.model_dump_json(indent=2) + "\n")
    return bundle
