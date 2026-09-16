"""Dispatch comparisons from retained realized inputs, without resampling."""

import json
import pandas as pd
from collections import defaultdict
from mobile_sensing.application.resource_tables import read_table, publish_tables
from mobile_sensing.application.project_resolution import ResolvedProject, assemble_runtime
from mobile_sensing.application.models import ReplicationInput
from mobile_sensing.application.project_models import ProjectConfig
from mobile_sensing.artifacts import verify_partitioned_artifact
from mobile_sensing.contracts import (
    ArtifactDependency,
    AssignmentPlan,
    ClockConfig,
    LocationRef,
    Task,
    VehicleSpec,
    VehicleAvailability,
    scientific_hash,
    stable_id,
)
from mobile_sensing.environment import PreparedEnvironmentReader
from mobile_sensing.simulation.rng import SemanticRngStreams


def replay_project(root, config, source_run=None, *, cancellation, progress, source_reference=None):
    from mobile_sensing.application.run_pipeline import mobility_projection

    if source_run is not None:
        old, new = mobility_projection(source_run.config), mobility_projection(config)
        old_fleets = {fleet["fleet_id"]: fleet for fleet in old["fleets"]}
        new_fleets = {fleet["fleet_id"]: fleet for fleet in new["fleets"]}
        if set(old_fleets) != set(new_fleets):
            raise ValueError("A dispatch comparison must retain the physical fleets")
        for key in old_fleets:
            left, right = old_fleets[key], new_fleets[key]
            if left["dispatch"] != right["dispatch"]:
                if {left["dispatch"]["mode"], right["dispatch"]["mode"]} - {"sequential", "batch"}:
                    raise ValueError(
                        "Retained-input comparisons support Sequential/Batch changes only"
                    )
                left.pop("dispatch")
                right.pop("dispatch")
        if old != new:
            raise ValueError(
                "A dispatch comparison must retain demand, supply, dates, seed and replication count"
            )
    artifact = verify_partitioned_artifact(
        artifact_root=root,
        collection="datasets",
        reference=source_run.resolution if source_run else source_reference,
    )
    source = dict(artifact.manifest.scientific_identity.resolved_config)
    retained = ProjectConfig.model_validate_json(json.dumps(source["project"]))
    if source_run is None and mobility_projection(retained) != mobility_projection(config):
        raise ValueError("Retained resolution does not match this mobility configuration")
    if source_run is not None and retained.simulation != source_run.config.simulation:
        # A rebinned run may legitimately share this realization source.
        a, b = retained.simulation.model_dump(), source_run.config.simulation.model_dump()
        a.pop("temporal_resolution_minutes")
        b.pop("temporal_resolution_minutes")
        if a != b:
            raise ValueError("Source record disagrees with retained realization identity")
    frames = {
        table.name: read_table(root, artifact.reference, table.name)
        for table in artifact.manifest.tables
    }
    for name, columns in {
        "planning_metrics": ["replication_id", "fleet_id", "search_seconds"],
        "replication_plans": ["replication_id", "fleet_id", "plan_json"],
        "realized_tasks": ["replication_id", "fleet_id", "task_id", "task_json"],
        "od_rejections": [
            "replication_id",
            "fleet_id",
            "task_rank",
            "attempt",
            "origin",
            "destination",
            "reason",
        ],
    }.items():
        if frames[name].empty:
            frames[name] = pd.DataFrame(columns=columns)
    specs = tuple(
        VehicleSpec.model_validate_json(value) for value in frames["physical_catalog"].spec_json
    )
    locations = {
        row.location_id: LocationRef.model_validate_json(row.location_json)
        for row in frames["resolved_locations"].itertuples()
    }
    tasks, availability, plans = defaultdict(list), defaultdict(list), defaultdict(list)
    for row in frames["realized_tasks"].itertuples():
        tasks[row.replication_id].append(Task.model_validate_json(row.task_json))
    for row in frames["availability"].itertuples():
        availability[row.replication_id].append(
            VehicleAvailability.model_validate_json(row.availability_json)
        )
    for row in frames["replication_plans"].itertuples():
        plans[row.replication_id].append(AssignmentPlan.model_validate_json(row.plan_json))
    replications = []
    for replication_id in sorted(availability):
        cancellation.raise_if_cancelled()
        realized = tuple(
            sorted(
                tasks[replication_id],
                key=lambda task: (task.release_s, task.fleet_id, task.task_id),
            )
        )
        available = tuple(
            sorted(
                availability[replication_id],
                key=lambda row: (row.vehicle.fleet_id, row.vehicle.vehicle_id),
            )
        )
        identity = scientific_hash({"tasks": realized, "availability": available})
        replications.append(
            ReplicationInput(
                replication_id=replication_id,
                joint_scenario_id=stable_id("joint_scenario", identity),
                tasks=realized,
                availability=available,
                input_hash=identity,
                rng=SemanticRngStreams(config.simulation.seed, namespace="studio.simulation"),
                assignment_plans=tuple(plans[replication_id]),
                online_fleet_ids=tuple(
                    fleet.fleet_id
                    for fleet in config.fleets
                    if fleet.demand.generation_timing == "online"
                ),
            )
        )
    if len(replications) != config.simulation.replications:
        raise ValueError("Retained input does not contain the complete joint replication set")
    static = {
        key: AssignmentPlan.model_validate_json(json.dumps(value))
        for key, value in source["assignment_plans"].items()
    }
    if source_run is not None:
        source["project"] = config.model_dump(mode="json")
        source["reports"] = {
            **source["reports"],
            "replay": {
                "source_run_id": source_run.run_id,
                "source_resolution": source_run.resolution.model_dump(mode="json"),
                "rule": "Exact retained task, availability, catalog and plan rows; no generator or duty inference called",
            },
        }
        dependencies = (
            *artifact.manifest.dependencies,
            ArtifactDependency(
                role="realization_source",
                artifact_id=source_run.resolution.artifact_id,
                content_hash=source_run.resolution.content_hash,
            ),
        )
        reference = publish_tables(
            root,
            config=source,
            dependencies=dependencies,
            algorithm="project-resolution-replay@1",
            frames=frames,
            keys={
                "planning_metrics": ("replication_id", "fleet_id"),
                "replication_plans": ("replication_id", "fleet_id"),
                "realized_tasks": ("replication_id", "fleet_id", "task_id"),
                "availability": ("replication_id", "fleet_id", "vehicle_id"),
                "physical_catalog": ("fleet_id", "vehicle_id"),
                "resolved_locations": ("location_id",),
                "spatial_support": ("cell_id",),
                "od_rejections": ("replication_id", "fleet_id", "task_rank", "attempt"),
            },
        )
    else:
        reference = artifact.reference
    environment = PreparedEnvironmentReader(root).read(config.prepared_environment.artifact)
    # Explicit area memberships are serialized separately from geometry/depot.
    memberships = source.get("location_area_ids", {})
    if any(fleet.supply.service_area_input for fleet in config.fleets) and not memberships:
        raise ValueError("This old realization lacks saved area memberships; resolve it again")
    clock = ClockConfig.model_validate_json(json.dumps(source["clock"]))
    progress.update(
        phase="resolve.reuse_realized_inputs", completed=len(replications), total=len(replications)
    )
    validated = assemble_runtime(
        config,
        environment,
        clock,
        specs,
        locations,
        memberships,
        static,
        tuple(replications),
        reference,
        (
            set(source_run.assumptions)
            if source_run
            else set(source.get("assumptions", restored_assumptions(config)))
        ),
        cancellation,
    )
    return ResolvedProject(config, validated, source["reports"])


def restored_assumptions(config):
    values = set(config.prepared_environment.assumptions)
    for fleet in config.fleets:
        if fleet.demand.source == "generator" or fleet.demand.content != "instances":
            values.add(f"Synthetic demand: {fleet.name}")
        if fleet.supply.source == "timetable":
            values.add(
                "Inferred duties are synthetic physical-vehicle identities on a merged timeline; no minimum fleet guarantee"
            )
        if fleet.supply.synthetic_depot:
            values.add(
                f"Synthetic depot near the {fleet.supply.spatial_feature}-weighted centre: {fleet.name}"
            )
        if fleet.demand.location_condition == "depot_roundtrip":
            values.add(
                f"Generated locations are conditioned on depot round-trip reachability: {fleet.name}"
            )
        if fleet.dispatch.mode == "one_shot":
            values.add(
                "One-shot uses deterministic heuristic search; no global optimality guarantee"
            )
    return values
