"""Spawn-safe scientific job entry point; it never writes SQLite metadata."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.parquet as pq

from mobile_sensing.application import (
    HeadlessApplication,
    LocalCatalogConfig,
    ScenarioResourceBundle,
)
from mobile_sensing.contracts import (
    ArtifactRef,
    EnvironmentArtifactRef,
    EnvironmentBuildConfig,
    EnvironmentProviderRequest,
    ExecutionOptions,
    ExposureConfig,
    PortfolioConfig,
    canonical_json_text,
    stable_id,
)
from mobile_sensing.datasets import (
    DemandImportMapping,
    GTFSReconstructionConfig,
    RateImportMapping,
    VehicleImportMapping,
    discover_gtfs_directory,
    load_registered_tabular_source,
)
from mobile_sensing.environment import PreparedEnvironmentReader
from mobile_sensing.portfolio import PortfolioResourceLimits, UtilityWeightResource


class JobCancelled(RuntimeError):
    pass


def initialize_worker_limits() -> None:
    """Prevent each spawned scientific worker from creating a BLAS thread pool."""

    for name in ("OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "OMP_NUM_THREADS"):
        os.environ[name] = "1"


class FileCancellationToken:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def is_cancelled(self) -> bool:
        return self.path.exists()

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled():
            raise JobCancelled("job cancellation requested")


class FileProgress:
    """Atomic, bounded progress handoff from a child to its coordinator."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def update(self, *, phase: str, completed: int, total: int | None) -> None:
        payload = canonical_json_text({"phase": phase, "completed": completed, "total": total})
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, self.path)


def _artifact_payload(reference: ArtifactRef, **values: Any) -> dict[str, Any]:
    return {
        "resource_id": reference.artifact_id,
        "artifact": reference.model_dump(mode="json"),
        **values,
    }


def _validated(model, value):
    """Validate a value after its durable JSON representation has been decoded."""

    return model.model_validate_json(canonical_json_text(value))


def _environment(root: Path, value: dict[str, Any]):
    return PreparedEnvironmentReader(root).read(_validated(EnvironmentArtifactRef, value))


def _export(root: Path, resource_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    sources = tuple((root / item).resolve() for item in payload["source_paths"])
    for source in sources:
        try:
            source.relative_to(root)
        except ValueError as exc:
            raise ValueError("export sources must be inside artifact_root") from exc
        if not source.is_file() or source.suffix != ".parquet":
            raise ValueError("export sources must be managed Parquet files")
    format_name = payload.get("format", "parquet")
    if format_name not in {"parquet", "csv"}:
        raise ValueError("export format must be parquet or csv")
    source_hashes = [hashlib.sha256(source.read_bytes()).hexdigest() for source in sources]
    export_id = stable_id("export", {"source_sha256": source_hashes, "format": format_name})
    target = root / "exports" / export_id
    if not target.exists():
        staging = root / ".staging" / f"{export_id}.{os.getpid()}"
        staging.mkdir(parents=True, exist_ok=False)
        output = staging / f"data.{format_name}"
        table = (
            pa.concat_tables([pq.read_table(source) for source in sources])
            if sources
            else pa.table({})
        )
        if format_name == "parquet":
            pq.write_table(table, output, compression="zstd")
        else:
            pacsv.write_csv(table, output)
        checksum = hashlib.sha256(output.read_bytes()).hexdigest()
        (staging / "export.json").write_text(
            canonical_json_text(
                {
                    "export_id": export_id,
                    "format": format_name,
                    "file": output.name,
                    "sha256": checksum,
                    "source_paths": [str(source.relative_to(root)) for source in sources],
                }
            ),
            encoding="utf-8",
        )
        (staging / "_SUCCESS").write_text("complete\n", encoding="utf-8")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(staging, target)
        except FileExistsError:
            shutil.rmtree(staging)
    metadata = json.loads((target / "export.json").read_text(encoding="utf-8"))
    return {
        "resource_id": export_id,
        "export": metadata,
        "download_path": str((target / metadata["file"]).relative_to(root)),
        "requested_resource_id": payload.get("source_resource_id", resource_id),
    }


def execute_job(
    artifact_root: str,
    resource_id: str,
    kind: str,
    payload: dict[str, Any],
    cancellation_path: str,
    progress_path: str,
) -> dict[str, Any]:
    """Execute one validated request. Only immutable artifacts/files are published here."""

    root = Path(artifact_root).resolve()
    application = HeadlessApplication(root)
    cancellation = FileCancellationToken(cancellation_path)
    progress = FileProgress(progress_path)
    cancellation.raise_if_cancelled()

    if kind in {"project_files", "project_export", "project_import"}:
        from mobile_sensing.application.project_package import export_project, import_project

        if kind == "project_import":
            return import_project(root, payload, cancellation, progress)
        return export_project(
            root,
            resource_id,
            payload["snapshot"],
            cancellation,
            progress,
            archive=kind == "project_export",
        )

    if kind == "example_import":
        from mobile_sensing.application.example_bundle import install_example

        bundle = install_example(
            root,
            cancellation=cancellation,
            progress=progress,
            example_key=payload.get("example_key", "lausanne"),
        )
        if bundle.bundle_id != payload["bundle_id"]:
            raise ValueError("The bundled example changed while the import was queued")
        return {
            "bundle_id": bundle.bundle_id,
            "editable": payload["editable"],
            "config": bundle.config.model_dump(mode="json"),
            "saved_views": bundle.saved_views,
        }

    if kind in {"studio_run", "studio_analysis"}:
        from mobile_sensing.application.project_models import ProjectConfig, PortfolioEditor
        from mobile_sensing.application.run_models import RunOptions
        from mobile_sensing.application.run_pipeline import run_project, run_analysis

        operation = run_project if kind == "studio_run" else run_analysis
        model = ProjectConfig if kind == "studio_run" else PortfolioEditor
        result = operation(
            root,
            _validated(model, payload["config"]),
            name=payload["name"],
            source_revision_id=payload.get("source_revision_id"),
            options=_validated(RunOptions, payload.get("options", {})),
            cancellation=cancellation,
            progress=progress,
        )
        from mobile_sensing.application.project_reports import build_report

        build_report(
            root, result.run_id if kind == "studio_run" else result.analysis_id, kind, cancellation
        )
        return result.model_dump(mode="json")

    if kind == "region_search":
        from mobile_sensing.application.region_search import search_region

        return search_region(root, payload["query"], cancellation, progress).model_dump(mode="json")

    if kind == "studio_resolve":
        from mobile_sensing.application.configuration_check import check_configuration
        from mobile_sensing.application.project_models import ProjectConfig

        return check_configuration(
            root,
            _validated(ProjectConfig, payload["config"]),
            cancellation=cancellation,
            progress=progress,
        ).model_dump(mode="json")

    if kind == "studio_environment":
        from mobile_sensing.application.environment_editor import build_environment
        from mobile_sensing.application.studio_models import EnvironmentEditor

        result = build_environment(
            root,
            _validated(EnvironmentEditor, payload["config"]),
            application=application,
            cancellation=cancellation,
            progress=progress,
        )
        return result.model_dump(mode="json")

    if kind == "test_probe":
        steps = int(payload.get("steps", 1))
        delay_s = float(payload.get("delay_s", 0.0))
        if steps < 1 or delay_s < 0:
            raise ValueError("test probe requires positive steps and nonnegative delay")
        accumulator = 0
        for index in range(steps):
            cancellation.raise_if_cancelled()
            until = time.monotonic() + delay_s
            while time.monotonic() < until:
                accumulator = (accumulator * 1664525 + index + 1013904223) & 0xFFFFFFFF
            progress.update(phase="test_probe", completed=index + 1, total=steps)
        if payload.get("crash"):
            os._exit(73)
        return {"resource_id": resource_id, "value": accumulator, "steps": steps}

    if kind == "environment":
        catalog = _validated(LocalCatalogConfig, payload["catalog"]).build(
            relative_to=payload.get("catalog_relative_to")
        )
        reference = application.prepare_environment(
            catalog,
            _validated(EnvironmentProviderRequest, payload["request"]),
            _validated(EnvironmentBuildConfig, payload["config"]),
            cancellation=cancellation,
            progress=progress,
        )
        return _artifact_payload(reference)

    if kind == "gtfs_reconstruction":
        result = application.reconstruct_gtfs(
            discover_gtfs_directory(payload["source_directory"]),
            _validated(GTFSReconstructionConfig, payload["config"]),
            _environment(root, payload["environment"]),
        )
        return _artifact_payload(
            result.reference,
            task_count=len(result.tasks),
            vehicle_count=len(result.vehicles),
            assignment_plan_id=result.assignments.assignment_plan_id,
        )

    if kind in {"scenario_validation", "simulation"}:
        environment = _environment(root, payload["environment"])
        validated = application.validate_scenario(
            environment, _validated(ScenarioResourceBundle, payload["resources"])
        )
        if kind == "scenario_validation":
            return _artifact_payload(
                validated.reference,
                replications_R=len(validated.replications),
                vehicle_count=len(validated.vehicle_specs),
            )
        options = _validated(ExecutionOptions, payload["options"])
        if options.job_timeout_s is not None:
            options = options.model_copy(update={"job_timeout_s": None})
        result = application.run_simulation(
            validated,
            options,
            cancellation=cancellation,
            progress=progress,
        )
        return _artifact_payload(result.reference, replications_R=len(result.results))

    if kind == "exposure":
        reference = application.allocate_exposure(
            _environment(root, payload["environment"]),
            _validated(ArtifactRef, payload["simulation"]),
            _validated(ExposureConfig, payload["config"]),
        )
        return _artifact_payload(reference)

    if kind == "portfolio":
        mode = payload.get("mode", "analysis")
        config = _validated(PortfolioConfig, payload["config"])
        if mode == "samples":
            result = application.evaluate_portfolio_samples(
                _validated(ArtifactRef, payload["exposure"]),
                config,
                _validated(UtilityWeightResource, payload["weights"]),
                limits=(
                    _validated(PortfolioResourceLimits, payload["limits"])
                    if payload.get("limits")
                    else None
                ),
            )
            return _artifact_payload(
                result.reference,
                portfolio_stage="samples",
                replications_R=result.preview.replications_R,
                sampling_rounds_J=result.sampling_rounds_J,
            )
        if mode != "analysis":
            raise ValueError("portfolio mode must be samples or analysis")
        result = application.summarize_portfolios(
            _validated(ArtifactRef, payload["samples"]), config
        )
        return _artifact_payload(
            result.reference,
            portfolio_stage="analysis",
            replications_R=result.replications_R,
            sampling_rounds_J=result.sampling_rounds_J,
        )

    if kind == "import":
        environment = _environment(root, payload["environment"])
        source = load_registered_tabular_source(payload["source_id"], artifact_root=root)
        import_kind = payload["import_kind"]
        if import_kind == "demand":
            mapping_payload = payload["mapping"]
            model = (
                RateImportMapping
                if mapping_payload.get("adapter") == "demand.upload_sparse_od_rate@1"
                else DemandImportMapping
            )
            result = application.normalize_demand(
                source,
                _validated(model, mapping_payload),
                environment,
                routing_profile_id=payload.get("routing_profile_id"),
            )
        elif import_kind == "supply":
            result = application.normalize_supply(
                source,
                _validated(VehicleImportMapping, payload["mapping"]),
                environment,
                routing_profile_id=payload.get("routing_profile_id"),
            )
        else:
            raise ValueError("import_kind must be demand or supply")
        return _artifact_payload(result.reference, mapping_id=result.mapping_id)

    if kind == "export":
        return _export(root, resource_id, payload)

    raise ValueError(f"unsupported job kind: {kind}")
