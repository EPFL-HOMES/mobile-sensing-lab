"""Version-three authoring resolves into the existing vehicle-agent event kernel."""

import json
from dataclasses import dataclass, replace
import pandas as pd

from mobile_sensing.application.civil_time import civil_clock, clock_seconds
from mobile_sensing.application.duty_authoring import civil_gtfs, infer_duties
from mobile_sensing.application.models import (
    FleetRuntimeResource,
    ReplicationInput,
    ScenarioResourceBundle,
    ValidatedScenario,
)
from mobile_sensing.application.project_models import ProjectConfig
from mobile_sensing.application.resource_tables import publish_tables
from mobile_sensing.application.spatial_support import prepare_support
from mobile_sensing.application.supply_authoring import (
    build_catalog,
    realize_supply,
    assign_areas,
)
from mobile_sensing.application.task_authoring import (
    generate_daily_tasks,
    import_tasks,
    sparse_od_input,
    table_input,
)
from mobile_sensing.contracts import (
    ArtifactDependency,
    ArtifactRef,
    Task,
    VehicleSpec,
    VehicleAvailability,
    AssignmentPlan,
    AssignmentRow,
    FleetConfig,
    ScenarioConfig,
    VehicleKey,
    scientific_hash,
    stable_id,
)
from mobile_sensing.environment import PreparedEnvironmentReader
from mobile_sensing.simulation.dispatch import SimulationDecisionAdapter
from mobile_sensing.simulation.one_shot import (
    plan_one_shot,
    assignment_alias,
    OneShotPlan,
    ONE_SHOT_PLANNER_VERSION,
)
from mobile_sensing.simulation.rng import SemanticRngStreams
from mobile_sensing.application.services import _merge_catalog
from mobile_sensing.datasets.inputs import load_input


PROJECT_RESOLUTION_VERSION = "project-resolution@5"


@dataclass
class ResolvedProject:
    config: ProjectConfig
    validated: ValidatedScenario
    reports: dict


def imported_assignments(root, input_id, fleet_id, task_ids):
    frame, metadata = table_input(root, input_id, {"assignment"})
    if not {"task_id", "vehicle_id", "order_index"} <= set(frame):
        raise ValueError("Assignment plan requires task_id, vehicle_id, order_index")
    if frame.task_id.duplicated().any() or set(frame.task_id) != set(task_ids):
        raise ValueError("Assignment plan must own every task exactly once")
    rows = []
    for number, row in enumerate(frame.itertuples(index=False), start=2):
        try:
            value = float(row.order_index)
            if not value.is_integer() or value < 0:
                raise ValueError("Order indices must be nonnegative integers")
            rows.append(
                AssignmentRow(
                    vehicle=VehicleKey(fleet_id=fleet_id, vehicle_id=row.vehicle_id),
                    order_index=int(value),
                    task_id=row.task_id,
                )
            )
        except (ValueError, TypeError) as exc:
            raise ValueError(
                f"Assignment input {metadata['name']}, source row {number}: {exc}"
            ) from exc
    return AssignmentPlan(assignment_plan_id=stable_id("assignment", rows), rows=tuple(rows))


def resolve_project(
    root, config, *, cancellation, progress, workers=1, memory_limit_bytes=4 * 1024**3
):
    config = ProjectConfig.model_validate(config)
    from mobile_sensing.application.calendar_authoring import resolve_calendar
    from mobile_sensing.application.temporal_authoring import profile_intervals

    config, calendar = resolve_calendar(root, config)
    if config.prepared_environment is None or not config.fleets:
        raise ValueError("Prepare an environment and configure at least one fleet")
    environment = PreparedEnvironmentReader(root).read(config.prepared_environment.artifact)
    from mobile_sensing.environment.cost_cache import PersistentTravelTimes
    from mobile_sensing.application.preparation_cache import cached_stage

    environment = replace(
        environment,
        routing=PersistentTravelTimes(
            environment.routing,
            root,
            workers=workers,
            memory_limit_bytes=memory_limit_bytes,
        ),
    )
    clock = civil_clock(config.simulation, config.prepared_environment.timezone)
    progress.update(phase="resolve.spatial_support", completed=0, total=1)
    support = prepare_support(root, environment, config.prepared_environment.features, cancellation)

    def cache(stage, identity, result_type, compute, rng=None):
        return cached_stage(
            root,
            stage,
            {
                "algorithm": PROJECT_RESOLUTION_VERSION,
                "environment": config.prepared_environment.artifact,
                "features": config.prepared_environment.features,
                **identity,
            },
            result_type,
            compute,
            support=support,
            rng=rng,
            cancellation=cancellation,
            progress=progress,
        )

    catalog_rng = SemanticRngStreams(config.simulation.seed, namespace="studio.catalog")
    specs_by_fleet, static_tasks, static_plans, memberships, reports, sparse_inputs = (
        {},
        {},
        {},
        {},
        {},
        {},
    )
    dependencies = [
        ArtifactDependency(
            role="environment",
            artifact_id=environment.reference.artifact_id,
            content_hash=environment.reference.content_hash,
        ),
        ArtifactDependency(
            role="features",
            artifact_id=config.prepared_environment.features.artifact_id,
            content_hash=config.prepared_environment.features.content_hash,
        ),
    ]
    assumption_set = set(config.prepared_environment.assumptions)
    allowed_locations_by_fleet = {}
    temporal_inputs = {}
    fleets = sorted(config.fleets, key=lambda fleet: fleet.fleet_id)
    for index, fleet in enumerate(fleets):
        cancellation.raise_if_cancelled()
        progress.update(phase=f"resolve.fleet.{fleet.fleet_id}", completed=index, total=len(fleets))
        history = {}
        if fleet.demand.temporal_mode != "window":
            temporal_inputs[fleet.fleet_id] = profile_intervals(root, fleet.demand)
        for role, input_id in [
            ("demand", fleet.demand.input_id),
            ("temporal_profile", fleet.demand.time_profile_input),
            ("od_weights", fleet.demand.od_distribution_input),
            ("supply", fleet.supply.input_id),
            ("assignment", fleet.dispatch.assignment_input),
            ("areas", fleet.supply.service_area_input),
            ("area_assignments", fleet.supply.area_assignment_input),
        ]:
            if input_id:
                metadata, _ = load_input(root, input_id)
                dependencies.append(
                    ArtifactDependency(
                        role=f"input_{fleet.fleet_id}_{role}",
                        artifact_id=input_id,
                        content_hash=metadata["content_hash"],
                    )
                )
        if fleet.supply.post_service == "return_after_plan" and fleet.dispatch.mode != "one_shot":
            raise ValueError("Return after plan requires One-shot dispatch")
        if fleet.demand.template == "gtfs":
            tasks, report, references = cache(
                "gtfs",
                {
                    "fleet": fleet.fleet_id,
                    "demand": fleet.demand,
                    "routing": fleet.routing_profile,
                    "clock": clock,
                },
                tuple[tuple[Task, ...], dict, tuple[ArtifactRef, ...]],
                lambda: civil_gtfs(root, fleet, support, clock, cancellation),
            )
            static_tasks[fleet.fleet_id] = tasks
            reports[fleet.fleet_id] = report
            dependencies.extend(
                ArtifactDependency(
                    role=f"gtfs_{fleet.fleet_id}_{i}",
                    artifact_id=ref.artifact_id,
                    content_hash=ref.content_hash,
                )
                for i, ref in enumerate(references)
            )
        elif fleet.demand.source == "import" and fleet.demand.content == "instances":
            tasks, history = import_tasks(root, fleet, support, clock, catalog_rng, "static")
            static_tasks[fleet.fleet_id] = tasks
        if fleet.supply.source == "timetable":
            if fleet.demand.task_type != "ordered" or fleet.fleet_id not in static_tasks:
                raise ValueError("Timetable supply requires imported ordered-stop tasks")
            if fleet.supply.capacity_mode != "none":
                raise ValueError(
                    "Timetable duty inference currently supports no-capacity ordered-stop tasks"
                )
            execute_history = history if fleet.demand.imported_assignment == "execute" else None
            if (
                execute_history
                and fleet.supply.fleet_size is None
                and not fleet.supply.timetable_catalog_complete
            ):
                raise ValueError(
                    "Task vehicle references do not establish a complete physical catalog; declare completeness, supply a catalog, or specify its full size"
                )
            tasks, specs, plan, duty_report = cache(
                "duties",
                {
                    "fleet": fleet.fleet_id,
                    "tasks": static_tasks[fleet.fleet_id],
                    "supply": fleet.supply,
                    "routing": fleet.routing_profile,
                    "assignments": execute_history,
                },
                tuple[
                    tuple[Task, ...],
                    tuple[VehicleSpec, ...],
                    AssignmentPlan,
                    list[dict],
                ],
                lambda: infer_duties(
                    fleet,
                    static_tasks[fleet.fleet_id],
                    support,
                    fixed_size=fleet.supply.fleet_size,
                    assignments=execute_history,
                ),
            )
            static_tasks[fleet.fleet_id], static_plans[fleet.fleet_id] = tasks, plan
            specs, area_membership, area_report = assign_areas(root, fleet, specs, support)
            if fleet.supply.operating_start is not None or fleet.supply.operating_end is not None:
                if fleet.supply.operating_start is None or fleet.supply.operating_end is None:
                    raise ValueError(
                        "Custom timetable operating window requires both start and end"
                    )
                start, end = (
                    clock_seconds(fleet.supply.operating_start, clock),
                    clock_seconds(fleet.supply.operating_end, clock),
                )
                if any(
                    spec.availability_start_s < start or spec.availability_end_s > end
                    for spec in specs
                ):
                    raise ValueError(
                        "The inferred duties do not fit the custom operating window; inspect pre-run and carry-in or use the duty-derived window"
                    )
            reports.setdefault(fleet.fleet_id, {})["duties"] = duty_report
            assumption_set.add(
                "Inferred duties are synthetic physical-vehicle identities on a merged timeline; no minimum fleet guarantee"
            )
        else:
            if fleet.supply.source == "generated":
                specs, area_membership, area_report = cache(
                    "catalog",
                    {
                        "fleet": fleet.fleet_id,
                        "supply": fleet.supply,
                        "auto_area_demand": (
                            fleet.demand if fleet.supply.service_area_mode == "auto" else None
                        ),
                        "clock": clock,
                        "seed": config.simulation.seed,
                    },
                    tuple[
                        tuple[VehicleSpec, ...],
                        dict[str, tuple[str, ...]],
                        dict | None,
                    ],
                    lambda: build_catalog(root, fleet, support, clock, catalog_rng),
                    catalog_rng,
                )
            else:
                specs, area_membership, area_report = build_catalog(
                    root, fleet, support, clock, catalog_rng
                )
            if history and fleet.demand.imported_assignment == "execute":
                if fleet.dispatch.mode != "scheduled":
                    raise ValueError(
                        "Input contains vehicle assignments: choose Scheduled execution or keep them as history only"
                    )
                grouped = {}
                for task in static_tasks[fleet.fleet_id]:
                    if task.task_id not in history:
                        raise ValueError("Input vehicle assignments are incomplete")
                    grouped.setdefault(history[task.task_id], []).append(task)
                rows = []
                for vehicle_id, tasks in sorted(grouped.items()):
                    for order, task in enumerate(
                        sorted(tasks, key=lambda task: (task.release_s, task.task_id))
                    ):
                        rows.append(
                            AssignmentRow(
                                vehicle=VehicleKey(fleet_id=fleet.fleet_id, vehicle_id=vehicle_id),
                                order_index=order,
                                task_id=task.task_id,
                            )
                        )
                static_plans[fleet.fleet_id] = AssignmentPlan(
                    assignment_plan_id=stable_id("assignment", rows), rows=tuple(rows)
                )
        specs_by_fleet[fleet.fleet_id] = specs
        if area_report is not None:
            reports.setdefault(fleet.fleet_id, {})["service_areas"] = area_report
            if fleet.supply.service_area_mode == "auto":
                assumption_set.add(
                    f"Auto service areas are deterministic expected-demand partitions frozen before replications: {fleet.name}"
                )
        if fleet.demand.location_condition == "depot_roundtrip":
            depots = {spec.depot_location_id for spec in specs}
            if None in depots or len(depots) != 1:
                raise ValueError("Depot-conditioned demand requires a single resolved depot")
            depot = support.locations[next(iter(depots))]
            nodes = environment.routing.roundtrip_node_ids(depot.node_id)
            allowed = frozenset(
                key for key, value in support.locations.items() if value.node_id in nodes
            )
            allowed_locations_by_fleet[fleet.fleet_id] = allowed
            weights = support.raw_mixture(
                fleet.demand.spatial_weights or ((fleet.demand.spatial_feature, 1.0),)
            )
            excluded = sorted(
                cell for cell, location in support.cell_locations.items() if location not in allowed
            )
            reports.setdefault(fleet.fleet_id, {})["location_condition"] = {
                "rule": "Population or selected feature draws conditioned on directed round-trip reachability to the declared depot before sampling",
                "depot_location_id": depot.location_id,
                "excluded_resolved_cells": excluded,
                "excluded_feature_mass": sum(weights.get(cell, 0.0) for cell in excluded),
                "accepted_feature_mass": sum(
                    weights.get(cell, 0.0)
                    for cell, location in support.cell_locations.items()
                    if location in allowed
                ),
            }
            assumption_set.add(
                f"Generated locations are conditioned on depot round-trip reachability: {fleet.name}"
            )
        for location_id, areas in area_membership.items():
            memberships[location_id] = tuple(
                sorted(set(memberships.get(location_id, ())) | set(areas))
            )
        if fleet.dispatch.assignment_input:
            static_plans[fleet.fleet_id] = imported_assignments(
                root,
                fleet.dispatch.assignment_input,
                fleet.fleet_id,
                [task.task_id for task in static_tasks.get(fleet.fleet_id, ())],
            )
        if fleet.dispatch.mode == "scheduled" and fleet.fleet_id not in static_plans:
            raise ValueError(
                "Scheduled dispatch requires a complete input or inferred assignment plan"
            )
        if fleet.demand.source == "generator" or fleet.demand.content != "instances":
            assumption_set.add(f"Synthetic demand: {fleet.name}")
        if fleet.supply.synthetic_depot:
            assumption_set.add(
                (
                    f"Synthetic depot at the supplied geographic coordinates: {fleet.name}"
                    if fleet.supply.depot_longitude is not None
                    else f"Synthetic depot near the configured spatial-feature mixture centre: {fleet.name}"
                )
            )
        if fleet.demand.od_distribution_input:
            sparse_inputs[fleet.fleet_id] = sparse_od_input(
                root, fleet.demand.od_distribution_input, support
            )
    specs = tuple(
        sorted(
            (spec for group in specs_by_fleet.values() for spec in group),
            key=lambda spec: (spec.key.fleet_id, spec.key.vehicle_id),
        )
    )
    earliest = min(
        [clock.simulation_start_s]
        + [task.release_s for group in static_tasks.values() for task in group]
        + [spec.availability_start_s for spec in specs]
    )
    clock = clock.model_copy(update={"simulation_start_s": earliest})
    replication_inputs, realized_rows, availability_rows, rejection_rows = (
        [],
        [],
        [],
        [],
    )
    planning_rows, plan_rows = [], []
    for index in range(config.simulation.replications):
        cancellation.raise_if_cancelled()
        progress.update(
            phase="resolve.replications",
            completed=index,
            total=config.simulation.replications,
        )
        # Common random numbers survive dispatch, sensing, display and process changes.
        replication_id = stable_id(
            "replication",
            {
                "seed": config.simulation.seed,
                "origin": clock.origin_utc,
                "index": index,
            },
        )
        rng = SemanticRngStreams(config.simulation.seed, namespace="studio.simulation")
        tasks, availability, replication_plans = [], [], []
        for fleet in fleets:
            if fleet.fleet_id in static_tasks:
                fleet_tasks = static_tasks[fleet.fleet_id]
            elif fleet.demand.source == "generator":
                fleet_tasks, rejected = cache(
                    "demand",
                    {
                        "fleet": fleet.fleet_id,
                        "demand": fleet.demand,
                        "clock": clock,
                        "seed": config.simulation.seed,
                        "capacity_mode": fleet.supply.capacity_mode,
                        "routing": fleet.routing_profile,
                        "replication": replication_id,
                        "warmup": config.simulation.warmup_hours,
                        "temporal": temporal_inputs.get(fleet.fleet_id),
                        "od": sparse_inputs.get(fleet.fleet_id, ()),
                        "allowed": sorted(allowed_locations_by_fleet.get(fleet.fleet_id, ())),
                    },
                    tuple[tuple[Task, ...], list[dict] | tuple[dict, ...]],
                    lambda: generate_daily_tasks(
                        fleet,
                        support,
                        clock,
                        rng,
                        replication_id,
                        cancellation=cancellation,
                        warmup_hours=config.simulation.warmup_hours,
                        temporal_intervals=temporal_inputs.get(fleet.fleet_id),
                        sparse_od=sparse_inputs.get(fleet.fleet_id, ()),
                        allowed_locations=allowed_locations_by_fleet.get(fleet.fleet_id),
                    ),
                    rng,
                )
                rejection_rows.extend(
                    {
                        "replication_id": replication_id,
                        "fleet_id": fleet.fleet_id,
                        **row,
                    }
                    for row in rejected
                )
            else:
                fleet_tasks, _ = import_tasks(root, fleet, support, clock, rng, replication_id)
            fleet_availability = cache(
                "availability",
                {
                    "fleet": fleet.fleet_id,
                    "supply": fleet.supply,
                    "specs": specs_by_fleet[fleet.fleet_id],
                    "clock": clock,
                    "seed": config.simulation.seed,
                    "replication": replication_id,
                },
                tuple[VehicleAvailability, ...],
                lambda: realize_supply(
                    fleet, specs_by_fleet[fleet.fleet_id], clock, rng, replication_id
                ),
                rng,
            )
            if fleet.dispatch.mode == "one_shot":
                if fleet.supply.service_area_mode != "none":
                    _, area_mapping, _ = assign_areas(
                        root, fleet, specs_by_fleet[fleet.fleet_id], support
                    )
                else:
                    area_mapping = {}
                planned = cache(
                    "one_shot",
                    {
                        "version": ONE_SHOT_PLANNER_VERSION,
                        "fleet": fleet.fleet_id,
                        "dispatch": fleet.dispatch,
                        "supply": fleet.supply,
                        "demand": fleet.demand,
                        "tasks": fleet_tasks,
                        "specs": specs_by_fleet[fleet.fleet_id],
                        "availability": fleet_availability,
                        "end": clock.end_s,
                        "routing": fleet.routing_profile,
                        "areas": area_mapping,
                        "replication": replication_id,
                    },
                    OneShotPlan,
                    lambda: plan_one_shot(
                        fleet,
                        fleet_tasks,
                        specs_by_fleet[fleet.fleet_id],
                        fleet_availability,
                        support.locations,
                        environment.routing,
                        replication_id,
                        cancellation=cancellation,
                        progress=progress,
                        end_s=clock.end_s,
                        location_area_ids=area_mapping,
                    ),
                )
                fleet_tasks = planned.tasks
                replication_plans.append(planned.assignment)
                report = dict(planned.report)
                elapsed = report.pop("search_seconds", 0.0)
                reports.setdefault(fleet.fleet_id, {}).setdefault("planning", {})[
                    replication_id
                ] = report
                planning_rows.append(
                    {
                        "replication_id": replication_id,
                        "fleet_id": fleet.fleet_id,
                        "search_seconds": elapsed,
                    }
                )
                plan_rows.append(
                    {
                        "replication_id": replication_id,
                        "fleet_id": fleet.fleet_id,
                        "plan_json": planned.assignment.model_dump_json(),
                    }
                )
                assumption_set.add(
                    "One-shot uses deterministic heuristic search; no global optimality guarantee"
                )
            tasks.extend(fleet_tasks)
            availability.extend(fleet_availability)
        tasks = tuple(sorted(tasks, key=lambda task: (task.release_s, task.fleet_id, task.task_id)))
        available = tuple(
            sorted(
                availability,
                key=lambda row: (row.vehicle.fleet_id, row.vehicle.vehicle_id),
            )
        )
        input_hash = scientific_hash({"tasks": tasks, "availability": available})
        replication_inputs.append(
            ReplicationInput(
                replication_id=replication_id,
                joint_scenario_id=stable_id("joint_scenario", input_hash),
                tasks=tasks,
                availability=available,
                input_hash=input_hash,
                rng=rng,
                assignment_plans=tuple(replication_plans),
                online_fleet_ids=tuple(
                    fleet.fleet_id
                    for fleet in fleets
                    if fleet.demand.source == "generator"
                    and fleet.demand.generation_timing == "online"
                ),
            )
        )
        realized_rows.extend(
            {
                "replication_id": replication_id,
                "fleet_id": task.fleet_id,
                "task_id": task.task_id,
                "task_json": task.model_dump_json(),
            }
            for task in tasks
        )
        availability_rows.extend(
            {
                "replication_id": replication_id,
                "fleet_id": row.vehicle.fleet_id,
                "vehicle_id": row.vehicle.vehicle_id,
                "availability_json": row.model_dump_json(),
            }
            for row in available
        )
    frames = {
        "planning_metrics": pd.DataFrame(
            planning_rows, columns=["replication_id", "fleet_id", "search_seconds"]
        ),
        "replication_plans": pd.DataFrame(
            plan_rows, columns=["replication_id", "fleet_id", "plan_json"]
        ),
        "realized_tasks": pd.DataFrame(
            realized_rows,
            columns=["replication_id", "fleet_id", "task_id", "task_json"],
        ),
        "availability": pd.DataFrame(availability_rows),
        "physical_catalog": pd.DataFrame(
            [
                {
                    "fleet_id": spec.key.fleet_id,
                    "vehicle_id": spec.key.vehicle_id,
                    "spec_json": spec.model_dump_json(),
                }
                for spec in specs
            ]
        ),
        "resolved_locations": pd.DataFrame(
            [
                {"location_id": key, "location_json": value.model_dump_json()}
                for key, value in sorted(support.locations.items())
            ]
        ),
        "spatial_support": pd.DataFrame(support.audit),
        "od_rejections": pd.DataFrame(
            rejection_rows,
            columns=[
                "replication_id",
                "fleet_id",
                "task_rank",
                "attempt",
                "origin",
                "destination",
                "reason",
            ],
        ),
    }
    # Coordinate-based count/rate imports may add locations during realization.
    for fleet in fleets:
        if fleet.supply.service_area_mode != "none":
            _, mapping, _ = assign_areas(root, fleet, specs_by_fleet[fleet.fleet_id], support)
            for location_id, areas in mapping.items():
                memberships[location_id] = tuple(
                    sorted(set(memberships.get(location_id, ())) | set(areas))
                )
    keys = {
        "planning_metrics": ("replication_id", "fleet_id"),
        "replication_plans": ("replication_id", "fleet_id"),
        "realized_tasks": ("replication_id", "fleet_id", "task_id"),
        "availability": ("replication_id", "fleet_id", "vehicle_id"),
        "physical_catalog": ("fleet_id", "vehicle_id"),
        "resolved_locations": ("location_id",),
        "spatial_support": ("cell_id",),
        "od_rejections": ("replication_id", "fleet_id", "task_rank", "attempt"),
    }
    reports["spatial_support"] = {
        "cells": len(support.audit),
        "accepted_cells": len(support.cell_locations),
        "rejected_cells": len(support.audit) - len(support.cell_locations),
        "feature_mass": {
            name: {
                "total": sum(values.values()),
                "resolvable": sum(
                    value for cell, value in values.items() if cell in support.cell_locations
                ),
            }
            for name, values in support.feature_values.items()
        },
        "od_sampling": "Independent origin/destination or sparse-pair draws conditioned on distinct nodes and directed reachability; all rejected pairs retained",
    }
    resolved_config = {
        "project": config.model_dump(mode="json"),
        "clock": clock.model_dump(mode="json"),
        "reports": reports,
        "assumptions": sorted(assumption_set),
        "location_area_ids": memberships,
        "assignment_plans": {
            key: plan.model_dump(mode="json") for key, plan in static_plans.items()
        },
        "seed_manifest": catalog_rng.manifest.model_dump(mode="json"),
    }
    reference = publish_tables(
        root,
        config=resolved_config,
        dependencies=dependencies,
        algorithm=PROJECT_RESOLUTION_VERSION,
        frames=frames,
        keys=keys,
    )
    validated = assemble_runtime(
        config,
        environment,
        clock,
        specs,
        support.locations,
        memberships,
        static_plans,
        replication_inputs,
        reference,
        assumption_set,
        cancellation,
    )
    return ResolvedProject(config, validated, reports)


def assemble_runtime(
    config,
    environment,
    clock,
    specs,
    locations,
    memberships,
    static_plans,
    replication_inputs,
    reference,
    assumption_set,
    cancellation,
):
    runtime_configs, resources = [], []
    for fleet in sorted(config.fleets, key=lambda item: item.fleet_id):
        supply = fleet.supply
        area_definitions_ref = supply.service_area_input
        area_assignments_ref = supply.area_assignment_input
        if supply.service_area_mode == "auto":
            auto_identity = {
                "fleet": fleet.fleet_id,
                "areas": supply.auto_service_area_count,
                "resolution": reference.content_hash,
            }
            area_definitions_ref = stable_id("auto_area_definitions", auto_identity)
            area_assignments_ref = stable_id("auto_area_assignments", auto_identity)
        capacity = {"mode": supply.capacity_mode}
        if supply.capacity_mode != "none":
            capacity["unit"] = supply.capacity_unit
        if supply.capacity_mode == "consumable":
            capacity["replenishment_duration_s"] = supply.depot_min_stay_minutes * 60
        if fleet.dispatch.mode == "scheduled":
            dispatch = {
                "policy": "predefined",
                "assignment_plan_ref": static_plans[fleet.fleet_id].assignment_plan_id,
            }
        elif fleet.dispatch.mode == "sequential":
            dispatch = {
                "policy": "nearest_matching",
                "max_pickup_time_s": fleet.dispatch.max_pickup_minutes * 60,
            }
        elif fleet.dispatch.mode == "batch":
            dispatch = {
                "policy": "batch_nearest_matching",
                "max_pickup_time_s": fleet.dispatch.max_pickup_minutes * 60,
                "batch_interval_s": fleet.dispatch.batch_minutes * 60,
                "batch_origin_s": clock.observation_start_s,
            }
        else:
            dispatch = {
                "policy": "one_shot",
                "assignment_plan_ref": assignment_alias(fleet.fleet_id),
            }
        value = {
            "fleet_id": fleet.fleet_id,
            "label": fleet.name,
            "demand": {
                "source": "upload",
                "structure": fleet.demand.task_type,
                "adapter": "demand.resolved_authoring@3",
                "parameters": {
                    "dataset_id": reference.artifact_id,
                    "mapping_id": "project-authoring@3",
                },
            },
            "supply": {
                "source": "upload",
                "catalog_ref": reference.artifact_id,
                "availability_ref": reference.artifact_id,
                "capacity": capacity,
                "idle_policy": {
                    "policy": (
                        "random_cruise" if supply.post_service == "random_cruise" else "stationary"
                    )
                },
                "area_definitions_ref": area_definitions_ref,
                "area_assignments_ref": area_assignments_ref,
            },
            "dispatch": dispatch,
            "routing": {"profile_id": fleet.routing_profile},
        }
        runtime_configs.append(FleetConfig.model_validate_json(json.dumps(value)))
        resources.append(
            FleetRuntimeResource(
                fleet_id=fleet.fleet_id,
                demand_artifact=reference,
                supply_artifact=reference,
            )
        )
    scenario = ScenarioConfig(
        schema_version="2.0",
        environment_id=environment.reference.artifact_id,
        clock=clock,
        fleets=tuple(runtime_configs),
        replications=config.simulation.replications,
        master_seed=config.simulation.seed,
        joint_scenario_model={"kind": "independent_conditional_environment"},
    )
    plans = {plan.assignment_plan_id: plan for plan in static_plans.values()}
    for replication in replication_inputs:
        cancellation.raise_if_cancelled()
        SimulationDecisionAdapter(
            fleets=scenario.fleets,
            tasks=replication.tasks,
            vehicle_specs=specs,
            locations=locations,
            routing=environment.routing,
            assignment_plans={
                **plans,
                **{plan.assignment_plan_id: plan for plan in replication.assignment_plans},
            },
            location_area_ids=memberships,
            replication_id=replication.replication_id,
            simulation_end_s=clock.end_s,
            availability=replication.availability,
            rng=replication.rng,
        )
    bundle = ScenarioResourceBundle(scenario=scenario, fleets=tuple(resources))
    validated = ValidatedScenario(
        reference=reference,
        environment=environment,
        bundle=bundle,
        catalog=_merge_catalog(specs),
        vehicle_specs=specs,
        locations=locations,
        location_area_ids=memberships,
        assignment_plans=plans,
        replications=tuple(replication_inputs),
        scenario_hash=scientific_hash({"project": config, "resolved": reference.content_hash}),
        assumptions=tuple(sorted(assumption_set)),
        impact_summaries=(),
        estimated_working_bytes=environment.metadata.directed_edge_count * 2048
        + max(len(item.tasks) for item in replication_inputs) * 8192
        + len(specs) * 4096,
    )
    return validated
