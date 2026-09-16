"""Argparse adapter for the synchronous M07 application use cases."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from mobile_sensing.application import (
    HeadlessApplication,
    LausanneSmokeConfig,
    LocalCatalogConfig,
    ScenarioResourceBundle,
    run_lausanne_smoke,
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
)
from mobile_sensing.datasets import (
    AreaAssignmentMapping,
    DemandImportMapping,
    GTFSReconstructionConfig,
    RateImportMapping,
    TabularSource,
    VehicleImportMapping,
    discover_gtfs_directory,
    load_registered_tabular_source,
)
from mobile_sensing.environment import PreparedEnvironmentReader
from mobile_sensing.portfolio import PortfolioResourceLimits, UtilityWeightResource


def _json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _validated_json(model, path: str | Path):
    return model.model_validate_json(Path(path).read_bytes())


def _environment(artifact_root: Path, path: str | Path):
    reference = _validated_json(EnvironmentArtifactRef, path)
    return PreparedEnvironmentReader(artifact_root).read(reference)


def _registered_source(artifact_root: Path, dataset_id: str) -> TabularSource:
    return load_registered_tabular_source(dataset_id, artifact_root=artifact_root)


def _emit(value: Any) -> None:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    print(canonical_json_text(value))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mobile-sensing")
    subparsers = parser.add_subparsers(dest="command", required=True)

    upload = subparsers.add_parser("register-upload")
    upload.add_argument("--artifact-root", required=True)
    upload.add_argument("--source", required=True)
    upload.add_argument("--provenance", required=True)

    environment = subparsers.add_parser("prepare-environment")
    environment.add_argument("--artifact-root", required=True)
    environment.add_argument("--catalog", required=True)
    environment.add_argument("--request", required=True)
    environment.add_argument("--config", required=True)

    demand = subparsers.add_parser("normalize-demand")
    demand.add_argument("--artifact-root", required=True)
    demand.add_argument("--environment", required=True)
    demand.add_argument("--source-id", required=True)
    demand.add_argument("--mapping", required=True)
    demand.add_argument("--routing-profile-id")

    supply = subparsers.add_parser("normalize-supply")
    supply.add_argument("--artifact-root", required=True)
    supply.add_argument("--environment", required=True)
    supply.add_argument("--source-id", required=True)
    supply.add_argument("--mapping", required=True)
    supply.add_argument("--area-source-id")
    supply.add_argument("--area-mapping")
    supply.add_argument("--known-area-id", action="append", default=[])
    supply.add_argument("--routing-profile-id")

    gtfs = subparsers.add_parser("reconstruct-gtfs")
    gtfs.add_argument("--artifact-root", required=True)
    gtfs.add_argument("--environment", required=True)
    gtfs.add_argument("--source", required=True)
    gtfs.add_argument("--config", required=True)

    validate = subparsers.add_parser("validate-scenario")
    validate.add_argument("--artifact-root", required=True)
    validate.add_argument("--environment", required=True)
    validate.add_argument("--resources", required=True)

    simulation = subparsers.add_parser("run-simulation")
    simulation.add_argument("--artifact-root", required=True)
    simulation.add_argument("--environment", required=True)
    simulation.add_argument("--resources", required=True)
    simulation.add_argument("--options", required=True)

    exposure = subparsers.add_parser("allocate-exposure")
    exposure.add_argument("--artifact-root", required=True)
    exposure.add_argument("--environment", required=True)
    exposure.add_argument("--simulation", required=True)
    exposure.add_argument("--config", required=True)

    portfolio_preview = subparsers.add_parser("preview-portfolios")
    portfolio_preview.add_argument("--artifact-root", required=True)
    portfolio_preview.add_argument("--exposure", required=True)
    portfolio_preview.add_argument("--config", required=True)
    portfolio_preview.add_argument("--limits")

    portfolio_samples = subparsers.add_parser("evaluate-portfolio-samples")
    portfolio_samples.add_argument("--artifact-root", required=True)
    portfolio_samples.add_argument("--exposure", required=True)
    portfolio_samples.add_argument("--config", required=True)
    portfolio_samples.add_argument("--weights", required=True)
    portfolio_samples.add_argument("--limits")

    portfolio_analysis = subparsers.add_parser("summarize-portfolios")
    portfolio_analysis.add_argument("--artifact-root", required=True)
    portfolio_analysis.add_argument("--samples", required=True)
    portfolio_analysis.add_argument("--config", required=True)

    smoke = subparsers.add_parser("lausanne-smoke")
    smoke.add_argument("--artifact-root", required=True)
    smoke.add_argument("--data-root", required=True)
    smoke.add_argument("--config", required=True)
    smoke.add_argument("--report")

    api = subparsers.add_parser("serve-api")
    api.add_argument("--artifact-root", required=True)
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--port", type=int, default=8000)

    launch = subparsers.add_parser("launch")
    launch.add_argument(
        "--artifact-root",
        default="project",
        help="Project workspace directory (default: ./project)",
    )
    launch.add_argument("--host", default="127.0.0.1")
    launch.add_argument("--port", type=int, default=8000)
    launch.add_argument("--workers", type=int, default=2)
    launch.add_argument("--no-browser", action="store_true")

    coordinator = subparsers.add_parser("run-coordinator")
    coordinator.add_argument("--artifact-root", required=True)
    coordinator.add_argument("--workers", type=int, default=2)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    artifact_root = Path(args.artifact_root).resolve()
    application = HeadlessApplication(artifact_root)

    if args.command == "serve-api":
        import uvicorn

        from mobile_sensing.api import create_app

        uvicorn.run(create_app(artifact_root), host=args.host, port=args.port)
        return 0
    if args.command == "launch":
        from mobile_sensing.launcher import launch_local_application

        launch_local_application(
            artifact_root,
            host=args.host,
            port=args.port,
            workers=args.workers,
            open_browser=not args.no_browser,
        )
        return 0
    if args.command == "run-coordinator":
        from mobile_sensing.jobs import LocalCoordinator

        with LocalCoordinator(artifact_root, max_workers=args.workers) as coordinator:
            try:
                coordinator.run_forever()
            except KeyboardInterrupt:
                coordinator.request_stop()
        return 0

    if args.command == "register-upload":
        source = application.register_upload(args.source, provenance=args.provenance)
        _emit(source.registration)
        return 0
    if args.command == "prepare-environment":
        catalog_path = Path(args.catalog).resolve()
        catalog = _validated_json(LocalCatalogConfig, catalog_path).build(
            relative_to=catalog_path.parent
        )
        reference = application.prepare_environment(
            catalog,
            _validated_json(EnvironmentProviderRequest, args.request),
            _validated_json(EnvironmentBuildConfig, args.config),
        )
        _emit(reference)
        return 0
    if args.command == "normalize-demand":
        environment = _environment(artifact_root, args.environment)
        source = _registered_source(artifact_root, args.source_id)
        payload = _json(args.mapping)
        mapping = (
            _validated_json(RateImportMapping, args.mapping)
            if payload.get("adapter") == "demand.upload_sparse_od_rate@1"
            else _validated_json(DemandImportMapping, args.mapping)
        )
        result = application.normalize_demand(
            source,
            mapping,
            environment,
            routing_profile_id=args.routing_profile_id,
        )
        _emit(
            {
                "artifact": result.reference.model_dump(mode="json"),
                "mapping_id": result.mapping_id,
                "task_count": len(result.tasks),
                "rate_count": len(result.rates),
                "issue_count": len(result.issues),
            }
        )
        return 0
    if args.command == "normalize-supply":
        environment = _environment(artifact_root, args.environment)
        area_source = (
            _registered_source(artifact_root, args.area_source_id) if args.area_source_id else None
        )
        area_mapping = (
            _validated_json(AreaAssignmentMapping, args.area_mapping) if args.area_mapping else None
        )
        result = application.normalize_supply(
            _registered_source(artifact_root, args.source_id),
            _validated_json(VehicleImportMapping, args.mapping),
            environment,
            routing_profile_id=args.routing_profile_id,
            area_assignment_source=area_source,
            area_assignment_mapping=area_mapping,
            known_area_ids=set(args.known_area_id) if args.known_area_id else None,
        )
        _emit(
            {
                "artifact": result.reference.model_dump(mode="json"),
                "mapping_id": result.mapping_id,
                "vehicle_count": len(result.vehicles),
            }
        )
        return 0
    if args.command == "reconstruct-gtfs":
        environment = _environment(artifact_root, args.environment)
        result = application.reconstruct_gtfs(
            discover_gtfs_directory(args.source),
            _validated_json(GTFSReconstructionConfig, args.config),
            environment,
        )
        _emit(
            {
                "artifact": result.reference.model_dump(mode="json"),
                "task_count": len(result.tasks),
                "vehicle_count": len(result.vehicles),
                "assignment_plan_id": result.assignments.assignment_plan_id,
                "diagnostics": result.diagnostics,
            }
        )
        return 0
    if args.command in {"validate-scenario", "run-simulation"}:
        environment = _environment(artifact_root, args.environment)
        resources = _validated_json(ScenarioResourceBundle, args.resources)
        validated = application.validate_scenario(environment, resources)
        if args.command == "validate-scenario":
            _emit(
                {
                    "artifact": validated.reference.model_dump(mode="json"),
                    "scenario_hash": validated.scenario_hash,
                    "replication_count": len(validated.replications),
                    "vehicle_count": len(validated.vehicle_specs),
                    "task_count_by_replication": {
                        item.replication_id: len(item.tasks) for item in validated.replications
                    },
                    "assumptions": validated.assumptions,
                    "impact_summaries": validated.impact_summaries,
                    "estimated_working_bytes": validated.estimated_working_bytes,
                }
            )
            return 0
        result = application.run_simulation(
            validated,
            _validated_json(ExecutionOptions, args.options),
        )
        _emit(
            {
                "artifact": result.reference.model_dump(mode="json"),
                "replication_count": len(result.results),
                "elapsed_s": result.elapsed_s,
                "estimated_working_bytes": result.estimated_working_bytes,
            }
        )
        return 0
    if args.command == "allocate-exposure":
        environment = _environment(artifact_root, args.environment)
        simulation = _validated_json(ArtifactRef, args.simulation)
        reference = application.allocate_exposure(
            environment,
            simulation,
            _validated_json(ExposureConfig, args.config),
        )
        _emit(reference)
        return 0
    if args.command in {"preview-portfolios", "evaluate-portfolio-samples"}:
        exposure = _validated_json(ArtifactRef, args.exposure)
        config = _validated_json(PortfolioConfig, args.config)
        limits = _validated_json(PortfolioResourceLimits, args.limits) if args.limits else None
        if args.command == "preview-portfolios":
            _emit(application.preview_portfolios(exposure, config, limits=limits))
            return 0
        result = application.evaluate_portfolio_samples(
            exposure,
            config,
            _validated_json(UtilityWeightResource, args.weights),
            limits=limits,
        )
        _emit(
            {
                "artifact": result.reference.model_dump(mode="json"),
                "replications_R": result.preview.replications_R,
                "sampling_rounds_J": result.sampling_rounds_J,
                "count_portfolios_P": result.count_portfolios,
                "portfolio_samples": result.portfolio_samples,
                "unique_sample_matrices": result.unique_sample_matrices,
            }
        )
        return 0
    if args.command == "summarize-portfolios":
        result = application.summarize_portfolios(
            _validated_json(ArtifactRef, args.samples),
            _validated_json(PortfolioConfig, args.config),
        )
        _emit(
            {
                "artifact": result.reference.model_dump(mode="json"),
                "sample_artifact": result.sample_reference.model_dump(mode="json"),
                "replications_R": result.replications_R,
                "sampling_rounds_J": result.sampling_rounds_J,
                "count_portfolios_P": result.count_portfolios,
                "budget_levels": result.budget_levels,
                "frontier_memberships": result.frontier_memberships,
                "sensing_statistic_rows": result.sensing_statistic_rows,
            }
        )
        return 0
    if args.command == "lausanne-smoke":
        report = run_lausanne_smoke(
            artifact_root=artifact_root,
            data_root=args.data_root,
            config=_validated_json(LausanneSmokeConfig, args.config),
        )
        if args.report:
            report_path = Path(args.report).resolve()
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(canonical_json_text(report) + "\n", encoding="utf-8")
        _emit(report)
        return 0
    raise AssertionError("unreachable CLI command")


if __name__ == "__main__":
    raise SystemExit(main())
