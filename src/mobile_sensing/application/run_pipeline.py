"""Composite worker workflows with immutable named sources and mobility reuse."""

import json
import math
import os
import time
from decimal import Decimal
from pathlib import Path
import pandas as pd

from mobile_sensing.application.project_models import ProjectConfig, PortfolioEditor
from mobile_sensing.application.run_models import RunView, AnalysisView, RunOptions
from mobile_sensing.application.project_resolution import (
    PROJECT_RESOLUTION_VERSION,
    resolve_project,
)
from mobile_sensing.application.resource_tables import publish_tables, read_table
from mobile_sensing.application.services import HeadlessApplication
from mobile_sensing.application.civil_time import civil_clock
from mobile_sensing.contracts import (
    ArtifactRef,
    ArtifactDependency,
    ExposureConfig,
    ExecutionOptions,
    PortfolioConfig,
    scientific_hash,
    canonical_json_text,
)
from mobile_sensing.artifacts import verify_partitioned_artifact
from mobile_sensing.environment import PreparedEnvironmentReader
from mobile_sensing.exposure import ExposureArtifactReader
from mobile_sensing.simulation import (
    NEAREST_MATCHING_VERSION,
    PREDEFINED_DISPATCH_VERSION,
    RANDOM_CRUISE_VERSION,
    SimulationArtifactReader,
)
from mobile_sensing.simulation.kernel import EVENT_KERNEL_VERSION
from mobile_sensing.simulation.storage import SIMULATION_STORAGE_VERSION
from mobile_sensing.simulation.one_shot import ONE_SHOT_PLANNER_VERSION
from mobile_sensing.portfolio.models import (
    UtilityWeightResource,
    CellWeight,
    PortfolioResourceLimits,
)
from mobile_sensing.portfolio.storage import publish_portfolio_samples
from mobile_sensing.portfolio.analysis_storage import publish_portfolio_analysis


def mobility_projection(config):
    value = config.model_dump(mode="json")
    for field in (
        "portfolio",
        "source_revision",
        "linked_run_ids",
        "linked_analysis_ids",
        "example_bundle_id",
        "read_only",
        "saved_views",
    ):
        value.pop(field, None)
    value["schema_version"] = "3.0"
    simulation = value["simulation"]
    simulation.pop("temporal_resolution_minutes")
    # Selection metadata does not alter an already resolved calendar day.
    for field in ("weekday", "calendar_period_start", "calendar_period_end"):
        simulation.pop(field, None)
    if simulation.get("time_mode") in ("calendar", "weekday"):
        simulation.pop("time_mode")
    if not simulation.get("warmup_hours"):
        simulation.pop("warmup_hours", None)
    if not simulation.get("timezone"):
        simulation.pop("timezone", None)
    for fleet in value["fleets"]:
        fleet.pop("name")
        fleet.pop("sensing_movements")
        fleet.pop("sensing_mode", None)
        fleet["supply"].pop("timetable_idle_break_minutes", None)
        if fleet["demand"].get("temporal_mode") == "window":
            for field in ("temporal_mode", "time_profile", "time_profile_input"):
                fleet["demand"].pop(field, None)
        if not fleet["supply"].get("shift_groups"):
            fleet["supply"].pop("shift_groups", None)
    return value


def _dependency(role, reference):
    return ArtifactDependency(
        role=role,
        artifact_id=reference.artifact_id,
        content_hash=reference.content_hash,
    )


def _ref(value):
    return ArtifactRef.model_validate_json(json.dumps(value))


def _named_record(root, kind, content, dependencies, elapsed):
    reference = publish_tables(
        root,
        config={"record_kind": kind, **content},
        dependencies=dependencies,
        algorithm=f"{kind}@3",
        frames={
            "stage_metrics": pd.DataFrame(
                [{"stage": key, "seconds": value} for key, value in elapsed.items()]
            )
        },
        keys={"stage_metrics": ("stage",)},
    )
    return reference


def read_named_record(root, reference_id, kind):
    # IDs must resolve inside this one managed collection.
    if Path(reference_id).name != reference_id:
        raise ValueError("Invalid named record ID")
    path = Path(root) / "datasets" / reference_id / "manifest.json"
    if not path.is_file():
        raise KeyError(reference_id)
    from mobile_sensing.contracts import ArtifactManifest

    manifest = ArtifactManifest.model_validate_json(path.read_bytes())
    ref = ArtifactRef(
        artifact_id=manifest.artifact_id,
        artifact_kind="dataset",
        content_hash=manifest.content_fingerprint,
    )
    artifact = verify_partitioned_artifact(artifact_root=root, collection="datasets", reference=ref)
    value = dict(artifact.manifest.scientific_identity.resolved_config)
    if value.pop("record_kind", None) != kind:
        raise ValueError("Named record has the wrong result kind")
    metrics = read_table(root, ref, "stage_metrics")
    value.update(
        artifact=ref.model_dump(mode="json"),
        elapsed_seconds=dict(zip(metrics.stage, metrics.seconds)),
    )
    value["run_id" if kind == "studio_run" else "analysis_id"] = ref.artifact_id
    model = RunView if kind == "studio_run" else AnalysisView
    return model.model_validate_json(json.dumps(value))


def run_project(
    root,
    config,
    *,
    name,
    source_revision_id,
    options,
    cancellation,
    progress,
    replay_from=None,
    resolution_reference=None,
):
    root = Path(root)
    config = ProjectConfig.model_validate(config)
    from mobile_sensing.application.calendar_authoring import resolve_calendar

    config, calendar = resolve_calendar(root, config)
    options = RunOptions.model_validate(options)
    if config.prepared_environment is None:
        raise ValueError("Prepare the shared environment before running")
    started = time.perf_counter()
    cache_key = scientific_hash(
        {
            "mobility": mobility_projection(config),
            "kernel": EVENT_KERNEL_VERSION,
            "storage": SIMULATION_STORAGE_VERSION,
            "authoring": PROJECT_RESOLUTION_VERSION,
            "dispatch": {
                "predefined": PREDEFINED_DISPATCH_VERSION,
                "nearest": NEAREST_MATCHING_VERSION,
            },
            **(
                {"random_cruise": RANDOM_CRUISE_VERSION}
                if any(fleet.supply.post_service == "random_cruise" for fleet in config.fleets)
                else {}
            ),
            **(
                {"one_shot": ONE_SHOT_PLANNER_VERSION}
                if any(fleet.dispatch.mode == "one_shot" for fleet in config.fleets)
                else {}
            ),
            **({"replay_source": replay_from.run_id} if replay_from else {}),
        }
    )
    cache_path = root / "run_cache" / f"{cache_key}.json"
    elapsed = {}
    reused = cache_path.is_file()
    application = HeadlessApplication(root)
    if reused:
        cached = json.loads(cache_path.read_text())
        simulation = _ref(cached["simulation"])
        SimulationArtifactReader(root, simulation)
        resolution = _ref(cached["resolution"])
        verify_partitioned_artifact(artifact_root=root, collection="datasets", reference=resolution)
        progress.update(phase="simulation.reuse_completed_movement", completed=1, total=1)
        elapsed["resolution_and_mobility"] = 0.0
    else:
        if resolution_reference is not None:
            from mobile_sensing.application.replay import replay_project

            resolved = replay_project(
                root,
                config,
                source_reference=resolution_reference,
                cancellation=cancellation,
                progress=progress,
            )
        elif replay_from is None:
            resolved = resolve_project(
                root,
                config,
                cancellation=cancellation,
                progress=progress,
                workers=options.workers,
                memory_limit_bytes=options.memory_limit_bytes,
            )
        else:
            from mobile_sensing.application.replay import replay_project

            resolved = replay_project(
                root, config, replay_from, cancellation=cancellation, progress=progress
            )
        resolution = resolved.validated.reference
        elapsed["resolution"] = time.perf_counter() - started
        phase = time.perf_counter()
        executed = application.run_simulation(
            resolved.validated,
            ExecutionOptions(
                workers=options.workers, memory_limit_bytes=options.memory_limit_bytes
            ),
            cancellation=cancellation,
            progress=progress,
        )
        simulation = executed.reference
        elapsed["simulation"] = time.perf_counter() - phase
        cached = {
            "simulation": simulation.model_dump(mode="json"),
            "resolution": resolution.model_dump(mode="json"),
            "vehicle_counts": {
                fleet.fleet_id: sum(
                    spec.key.fleet_id == fleet.fleet_id for spec in resolved.validated.vehicle_specs
                )
                for fleet in config.fleets
            },
            "task_counts": {
                fleet.fleet_id: [
                    sum(
                        task.fleet_id == fleet.fleet_id and task.kind == "service"
                        for task in rep.tasks
                    )
                    for rep in resolved.validated.replications
                ]
                for fleet in config.fleets
            },
            "assumptions": list(resolved.validated.assumptions),
        }
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache_path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(canonical_json_text(cached))
        os.replace(temporary, cache_path)
    cancellation.raise_if_cancelled()
    environment = PreparedEnvironmentReader(root).read(config.prepared_environment.artifact)
    clock = civil_clock(config.simulation, config.prepared_environment.timezone)
    width = config.simulation.temporal_resolution_minutes * 60
    bins = math.ceil((clock.end_s - clock.observation_start_s) / width)
    if bins > 10000:
        raise ValueError("Temporal resolution exceeds the 10,000 reporting-bin limit")
    edges = tuple([clock.observation_start_s + i * width for i in range(bins)] + [clock.end_s])
    exposure_config = ExposureConfig(
        schema_version="2.0",
        simulation_id=simulation.artifact_id,
        sensing_geometry_id=environment.metadata.sensing_hash,
        bin_edges_s=edges,
        active_movement_kinds=(),
        fleet_movement_kinds={
            fleet.fleet_id: (
                (
                    "service_pickup",
                    "service_inter_step",
                    "cruise",
                    "depot_return",
                    "reposition",
                )
                if fleet.sensing_mode == "operating_duration"
                else fleet.sensing_movements
            )
            for fleet in config.fleets
        },
        measurement=(
            "operating_duration"
            if any(f.sensing_mode == "operating_duration" for f in config.fleets)
            else "movement_duration"
        ),
        activity_source_ref=(
            resolution
            if any(f.sensing_mode == "operating_duration" for f in config.fleets)
            else None
        ),
        operating_fleet_ids=tuple(
            f.fleet_id for f in config.fleets if f.sensing_mode == "operating_duration"
        ),
        fleet_idle_break_seconds={
            f.fleet_id: f.supply.timetable_idle_break_minutes * 60
            for f in config.fleets
            if f.sensing_mode == "operating_duration"
            and f.supply.source == "timetable"
            and f.supply.timetable_idle_break_minutes is not None
        },
    )
    phase = time.perf_counter()
    progress.update(phase="exposure.allocating_activity", completed=0, total=1)
    exposure = application.allocate_exposure(environment, simulation, exposure_config)
    elapsed["exposure"] = time.perf_counter() - phase
    cancellation.raise_if_cancelled()
    elapsed["total"] = time.perf_counter() - started
    content = {
        "name": name,
        "source_revision_id": source_revision_id,
        "config": config.model_dump(mode="json"),
        "simulation": simulation.model_dump(mode="json"),
        "exposure": exposure.model_dump(mode="json"),
        "resolution": resolution.model_dump(mode="json"),
        "replications": config.simulation.replications,
        "vehicle_counts": cached["vehicle_counts"],
        "task_counts": cached["task_counts"],
        "assumptions": cached["assumptions"],
        "mobility_reused": reused,
        "realization_source_run_id": replay_from.run_id if replay_from else None,
    }
    ref = _named_record(
        root,
        "studio_run",
        content,
        [
            _dependency("simulation", simulation),
            _dependency("exposure", exposure),
            _dependency("resolution", resolution),
        ],
        elapsed,
    )
    return read_named_record(root, ref.artifact_id, "studio_run")


def portfolio_inputs(root, run, editor):
    if {fleet.fleet_id for fleet in editor.fleets} != set(run.vehicle_counts):
        raise ValueError("Sensor-count settings must cover the source run's exact physical fleets")
    decimals = [
        Decimal(str(value))
        for value in [*editor.budgets, *(fleet.unit_cost for fleet in editor.fleets)]
    ]
    precision = max([0] + [-value.as_tuple().exponent for value in decimals])
    if precision > 6:
        raise ValueError("Costs and budgets support at most six decimal places")
    scale = 10**precision
    if any(value < 0 or value * scale > 2**63 - 1 for value in decimals):
        raise ValueError("Cost or budget is outside the supported nonnegative range")
    if editor.spatial_weight == "uniform":
        weights = UtilityWeightResource(
            weights_id="uniform",
            kind="uniform_spatial_duration_temporal",
            provenance="Explicit uniform spatial weights and reporting-bin duration weights",
        )
    else:
        features = read_table(root, run.config.prepared_environment.features, "grid_features")
        exposure_cells = set(ExposureArtifactReader(root).axes(run.exposure)["cell_ids"])
        selected = features.loc[
            (features.feature == editor.spatial_weight)
            & features.cell_id.astype(str).isin(exposure_cells)
        ].sort_values("cell_id")
        if selected.empty:
            raise ValueError(
                "Selected utility weight feature is absent from the source run environment"
            )
        weights = UtilityWeightResource(
            weights_id=editor.spatial_weight,
            kind="spatial_duration_temporal",
            spatial_values=tuple(
                CellWeight(cell_id=row.cell_id, raw_weight=float(row.value))
                for row in selected.itertuples()
            ),
            provenance=f"Prepared feature {editor.spatial_weight}; source {run.config.prepared_environment.features.artifact_id}; temporal weights equal reporting-bin durations",
        )
    config = PortfolioConfig.model_validate_json(
        json.dumps(
            {
                "schema_version": "2.0",
                "exposure_id": run.exposure.artifact_id,
                "utility": {
                    "kind": (
                        "exponential_saturation"
                        if editor.utility == "exponential"
                        else editor.utility
                    ),
                    "saturation_s": editor.saturation_minutes * 60,
                    "weights_ref": weights.weights_id,
                    "temporal_interval_s": (
                        editor.utility_temporal_resolution_minutes
                        or run.config.simulation.temporal_resolution_minutes
                    )
                    * 60,
                },
                "count_enumeration": {
                    "fleets": {
                        fleet.fleet_id: {"count_levels": list(fleet.counts)}
                        for fleet in editor.fleets
                    }
                },
                "budgets": {
                    "levels_minor": sorted(
                        set(int(Decimal(str(value)) * scale) for value in editor.budgets)
                    )
                },
                "costs": {
                    "unit": editor.cost_unit,
                    "minor_unit_scale": scale,
                    "by_fleet_minor": {
                        fleet.fleet_id: int(Decimal(str(fleet.unit_cost)) * scale)
                        for fleet in editor.fleets
                    },
                },
                "sampling_rounds": editor.sampling_runs,
                "sampling_seed": editor.seed,
                "sampling_design": "joint_replication_uniform_vehicle",
                "comparison_resolution": {},
                "sample_matrix_storage": "reconstruct",
                "sensing_statistics_mode": "on_demand",
                "risk_metric": editor.risk_metric,
            }
        )
    )
    return config, weights


def run_analysis(root, editor, *, name, source_revision_id, options, cancellation, progress):
    from pydantic import TypeAdapter
    from mobile_sensing.portfolio.evaluation import (
        build_portfolio_plan,
        PORTFOLIO_UTILITY_VERSION,
    )
    from mobile_sensing.portfolio.storage import PortfolioArtifactReader
    from mobile_sensing.portfolio.models import (
        PortfolioEvaluationResult,
        PortfolioAnalysisResult,
    )
    from mobile_sensing.portfolio.analysis_storage import (
        PortfolioAnalysisArtifactReader,
        PORTFOLIO_ANALYSIS_STORAGE_VERSION,
    )

    root = Path(root)
    evaluation_codec = TypeAdapter(PortfolioEvaluationResult)
    analysis_codec = TypeAdapter(PortfolioAnalysisResult)
    cancellation.raise_if_cancelled()
    editor = PortfolioEditor.model_validate(editor)
    if not editor.source_run_id:
        raise ValueError("Select a completed simulation run")
    run = read_named_record(root, editor.source_run_id, "studio_run")
    from mobile_sensing.application.temporal_authoring import resolve_portfolio

    editor = resolve_portfolio(editor, run.vehicle_counts)
    config, weights = portfolio_inputs(root, run, editor)
    started = time.perf_counter()
    limits = PortfolioResourceLimits(max_working_bytes=options.memory_limit_bytes)
    plan = build_portfolio_plan(
        artifact_root=root, exposure=run.exposure, config=config, limits=limits
    )
    if plan.preview.blocked:
        raise ValueError("Portfolio analysis blocked: " + "; ".join(plan.preview.blocking_reasons))
    sample_key = scientific_hash(
        {
            "exposure": run.exposure,
            "weights": weights,
            "config": config.model_dump(
                mode="json",
                exclude={
                    "budgets",
                    "costs",
                    "comparison_resolution",
                    "risk_metric",
                    "sensing_statistics_mode",
                },
            ),
            "algorithm": PORTFOLIO_UTILITY_VERSION,
        }
    )
    cache = root / "analysis_cache"
    cache.mkdir(exist_ok=True)
    sample_cache = cache / f"samples-{sample_key}.json"
    candidates = json.loads(sample_cache.read_text()) if sample_cache.is_file() else []
    required = {row.portfolio_id for row in plan.counts}
    samples = None
    for value in reversed(candidates):
        cancellation.raise_if_cancelled()
        candidate = evaluation_codec.validate_json(json.dumps(value))
        reader = PortfolioArtifactReader(root, candidate.reference)
        available = {row["portfolio_id"] for row in reader.read("portfolio_counts").to_pylist()}
        if required <= available:
            samples = candidate
            progress.update(phase="portfolio.reuse_sample_utilities", completed=1, total=1)
            break
    if samples is None:
        samples = publish_portfolio_samples(
            artifact_root=root,
            exposure=run.exposure,
            config=config,
            weights=weights,
            limits=limits,
            cancellation=cancellation,
            progress=progress,
        )
        _write_analysis_cache(
            sample_cache,
            [*candidates[-7:], evaluation_codec.dump_python(samples, mode="json")],
        )
    elapsed = {"sampling": time.perf_counter() - started}
    phase = time.perf_counter()
    frontier_key = scientific_hash(
        {
            "samples": samples.reference,
            "config": config,
            "storage": PORTFOLIO_ANALYSIS_STORAGE_VERSION,
        }
    )
    frontier_cache = cache / f"frontier-{frontier_key}.json"
    if frontier_cache.is_file():
        frontier = analysis_codec.validate_json(frontier_cache.read_bytes())
        PortfolioAnalysisArtifactReader(root, frontier.reference)
        progress.update(phase="portfolio.reuse_statistics", completed=1, total=1)
    else:
        frontier = publish_portfolio_analysis(
            artifact_root=root,
            samples=samples.reference,
            config=config,
            cancellation=cancellation,
            progress=progress,
        )
        _write_analysis_cache(frontier_cache, analysis_codec.dump_python(frontier, mode="json"))
    elapsed["statistics"] = time.perf_counter() - phase
    elapsed["total"] = time.perf_counter() - started
    cancellation.raise_if_cancelled()
    content = {
        "name": name,
        "source_run_id": run.run_id,
        "source_revision_id": source_revision_id,
        "samples": samples.reference.model_dump(mode="json"),
        "frontier": frontier.reference.model_dump(mode="json"),
        "config": editor.model_dump(mode="json"),
        "replications": run.replications,
        "sampling_runs": editor.sampling_runs,
        "count_portfolios": len(plan.counts),
    }
    ref = _named_record(
        root,
        "studio_analysis",
        content,
        [
            _dependency("source_run", run.artifact),
            _dependency("samples", samples.reference),
            _dependency("frontier", frontier.reference),
        ],
        elapsed,
    )
    return read_named_record(root, ref.artifact_id, "studio_analysis")


def _write_analysis_cache(path, value):
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    os.replace(temporary, path)
