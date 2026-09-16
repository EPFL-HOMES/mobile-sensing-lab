"""Build the deterministic real-backend workspace used by M11 browser acceptance."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from mobile_sensing.contracts import ExecutionOptions, ExposureConfig, PortfolioConfig
from mobile_sensing.jobs import JobStore
from mobile_sensing.portfolio import UtilityWeightResource
from tests.v2.test_m07_headless_application import _bundle, _prepared, _upload_inputs


def _completed_job(store, project_id, kind, operation, reference, **result_values):
    snapshot, _ = store.submit(
        kind=kind,
        operation=operation,
        payload={"acceptance_fixture": operation},
        project_id=project_id,
    )
    claimed, token = store.claim_next("m11-acceptance", 60.0)
    assert claimed.job_id == snapshot.job_id
    return store.complete(
        snapshot.job_id,
        token,
        {"artifact": reference.model_dump(mode="json"), **result_values},
    )


def build(root: Path) -> dict[str, object]:
    application, environment = _prepared(root)
    demand, supply, locations = _upload_inputs(application, environment, root)
    validated = application.validate_scenario(
        environment, _bundle(environment, demand, supply, locations)
    )
    simulation_result = application.run_simulation(
        validated,
        ExecutionOptions(
            workers=1,
            memory_limit_bytes=16 * 1024 * 1024,
            progress_frequency_events=2,
        ),
    )
    exposure = application.allocate_exposure(
        environment,
        simulation_result.reference,
        ExposureConfig(
            schema_version="2.0",
            simulation_id=simulation_result.reference.artifact_id,
            sensing_geometry_id=environment.metadata.sensing_hash,
            bin_edges_s=(0.0, 10.0, 20.0, 30.0),
            active_movement_kinds=("service_inter_step", "service_pickup"),
        ),
    )
    fleet_ids = tuple(sorted(fleet.fleet_id for fleet in validated.bundle.scenario.fleets))
    config = PortfolioConfig.model_validate(
        {
            "schema_version": "2.0",
            "exposure_id": exposure.artifact_id,
            "utility": {
                "kind": "exponential_saturation",
                "saturation_s": 10.0,
                "weights_ref": "m11_uniform_weights",
            },
            "count_enumeration": {
                "fleets": {fleet_id: {"count_levels": (0, 1)} for fleet_id in fleet_ids}
            },
            "budgets": {"levels_minor": (0, 10, 20, 30)},
            "costs": {
                "unit": "CHF",
                "minor_unit_scale": 100,
                "by_fleet_minor": {fleet_id: 10 for fleet_id in fleet_ids},
            },
            "sampling_rounds": 8,
            "sampling_seed": 991,
            "sampling_design": "joint_replication_uniform_vehicle",
            "comparison_resolution": {
                "mean_utility": 1e-12,
                "std_utility": 1e-12,
            },
        }
    )
    weights = UtilityWeightResource(
        weights_id="m11_uniform_weights",
        kind="uniform_spatial_duration_temporal",
        provenance="M11 deterministic browser acceptance",
    )
    samples = application.evaluate_portfolio_samples(exposure, config, weights).reference
    analysis = application.summarize_portfolios(samples, config).reference

    store = JobStore(application.artifact_root)
    project = store.create_project("M11 analytic acceptance", "Real bounded result workflow")
    revision = store.create_revision(
        project.project_id,
        None,
        {"schema_version": "m10-ui-draft@1"},
    )
    jobs = (
        _completed_job(
            store,
            project.project_id,
            "simulation",
            "m11-simulation",
            simulation_result.reference,
            replications_R=2,
        ),
        _completed_job(
            store,
            project.project_id,
            "exposure",
            "m11-exposure",
            exposure,
        ),
        _completed_job(
            store,
            project.project_id,
            "portfolio",
            "m11-samples",
            samples,
            portfolio_stage="samples",
            replications_R=2,
            sampling_rounds_J=8,
        ),
        _completed_job(
            store,
            project.project_id,
            "portfolio",
            "m11-analysis",
            analysis,
            portfolio_stage="analysis",
            replications_R=2,
            sampling_rounds_J=8,
        ),
    )
    return {
        "artifact_root": str(application.artifact_root),
        "project_id": project.project_id,
        "revision_id": revision.revision_id,
        "simulation_id": simulation_result.reference.artifact_id,
        "exposure_id": exposure.artifact_id,
        "portfolio_samples_id": samples.artifact_id,
        "portfolio_analysis_id": analysis.artifact_id,
        "job_ids": [job.job_id for job in jobs],
        "replications_R": 2,
        "sampling_rounds_J": 8,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    print(json.dumps(build(args.root), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
