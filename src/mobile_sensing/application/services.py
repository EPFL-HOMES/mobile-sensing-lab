"""Synchronous application use cases shared by Python and the M07 CLI."""

from __future__ import annotations

import os
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor
from functools import lru_cache
from multiprocessing import get_context
from pathlib import Path

import pyarrow as pa

from mobile_sensing.artifacts import PartitionedTableData, publish_partitioned_artifact
from mobile_sensing.contracts import (
    ArtifactDependency,
    ArtifactRef,
    AssignmentPlan,
    CatalogIdentity,
    EnvironmentArtifactRef,
    EnvironmentBuildConfig,
    EnvironmentProviderRequest,
    ExecutionOptions,
    ExposureConfig,
    JointReplicationIdentity,
    LocationRef,
    PortfolioConfig,
    ScenarioConfig,
    ScientificIdentity,
    VehicleAvailability,
    VehicleSpec,
    canonical_json_text,
    scientific_hash,
    scientific_projection,
    stable_id,
)
from mobile_sensing.datasets import (
    AreaAssignmentMapping,
    DATASET_RUNTIME_LOADER_VERSION,
    DemandImportMapping,
    GTFSReconstruction,
    GTFSReconstructionConfig,
    GTFSSource,
    DatasetRuntimeContent,
    NormalizedDemand,
    NormalizedSupply,
    RateImportMapping,
    TabularSource,
    VehicleImportMapping,
    normalize_demand,
    normalize_supply,
    load_dataset_runtime_content,
    reconstruct_gtfs,
    register_tabular_source,
)
from mobile_sensing.datasets.parsing import LocationResolver
from mobile_sensing.datasets.storage import verified_dataset_directory
from mobile_sensing.environment import (
    LocalDatasetCatalog,
    LocalEnvironmentBuilder,
    LocalFileEnvironmentProvider,
    PreparedEnvironment,
    PreparedEnvironmentReader,
    validate_scenario_impact,
)
from mobile_sensing.exposure import publish_exposure_artifact
from mobile_sensing.portfolio import (
    PortfolioAnalysisResult,
    PortfolioEvaluationResult,
    PortfolioPreview,
    PortfolioResourceLimits,
    UtilityWeightResource,
    build_portfolio_plan,
    publish_portfolio_analysis,
    publish_portfolio_samples,
)
from mobile_sensing.simulation import (
    SimulationDecisionAdapter,
    build_generated_catalog,
    generate_piecewise_poisson,
    publish_simulation_results,
    realize_availability,
    run_event_kernel,
)
from mobile_sensing.simulation.rng import SemanticRngStreams

from mobile_sensing.application.models import (
    FleetRuntimeResource,
    ReplicationInput,
    ScenarioResourceBundle,
    SimulationRunResult,
    ValidatedScenario,
)


APPLICATION_VERSION = "headless-application@1"
SCENARIO_VALIDATION_VERSION = "scenario-validation@2"
ESTIMATED_EDGE_BYTES = 512
ESTIMATED_TASK_BYTES = 2_048
ESTIMATED_VEHICLE_BYTES = 4_096
DEFAULT_MAX_TASKS_PER_REPLICATION = 1_000_000

VALIDATION_SCHEMA = pa.schema(
    [
        ("scenario_hash", pa.string(), False),
        ("catalog_id", pa.string(), False),
        ("catalog_hash", pa.string(), False),
        ("replication_count", pa.int64(), False),
        ("fleet_count", pa.int64(), False),
        ("vehicle_count", pa.int64(), False),
        ("task_count_by_replication_json", pa.string(), False),
        ("impact_summaries_json", pa.string(), False),
        ("assumptions_json", pa.string(), False),
        ("estimated_working_bytes", pa.int64(), False),
    ]
)


class _NeverCancelled:
    def is_cancelled(self) -> bool:
        return False


def _initialize_replication_worker() -> None:
    for name in ("OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "OMP_NUM_THREADS"):
        os.environ[name] = "1"


class _PathCancellation:
    def __init__(self, path: str | None) -> None:
        self.path = Path(path) if path is not None else None

    def is_cancelled(self) -> bool:
        return self.path is not None and self.path.exists()


class _NullProgress:
    def update(self, *, phase: str, completed: int, total: int | None) -> None:
        return None


class _PhaseProgress:
    def __init__(self, target, prefix: str) -> None:
        self.target = target
        self.prefix = prefix

    def update(self, *, phase: str, completed: int, total: int | None) -> None:
        self.target.update(
            phase=f"{self.prefix}.{phase}",
            completed=completed,
            total=total,
        )


def _execute_replication(arguments):
    """Spawn-safe replication execution with semantic RNG evidence returned to the parent."""

    (
        index,
        replication,
        scenario,
        vehicle_specs,
        locations,
        routing,
        assignment_plans,
        location_area_ids,
        progress_frequency_events,
        cancellation,
    ) = arguments
    assignment_plans = {
        **assignment_plans,
        **{plan.assignment_plan_id: plan for plan in replication.assignment_plans},
    }
    decisions = SimulationDecisionAdapter(
        fleets=scenario.fleets,
        tasks=replication.tasks,
        vehicle_specs=vehicle_specs,
        locations=locations,
        routing=routing,
        assignment_plans=assignment_plans,
        location_area_ids=location_area_ids,
        replication_id=replication.replication_id,
        simulation_end_s=scenario.clock.end_s,
        availability=replication.availability,
        rng=replication.rng,
    )
    result = run_event_kernel(
        replication_id=replication.replication_id,
        clock=scenario.clock,
        tasks=tuple(
            task for task in replication.tasks if task.fleet_id not in replication.online_fleet_ids
        ),
        online_tasks=(
            task
            for task in sorted(
                replication.tasks, key=lambda task: (task.release_s, task.fleet_id, task.task_id)
            )
            if task.fleet_id in replication.online_fleet_ids
        ),
        vehicle_specs=vehicle_specs,
        availability=replication.availability,
        decisions=decisions,
        cancellation=cancellation,
        progress=_NullProgress(),
        progress_frequency_events=progress_frequency_events,
    )
    return index, result, replication.rng.manifest


def _execute_replication_spawn(arguments):
    """Reconstruct non-picklable cached routing state inside each spawned child."""

    artifact_root, environment_reference, remaining, cancellation_path = arguments
    environment = _cached_prepared_environment(
        artifact_root,
        environment_reference.artifact_id,
        environment_reference.content_hash,
    )
    values = list(remaining)
    values.insert(5, environment.routing)
    values.append(_PathCancellation(cancellation_path))
    return _execute_replication(tuple(values))


@lru_cache(maxsize=2)
def _cached_prepared_environment(artifact_root: str, artifact_id: str, content_hash: str):
    reference = EnvironmentArtifactRef(
        artifact_id=artifact_id,
        artifact_kind="environment",
        content_hash=content_hash,
    )
    return PreparedEnvironmentReader(Path(artifact_root)).read(reference)


def _merge_catalog(specs: Sequence[VehicleSpec]) -> CatalogIdentity:
    ordered = tuple(sorted(specs, key=lambda item: (item.key.fleet_id, item.key.vehicle_id)))
    keys = tuple(item.key for item in ordered)
    if not keys or len(keys) != len(set(keys)):
        raise ValueError("resolved scenario requires unique physical vehicle keys")
    physical_metadata_hash = scientific_hash(
        [
            {
                "key": item.key,
                "initial_location_id": item.initial_location_id,
                "depot_location_id": item.depot_location_id,
                "capacity_mode": item.capacity_mode,
                "capacity": item.capacity,
                "quantity_unit": item.quantity_unit,
                "assigned_area_ids": item.assigned_area_ids,
                "identity_provenance": item.identity_provenance,
            }
            for item in ordered
        ]
    )
    catalog_hash = scientific_hash(
        {
            "vehicle_keys": [[item.fleet_id, item.vehicle_id] for item in keys],
            "physical_metadata_hash": physical_metadata_hash,
        }
    )
    return CatalogIdentity(
        catalog_id=stable_id("catalog", catalog_hash),
        vehicle_keys=keys,
        physical_metadata_hash=physical_metadata_hash,
        catalog_hash=catalog_hash,
    )


def _availability_from_specs(
    specs: Sequence[VehicleSpec], replication_id: str
) -> tuple[VehicleAvailability, ...]:
    return tuple(
        VehicleAvailability(
            replication_id=replication_id,
            vehicle=spec.key,
            active=True,
            availability_start_s=spec.availability_start_s,
            availability_end_s=spec.availability_end_s,
            initial_location_id=spec.initial_location_id,
        )
        for spec in sorted(specs, key=lambda item: (item.key.fleet_id, item.key.vehicle_id))
    )


class HeadlessApplication:
    """One synchronous orchestration layer; it owns no scientific calculation."""

    def __init__(
        self,
        artifact_root: str | Path,
        *,
        max_tasks_per_replication: int = DEFAULT_MAX_TASKS_PER_REPLICATION,
    ) -> None:
        self.artifact_root = Path(artifact_root).resolve()
        if isinstance(max_tasks_per_replication, bool) or max_tasks_per_replication <= 0:
            raise ValueError("max_tasks_per_replication must be a positive integer")
        self.max_tasks_per_replication = max_tasks_per_replication

    def prepare_environment(
        self,
        catalog: LocalDatasetCatalog,
        request: EnvironmentProviderRequest,
        config: EnvironmentBuildConfig,
        *,
        cancellation=None,
        progress=None,
    ) -> EnvironmentArtifactRef:
        cancellation = cancellation or _NeverCancelled()
        progress = progress or _NullProgress()
        bundle = LocalFileEnvironmentProvider(catalog).acquire(
            request,
            cancellation=cancellation,
            progress=_PhaseProgress(progress, "environment.acquire"),
        )
        return LocalEnvironmentBuilder(catalog, self.artifact_root).prepare(
            bundle,
            config,
            cancellation=cancellation,
            progress=_PhaseProgress(progress, "environment.prepare"),
        )

    def register_upload(self, source_path: str | Path, *, provenance: str) -> TabularSource:
        return register_tabular_source(
            Path(source_path), artifact_root=self.artifact_root, provenance=provenance
        )

    def normalize_demand(
        self,
        source: TabularSource,
        mapping: DemandImportMapping | RateImportMapping,
        environment: PreparedEnvironment,
        *,
        routing_profile_id: str | None = None,
        chunk_size: int = 10_000,
        max_rows: int = 1_000_000,
    ) -> NormalizedDemand:
        profile_id = self._resolve_profile_id(environment, routing_profile_id)
        resolver = LocationResolver(
            environment=environment.reference,
            snapper=environment.snapping,
            routing=environment.routing,
            routing_profile_id=profile_id,
        )
        return normalize_demand(
            source,
            mapping,
            artifact_root=self.artifact_root,
            resolver=resolver,
            chunk_size=chunk_size,
            max_rows=max_rows,
        )

    def normalize_supply(
        self,
        source: TabularSource,
        mapping: VehicleImportMapping,
        environment: PreparedEnvironment,
        *,
        routing_profile_id: str | None = None,
        area_assignment_source: TabularSource | None = None,
        area_assignment_mapping: AreaAssignmentMapping | None = None,
        known_area_ids: set[str] | None = None,
        chunk_size: int = 10_000,
        max_rows: int = 1_000_000,
    ) -> NormalizedSupply:
        profile_id = self._resolve_profile_id(environment, routing_profile_id)
        resolver = LocationResolver(
            environment=environment.reference,
            snapper=environment.snapping,
            routing=environment.routing,
            routing_profile_id=profile_id,
        )
        return normalize_supply(
            source,
            mapping,
            artifact_root=self.artifact_root,
            resolver=resolver,
            area_assignment_source=area_assignment_source,
            area_assignment_mapping=area_assignment_mapping,
            known_area_ids=known_area_ids,
            chunk_size=chunk_size,
            max_rows=max_rows,
        )

    @staticmethod
    def _resolve_profile_id(environment: PreparedEnvironment, requested: str | None) -> str:
        profile_ids = tuple(sorted(environment.metadata.profile_hashes))
        if requested is not None:
            if requested not in profile_ids:
                raise ValueError(f"unknown routing profile: {requested!r}")
            return requested
        if len(profile_ids) != 1:
            raise ValueError(
                "routing_profile_id is required when the environment has multiple profiles"
            )
        return profile_ids[0]

    def reconstruct_gtfs(
        self,
        source: GTFSSource,
        config: GTFSReconstructionConfig,
        environment: PreparedEnvironment,
    ) -> GTFSReconstruction:
        resolver = LocationResolver(
            environment=environment.reference,
            snapper=environment.snapping,
            routing=environment.routing,
            routing_profile_id=config.routing_profile_id,
        )
        return reconstruct_gtfs(
            source,
            config,
            artifact_root=self.artifact_root,
            resolver=resolver,
            sensing_boundary=environment.boundary.geometry.iloc[0],
        )

    @staticmethod
    def _require_environment_dependency(
        content: DatasetRuntimeContent, environment: PreparedEnvironment
    ) -> None:
        dependency = next(
            (item for item in content.manifest.dependencies if item.role == "environment"), None
        )
        if dependency is None or (
            dependency.artifact_id != environment.reference.artifact_id
            or dependency.content_hash != environment.reference.content_hash
        ):
            raise ValueError("runtime dataset was not normalized against this environment")

    @staticmethod
    def _select_records(records, selected_ids: tuple[str, ...], *, key, label: str):
        by_id = {key(item): item for item in records}
        if selected_ids:
            missing = tuple(sorted(set(selected_ids) - set(by_id)))
            if missing:
                raise ValueError(f"{label} selection references unknown IDs: {missing[:5]!r}")
            return tuple(by_id[item_id] for item_id in selected_ids)
        return tuple(records)

    @staticmethod
    def _verified_gtfs_assignment(
        resource: FleetRuntimeResource,
        source: AssignmentPlan,
        selected_tasks,
        selected_vehicles,
    ) -> AssignmentPlan:
        selected_task_ids = {item.task_id for item in selected_tasks}
        selected_vehicle_ids = {item.key.vehicle_id for item in selected_vehicles}
        source_rows = tuple(
            row
            for row in source.rows
            if row.task_id in selected_task_ids and row.vehicle.vehicle_id in selected_vehicle_ids
        )
        if {row.task_id for row in source_rows} != selected_task_ids:
            raise ValueError("selected GTFS tasks are not owned by the selected duty catalog")
        if resource.assignment_plan is None:
            if resource.selected_task_ids or resource.selected_vehicle_ids:
                raise ValueError(
                    "a bounded GTFS selection requires an explicit derived assignment plan"
                )
            return source
        supplied = resource.assignment_plan
        if {row.task_id for row in supplied.rows} != selected_task_ids:
            raise ValueError("GTFS assignment plan must cover exactly the selected artifact tasks")
        source_by_task = {
            row.task_id: (row.vehicle.fleet_id, row.vehicle.vehicle_id, row.order_index)
            for row in source_rows
        }
        for row in supplied.rows:
            expected = source_by_task.get(row.task_id)
            if expected is None or expected[:2] != (
                row.vehicle.fleet_id,
                row.vehicle.vehicle_id,
            ):
                raise ValueError(
                    "GTFS assignment ownership differs from the reconstruction artifact"
                )
        for vehicle_id in sorted(selected_vehicle_ids):
            source_sequence = [
                row.task_id
                for row in sorted(source_rows, key=lambda item: item.order_index)
                if row.vehicle.vehicle_id == vehicle_id
            ]
            supplied_sequence = [
                row.task_id
                for row in sorted(supplied.rows, key=lambda item: item.order_index)
                if row.vehicle.vehicle_id == vehicle_id
            ]
            if supplied_sequence != source_sequence:
                raise ValueError("GTFS assignment order differs from the reconstruction artifact")
        return supplied

    def _hydrate_resource(
        self,
        environment: PreparedEnvironment,
        fleet,
        resource: FleetRuntimeResource,
        cache: dict[tuple[str, str], DatasetRuntimeContent],
    ) -> FleetRuntimeResource:
        def load(reference: ArtifactRef) -> DatasetRuntimeContent:
            key = (reference.artifact_id, reference.content_hash)
            if key not in cache:
                cache[key] = load_dataset_runtime_content(
                    reference, artifact_root=self.artifact_root
                )
                self._require_environment_dependency(cache[key], environment)
            return cache[key]

        inline_locations = {item.location_id: item for item in resource.locations}
        locations = dict(inline_locations)
        artifact_locations: dict[str, LocationRef] = {}
        tasks = resource.tasks
        specs = resource.vehicle_specs
        assignment = resource.assignment_plan
        if resource.demand_artifact is not None:
            content = load(resource.demand_artifact)
            algorithm = content.manifest.scientific_identity.algorithm_versions
            expected_algorithm = (
                "demand_normalization" if fleet.demand.source == "upload" else "gtfs_reconstruction"
            )
            if expected_algorithm not in algorithm:
                raise ValueError("demand artifact has an incompatible dataset role")
            selected = self._select_records(
                content.tasks,
                resource.selected_task_ids,
                key=lambda item: item.task_id,
                label="task",
            )
            if fleet.demand.source == "upload" and len(selected) != len(content.tasks):
                raise ValueError("uploaded demand must consume the complete normalized task set")
            if tasks and tasks != selected:
                raise ValueError("inline runtime tasks differ from the bound demand artifact")
            tasks = selected
            if fleet.demand.source == "upload":
                expected_mapping = content.manifest.scientific_identity.resolved_config.get(
                    "mapping_id"
                )
                if fleet.demand.parameters.mapping_id != expected_mapping:
                    raise ValueError("uploaded demand mapping does not match its artifact")
            for item in content.locations:
                existing = locations.get(item.location_id)
                if existing is not None and existing != item:
                    raise ValueError("inline runtime location differs from the bound artifact")
                locations[item.location_id] = item
                artifact_locations[item.location_id] = item
        if resource.supply_artifact is not None:
            content = load(resource.supply_artifact)
            algorithm = content.manifest.scientific_identity.algorithm_versions
            expected_algorithm = (
                "supply_normalization" if fleet.supply.source == "upload" else "gtfs_reconstruction"
            )
            if expected_algorithm not in algorithm:
                raise ValueError("supply artifact has an incompatible dataset role")
            selected = self._select_records(
                content.vehicle_specs,
                resource.selected_vehicle_ids,
                key=lambda item: item.key.vehicle_id,
                label="vehicle",
            )
            if fleet.supply.source == "upload" and len(selected) != len(content.vehicle_specs):
                raise ValueError("uploaded supply must consume the complete normalized catalog")
            if specs and specs != selected:
                raise ValueError("inline runtime vehicles differ from the bound supply artifact")
            specs = selected
            for item in content.locations:
                existing = locations.get(item.location_id)
                if existing is not None and existing != item:
                    raise ValueError("inline runtime location differs from the bound artifact")
                locations[item.location_id] = item
                artifact_locations[item.location_id] = item
            if fleet.dispatch.policy == "predefined":
                if fleet.demand.source != "gtfs" or content.assignment_plan is None:
                    raise ValueError(
                        "M07 predefined artifact dispatch requires verified GTFS assignments"
                    )
                assignment = self._verified_gtfs_assignment(
                    resource, content.assignment_plan, tasks, specs
                )
        allowed_inline_ids: set[str] = set()
        if fleet.demand.source == "generator":
            allowed_inline_ids.update(item.location_id for item in resource.location_weights)
            allowed_inline_ids.update(
                location_id
                for item in resource.od_weights
                for location_id in (item.origin_location_id, item.destination_location_id)
            )
        if fleet.supply.source == "generated":
            allowed_inline_ids.update(resource.generated_initial_location_ids)
            allowed_inline_ids.update(
                location_id
                for location_id in resource.generated_depot_location_ids
                if location_id is not None
            )
        if artifact_locations:
            unowned_inline = set(inline_locations) - set(artifact_locations) - allowed_inline_ids
            if unowned_inline:
                raise ValueError(
                    "inline runtime locations are not owned by a bound artifact or generated input: "
                    f"{tuple(sorted(unowned_inline))[:5]!r}"
                )
            locations = {
                location_id: location
                for location_id, location in locations.items()
                if location_id in artifact_locations or location_id in allowed_inline_ids
            }
        payload = resource.model_dump(mode="json")
        payload.update(
            {
                "locations": [item.model_dump(mode="json") for item in locations.values()],
                "tasks": [item.model_dump(mode="json") for item in tasks],
                "vehicle_specs": [item.model_dump(mode="json") for item in specs],
                "assignment_plan": (
                    assignment.model_dump(mode="json") if assignment is not None else None
                ),
            }
        )
        return FleetRuntimeResource.model_validate_json(canonical_json_text(payload))

    def _hydrate_bundle(
        self, environment: PreparedEnvironment, bundle: ScenarioResourceBundle
    ) -> ScenarioResourceBundle:
        by_fleet = {item.fleet_id: item for item in bundle.scenario.fleets}
        cache: dict[tuple[str, str], DatasetRuntimeContent] = {}
        resources = tuple(
            self._hydrate_resource(environment, by_fleet[item.fleet_id], item, cache)
            for item in bundle.fleets
        )
        return ScenarioResourceBundle(
            schema_version=bundle.schema_version,
            scenario=bundle.scenario,
            fleets=resources,
        )

    def _validate_resource(
        self, scenario: ScenarioConfig, fleet, resource: FleetRuntimeResource
    ) -> None:
        if resource.fleet_id != fleet.fleet_id:
            raise ValueError("runtime resource fleet does not match its fleet config")
        demand = fleet.demand
        location_ids = {item.location_id for item in resource.locations}
        if not set(resource.location_area_ids) <= location_ids:
            raise ValueError("location area membership references an unbound location")
        if any(task.fleet_id != fleet.fleet_id for task in resource.tasks):
            raise ValueError("runtime tasks must belong to their configured fleet")
        if any(
            step.location_id not in location_ids for task in resource.tasks for step in task.steps
        ):
            raise ValueError("every runtime task step must reference a bound location")
        if demand.source == "generator":
            if fleet.dispatch.policy == "predefined":
                raise ValueError("generated demand cannot use predefined dispatch in M07")
            if resource.tasks or resource.demand_artifact is not None:
                raise ValueError("generated demand cannot contain static uploaded tasks")
            expected = (
                demand.parameters.location_weights_ref
                if demand.structure == "location"
                else demand.parameters.od_weights_ref
            )
            if resource.weight_source_id != expected:
                raise ValueError("generated demand weight reference is not bound")
            if demand.structure == "location" and not resource.location_weights:
                raise ValueError("generated location demand requires location weights")
            if demand.structure == "od" and not resource.od_weights:
                raise ValueError("generated OD demand requires OD weights")
            weight_location_ids = (
                {item.location_id for item in resource.location_weights}
                if demand.structure == "location"
                else {
                    location_id
                    for item in resource.od_weights
                    for location_id in (
                        item.origin_location_id,
                        item.destination_location_id,
                    )
                }
            )
            if not weight_location_ids <= location_ids:
                raise ValueError("generated demand weights reference unbound locations")
            if any(
                interval.start_s < scenario.clock.simulation_start_s
                or interval.end_s > scenario.clock.end_s
                for interval in demand.parameters.intervals
            ):
                raise ValueError(
                    "generated demand intervals must lie inside the execution interval"
                )
        else:
            expected = (
                demand.parameters.dataset_id
                if demand.source == "upload"
                else demand.parameters.reconstruction_id
            )
            if (
                resource.demand_artifact is None
                or resource.demand_artifact.artifact_id != expected
                or not resource.tasks
            ):
                raise ValueError("static demand artifact/tasks are not bound to the config")

        supply = fleet.supply
        if supply.source == "generated":
            if resource.vehicle_specs:
                raise ValueError("generated supply is built by the application, not supplied")
            if resource.initial_locations_source_id != supply.initial_locations_ref:
                raise ValueError("generated initial-location reference is not bound")
            if len(resource.generated_initial_location_ids) != supply.catalog_size:
                raise ValueError("generated initial locations must cover the requested catalog")
            if any(
                location_id not in location_ids
                for location_id in resource.generated_initial_location_ids
            ):
                raise ValueError("generated initial locations must be bound locations")
            if (
                resource.generated_depot_location_ids
                and len(resource.generated_depot_location_ids) != supply.catalog_size
            ):
                raise ValueError("generated depot locations must cover the requested catalog")
            if any(
                location_id is not None and location_id not in location_ids
                for location_id in resource.generated_depot_location_ids
            ):
                raise ValueError("generated depot locations must be bound locations")
            if supply.depot_policy is not None and (
                not resource.generated_depot_location_ids
                or any(location_id is None for location_id in resource.generated_depot_location_ids)
            ):
                raise ValueError("depot policy requires a depot for every generated vehicle")
        else:
            expected = supply.catalog_ref if supply.source == "upload" else supply.reconstruction_id
            if (
                resource.supply_artifact is None
                or resource.supply_artifact.artifact_id != expected
                or not resource.vehicle_specs
            ):
                raise ValueError("fixed supply artifact/catalog is not bound to the config")
            if supply.source == "upload" and supply.availability_ref != expected:
                raise ValueError(
                    "M07 normalized upload supply requires one artifact for catalog/availability"
                )
            if any(spec.key.fleet_id != fleet.fleet_id for spec in resource.vehicle_specs):
                raise ValueError("runtime vehicles must belong to their configured fleet")
            if any(
                spec.initial_location_id not in location_ids
                or (
                    spec.depot_location_id is not None
                    and spec.depot_location_id not in location_ids
                )
                for spec in resource.vehicle_specs
            ):
                raise ValueError("every vehicle initial/depot location must be bound")
            if supply.depot_policy is not None and any(
                spec.depot_location_id is None for spec in resource.vehicle_specs
            ):
                raise ValueError("depot policy requires a depot for every vehicle")
        if fleet.dispatch.policy == "predefined":
            if (
                resource.assignment_plan is None
                or resource.assignment_plan.assignment_plan_id != fleet.dispatch.assignment_plan_ref
            ):
                raise ValueError("predefined dispatch requires its exact assignment plan")
            if any(row.vehicle.fleet_id != fleet.fleet_id for row in resource.assignment_plan.rows):
                raise ValueError("predefined assignment rows must belong to the fleet")
        if supply.area_assignments_ref is not None:
            task_entry_ids = {task.steps[0].location_id for task in resource.tasks}
            if demand.source == "generator" and demand.structure == "location":
                task_entry_ids.update(item.location_id for item in resource.location_weights)
            if demand.source == "generator" and demand.structure == "od":
                task_entry_ids.update(item.origin_location_id for item in resource.od_weights)
            if not task_entry_ids <= set(resource.location_area_ids):
                raise ValueError(
                    "area-constrained demand requires explicit membership for every task entry"
                )

    def _resolve_specs(
        self,
        scenario: ScenarioConfig,
        resources: Mapping[str, FleetRuntimeResource],
        locations: Mapping[str, LocationRef],
    ) -> tuple[VehicleSpec, ...]:
        specs = []
        for fleet in scenario.fleets:
            resource = resources[fleet.fleet_id]
            if fleet.supply.source == "generated":
                initials = tuple(
                    locations[location_id]
                    for location_id in resource.generated_initial_location_ids
                )
                depots = (
                    resource.generated_depot_location_ids
                    if resource.generated_depot_location_ids
                    else None
                )
                _, generated = build_generated_catalog(
                    fleet.supply,
                    fleet_id=fleet.fleet_id,
                    initial_locations=initials,
                    depot_location_ids=depots,
                    area_assignments=resource.generated_area_assignments,
                )
                specs.extend(generated)
            else:
                specs.extend(resource.vehicle_specs)
        ordered = tuple(sorted(specs, key=lambda item: (item.key.fleet_id, item.key.vehicle_id)))
        if any(spec.key.fleet_id not in resources for spec in ordered):
            raise ValueError("vehicle catalog contains an unknown fleet")
        return ordered

    def _resolve_replications(
        self,
        scenario: ScenarioConfig,
        resources: Mapping[str, FleetRuntimeResource],
        specs: tuple[VehicleSpec, ...],
        scenario_hash: str,
    ) -> tuple[ReplicationInput, ...]:
        expected_task_count = sum(
            (
                sum(
                    interval.rate_tasks_per_s * (interval.end_s - interval.start_s)
                    for interval in fleet.demand.parameters.intervals
                )
                if fleet.demand.source == "generator"
                else len(resources[fleet.fleet_id].tasks)
            )
            for fleet in scenario.fleets
        )
        if expected_task_count > self.max_tasks_per_replication:
            raise ValueError(
                "expected task count exceeds max_tasks_per_replication before generation"
            )
        by_fleet_specs = {
            fleet.fleet_id: tuple(spec for spec in specs if spec.key.fleet_id == fleet.fleet_id)
            for fleet in scenario.fleets
        }
        replications = []
        for index in range(scenario.replications):
            replication_id = stable_id(
                "replication", {"scenario_hash": scenario_hash, "index": index}
            )
            joint_scenario_id = stable_id(
                "joint_scenario", {"scenario_hash": scenario_hash, "index": index}
            )
            rng = SemanticRngStreams(scenario.master_seed, namespace="simulation")
            tasks = []
            availability = []
            for fleet in scenario.fleets:
                resource = resources[fleet.fleet_id]
                if fleet.demand.source == "generator":
                    tasks.extend(
                        generate_piecewise_poisson(
                            fleet.demand,
                            fleet_id=fleet.fleet_id,
                            replication_id=replication_id,
                            capacity_mode=fleet.supply.capacity.mode,
                            rng=rng,
                            location_weights=resource.location_weights,
                            od_weights=resource.od_weights,
                        )
                    )
                else:
                    tasks.extend(resource.tasks)
                if fleet.supply.source == "generated":
                    availability.extend(
                        realize_availability(
                            fleet.supply,
                            by_fleet_specs[fleet.fleet_id],
                            replication_id=replication_id,
                            rng=rng,
                        )
                    )
                else:
                    availability.extend(
                        _availability_from_specs(by_fleet_specs[fleet.fleet_id], replication_id)
                    )
            ordered_tasks = tuple(
                sorted(tasks, key=lambda item: (item.release_s, item.fleet_id, item.task_id))
            )
            if len(ordered_tasks) > self.max_tasks_per_replication:
                raise ValueError("realized task count exceeds max_tasks_per_replication")
            ordered_availability = tuple(
                sorted(
                    availability,
                    key=lambda item: (item.vehicle.fleet_id, item.vehicle.vehicle_id),
                )
            )
            input_hash = scientific_hash(
                {
                    "replication_id": replication_id,
                    "joint_scenario_id": joint_scenario_id,
                    "tasks": ordered_tasks,
                    "availability": ordered_availability,
                    "joint_scenario_model": scenario.joint_scenario_model,
                }
            )
            replications.append(
                ReplicationInput(
                    replication_id=replication_id,
                    joint_scenario_id=joint_scenario_id,
                    tasks=ordered_tasks,
                    availability=ordered_availability,
                    input_hash=input_hash,
                    rng=rng,
                    online_fleet_ids=tuple(
                        fleet.fleet_id
                        for fleet in scenario.fleets
                        if fleet.demand.source == "generator"
                        and fleet.demand.generation_timing == "online"
                    ),
                )
            )
        return tuple(replications)

    def _impact_summaries(
        self,
        environment: PreparedEnvironment,
        scenario: ScenarioConfig,
        resources: Mapping[str, FleetRuntimeResource],
        specs: Sequence[VehicleSpec],
        replications: Sequence[ReplicationInput],
        locations: Mapping[str, LocationRef],
    ) -> tuple[dict[str, object], ...]:
        summaries = []
        for fleet in scenario.fleets:
            if (
                fleet.supply.idle_policy.policy == "random_cruise"
                and environment.metadata.network_readiness_grade != "strict_ready"
            ):
                raise ValueError(
                    "random cruise on a quarantined network lacks a finite scenario-impact scope"
                )
            fleet_specs = [spec for spec in specs if spec.key.fleet_id == fleet.fleet_id]
            fleet_tasks = {
                (task.task_id, task.model_dump_json()): task
                for replication in replications
                for task in replication.tasks
                if task.fleet_id == fleet.fleet_id
            }.values()
            pairs: set[tuple[str, str]] = set()

            def add_pair(left_id: str, right_id: str) -> None:
                left = locations[left_id].node_id
                right = locations[right_id].node_id
                assert left is not None and right is not None
                if left != right:
                    pairs.add((left, right))

            for task in fleet_tasks:
                for left, right in zip(task.steps, task.steps[1:]):
                    add_pair(left.location_id, right.location_id)
            if fleet.dispatch.policy == "predefined":
                plan = resources[fleet.fleet_id].assignment_plan
                assert plan is not None
                task_by_id = {task.task_id: task for task in fleet_tasks}
                rows_by_vehicle: dict[str, list] = {}
                for row in plan.rows:
                    rows_by_vehicle.setdefault(row.vehicle.vehicle_id, []).append(row)
                spec_by_id = {spec.key.vehicle_id: spec for spec in fleet_specs}
                for vehicle_id, rows in rows_by_vehicle.items():
                    current = spec_by_id[vehicle_id].initial_location_id
                    for row in sorted(rows, key=lambda item: item.order_index):
                        task = task_by_id[row.task_id]
                        add_pair(current, task.steps[0].location_id)
                        current = task.steps[-1].location_id
            # Dynamic pickup pairs are evaluated at dispatch against the current
            # snapshot. They are candidates, not mandatory routes: constructing
            # their fleet-wide Cartesian product is neither necessary nor bounded.
            if fleet.supply.depot_policy is not None:
                possible_origins = {spec.initial_location_id for spec in fleet_specs} | {
                    task.steps[-1].location_id for task in fleet_tasks
                }
                for spec in fleet_specs:
                    assert spec.depot_location_id is not None
                    for source in possible_origins:
                        add_pair(source, spec.depot_location_id)
            impact = validate_scenario_impact(
                environment.routing,
                profile_id=fleet.routing.profile_id,
                required_pairs=tuple(sorted(pairs)),
                quarantined_source_count=environment.metadata.quarantined_source_rows,
            )
            if not impact.passed:
                raise ValueError(
                    f"fleet {fleet.fleet_id!r} has {len(impact.unreachable_pairs)} "
                    "unreachable required route pair(s)"
                )
            summaries.append(
                {
                    "fleet_id": fleet.fleet_id,
                    "profile_id": fleet.routing.profile_id,
                    "required_pair_count": impact.required_pair_count,
                    "route_edge_count": len(impact.route_edge_ids),
                    "readiness_grade": impact.readiness_grade,
                    "unreachable_pair_count": len(impact.unreachable_pairs),
                    "dynamic_pickup_validation": "on demand in the shared eligibility engine",
                }
            )
        return tuple(summaries)

    def validate_scenario(
        self,
        environment: PreparedEnvironment,
        bundle: ScenarioResourceBundle,
    ) -> ValidatedScenario:
        bundle = ScenarioResourceBundle.model_validate(bundle)
        bundle = self._hydrate_bundle(environment, bundle)
        scenario = bundle.scenario
        if scenario.environment_id != environment.reference.artifact_id:
            raise ValueError("scenario environment_id does not match the prepared environment")
        if scenario.joint_scenario_model.kind != "independent_conditional_environment":
            raise ValueError("shared-factor scenario models are not installed in M07")
        resources = {item.fleet_id: item for item in bundle.fleets}
        for fleet in scenario.fleets:
            if fleet.routing.profile_id not in environment.metadata.profile_hashes:
                raise ValueError(f"unknown routing profile: {fleet.routing.profile_id!r}")
            self._validate_resource(scenario, fleet, resources[fleet.fleet_id])
        references = {
            (reference.artifact_id, reference.content_hash): reference
            for resource in bundle.fleets
            for reference in (resource.demand_artifact, resource.supply_artifact)
            if reference is not None
        }
        for reference in references.values():
            if reference.artifact_kind != "dataset":
                raise ValueError("demand and supply inputs must be dataset artifacts")
            verified_dataset_directory(reference, artifact_root=self.artifact_root)
        locations = {}
        for resource in bundle.fleets:
            for location in resource.locations:
                existing = locations.get(location.location_id)
                if existing is not None and existing != location:
                    raise ValueError("one location ID maps to conflicting resolved locations")
                if location.node_id is None:
                    raise ValueError("runtime locations must be resolved to network nodes")
                locations[location.location_id] = location
        specs = self._resolve_specs(scenario, resources, locations)
        spec_keys = {(spec.key.fleet_id, spec.key.vehicle_id) for spec in specs}
        for fleet in scenario.fleets:
            if fleet.dispatch.policy != "predefined":
                continue
            plan = resources[fleet.fleet_id].assignment_plan
            assert plan is not None
            task_ids = {task.task_id for task in resources[fleet.fleet_id].tasks}
            if {row.task_id for row in plan.rows} != task_ids:
                raise ValueError("predefined assignments must cover every static task exactly")
            if any(
                (row.vehicle.fleet_id, row.vehicle.vehicle_id) not in spec_keys for row in plan.rows
            ):
                raise ValueError("predefined assignment references a vehicle outside the catalog")
        catalog = _merge_catalog(specs)
        resource_hash = scientific_hash(bundle.fleets)
        scenario_hash = scientific_hash(
            {
                "scenario": scientific_projection(scenario),
                "resource_hash": resource_hash,
                "environment_mobility_hash": environment.metadata.mobility_hash,
            }
        )
        realization_scenario = scientific_projection(scenario)
        for fleet_value in realization_scenario["fleets"]:
            if fleet_value["demand"]["source"] == "generator":
                fleet_value["demand"]["generation_timing"] = "offline"
        realization_hash = scientific_hash(
            {
                "scenario": realization_scenario,
                "resource_hash": resource_hash,
                "environment_mobility_hash": environment.metadata.mobility_hash,
            }
        )
        replications = self._resolve_replications(scenario, resources, specs, realization_hash)
        assignment_plans = {
            resource.assignment_plan.assignment_plan_id: resource.assignment_plan
            for resource in resources.values()
            if resource.assignment_plan is not None
        }
        location_area_ids: dict[str, tuple[str, ...]] = {}
        for resource in bundle.fleets:
            for location_id, area_ids in resource.location_area_ids.items():
                existing = location_area_ids.get(location_id)
                if existing is not None and existing != area_ids:
                    raise ValueError("one location has conflicting area memberships")
                location_area_ids[location_id] = area_ids
        for replication in replications:
            SimulationDecisionAdapter(
                fleets=scenario.fleets,
                tasks=replication.tasks,
                vehicle_specs=specs,
                locations=locations,
                routing=environment.routing,
                assignment_plans=assignment_plans,
                location_area_ids=location_area_ids,
                replication_id=replication.replication_id,
                simulation_end_s=scenario.clock.end_s,
                availability=replication.availability,
                rng=replication.rng,
            )
        impacts = self._impact_summaries(
            environment,
            scenario,
            resources,
            specs,
            replications,
            locations,
        )
        assumptions_set = set(environment.metadata.assumptions).union(
            assumption for resource in resources.values() for assumption in resource.assumptions
        )
        if any(fleet.demand.source == "generator" for fleet in scenario.fleets):
            assumptions_set.add("synthetic_demand")
        if any(fleet.supply.source == "gtfs_duties" for fleet in scenario.fleets):
            assumptions_set.add("inferred_vehicle_duties")
        assumptions = tuple(sorted(assumptions_set))
        estimated_working_bytes = (
            environment.metadata.directed_edge_count * ESTIMATED_EDGE_BYTES
            + max(len(item.tasks) for item in replications) * ESTIMATED_TASK_BYTES
            + len(specs) * ESTIMATED_VEHICLE_BYTES
        )
        dependencies = [
            ArtifactDependency(
                role="environment",
                artifact_id=environment.reference.artifact_id,
                content_hash=environment.reference.content_hash,
            )
        ]
        for resource in bundle.fleets:
            for kind, reference in (
                ("demand", resource.demand_artifact),
                ("supply", resource.supply_artifact),
            ):
                if reference is not None:
                    dependencies.append(
                        ArtifactDependency(
                            role=f"{kind}_{resource.fleet_id}",
                            artifact_id=reference.artifact_id,
                            content_hash=reference.content_hash,
                        )
                    )
        resolved_config = {
            "scenario": scientific_projection(scenario),
            "resource_hash": resource_hash,
            "replication_input_hashes": {
                item.replication_id: item.input_hash for item in replications
            },
            "impact_summaries": list(impacts),
            "assumptions": list(assumptions),
        }
        identity = ScientificIdentity(
            schema_version="2.0",
            artifact_kind="dataset",
            resolved_config=resolved_config,
            resolved_config_hash=scientific_hash(resolved_config),
            dependency_hashes={item.role: item.content_hash for item in dependencies},
            algorithm_versions={
                "application": APPLICATION_VERSION,
                "dataset_runtime_loader": DATASET_RUNTIME_LOADER_VERSION,
                "scenario_validation": SCENARIO_VALIDATION_VERSION,
            },
            catalog_hash=catalog.catalog_hash,
        )
        validation = publish_partitioned_artifact(
            artifact_root=self.artifact_root,
            collection="scenario_validations",
            identity=identity,
            dependencies=tuple(dependencies),
            tables=(
                PartitionedTableData(
                    name="scenario_validation",
                    schema=VALIDATION_SCHEMA,
                    partition_axes=(),
                    rows_by_partition={
                        (): [
                            {
                                "scenario_hash": scenario_hash,
                                "catalog_id": catalog.catalog_id,
                                "catalog_hash": catalog.catalog_hash,
                                "replication_count": scenario.replications,
                                "fleet_count": len(scenario.fleets),
                                "vehicle_count": len(specs),
                                "task_count_by_replication_json": canonical_json_text(
                                    {item.replication_id: len(item.tasks) for item in replications}
                                ),
                                "impact_summaries_json": canonical_json_text(impacts),
                                "assumptions_json": canonical_json_text(assumptions),
                                "estimated_working_bytes": estimated_working_bytes,
                            }
                        ]
                    },
                    key_columns=("scenario_hash",),
                ),
            ),
        )
        return ValidatedScenario(
            reference=validation.reference,
            environment=environment,
            bundle=bundle,
            catalog=catalog,
            vehicle_specs=specs,
            locations=locations,
            location_area_ids=location_area_ids,
            assignment_plans=assignment_plans,
            replications=replications,
            scenario_hash=scenario_hash,
            assumptions=assumptions,
            impact_summaries=impacts,
            estimated_working_bytes=estimated_working_bytes,
        )

    def run_simulation(
        self,
        validated: ValidatedScenario,
        options: ExecutionOptions,
        *,
        cancellation=None,
        progress=None,
    ) -> SimulationRunResult:
        options = ExecutionOptions.model_validate(options)
        if options.job_timeout_s is not None:
            raise ValueError("job_timeout_s requires the persistent M09 coordinator")
        if options.workers > (os.cpu_count() or 1):
            raise ValueError("workers exceeds the detected logical CPU count")
        if options.memory_limit_bytes < validated.estimated_working_bytes * options.workers:
            raise ValueError(
                "configured memory limit is below the conservative worker-scaled scenario estimate"
            )
        cancellation = cancellation or _NeverCancelled()
        progress = progress or _NullProgress()
        scenario = validated.bundle.scenario
        results = []
        seed_manifests = {}
        started = time.perf_counter()
        arguments = [
            (
                index,
                replication,
                scenario,
                validated.vehicle_specs,
                validated.locations,
                validated.environment.routing,
                validated.assignment_plans,
                validated.location_area_ids,
                options.progress_frequency_events,
                cancellation,
            )
            for index, replication in enumerate(validated.replications)
        ]
        if options.workers == 1:
            completed = (_execute_replication(item) for item in arguments)
            for index, result, seed_manifest in completed:
                if cancellation.is_cancelled():
                    raise RuntimeError("simulation cancelled during replication execution")
                results.append(result)
                seed_manifests[result.replication_id] = seed_manifest
                progress.update(
                    phase="simulation.replications",
                    completed=index + 1,
                    total=len(validated.replications),
                )
        else:
            if cancellation.is_cancelled():
                raise RuntimeError("simulation cancelled before replication execution")
            with ProcessPoolExecutor(
                max_workers=min(options.workers, len(arguments)),
                mp_context=get_context("spawn"),
                initializer=_initialize_replication_worker,
            ) as pool:
                spawned_arguments = [
                    (
                        str(self.artifact_root),
                        validated.environment.reference,
                        argument[:5] + argument[6:-1],
                        (
                            str(path)
                            if (path := getattr(cancellation, "path", None)) is not None
                            else None
                        ),
                    )
                    for argument in arguments
                ]
                for completed_count, (_, result, seed_manifest) in enumerate(
                    pool.map(_execute_replication_spawn, spawned_arguments, chunksize=1), start=1
                ):
                    if cancellation.is_cancelled():
                        pool.shutdown(wait=False, cancel_futures=True)
                        raise RuntimeError("simulation cancelled during replication execution")
                    results.append(result)
                    seed_manifests[result.replication_id] = seed_manifest
                    progress.update(
                        phase="simulation.replications",
                        completed=completed_count,
                        total=len(validated.replications),
                    )
        return self._publish_completed_simulation(
            validated,
            tuple(results),
            seed_manifests,
            started=started,
            worker_count=options.workers,
        )

    def _publish_completed_simulation(
        self,
        validated: ValidatedScenario,
        results,
        seed_manifests,
        *,
        started: float,
        worker_count: int,
    ) -> SimulationRunResult:
        """Coordinator-only publication after every replication is complete."""

        scenario = validated.bundle.scenario
        identities = tuple(
            JointReplicationIdentity(
                replication_id=item.replication_id,
                joint_scenario_id=item.joint_scenario_id,
                scenario_realization_hash=item.input_hash,
                catalog_hash=validated.catalog.catalog_hash,
            )
            for item in validated.replications
        )
        environment = validated.environment.reference
        reference = publish_simulation_results(
            artifact_root=self.artifact_root,
            catalog=validated.catalog,
            replications=identities,
            results=tuple(results),
            seed_manifests=seed_manifests,
            resolved_config={
                "scenario": scientific_projection(scenario),
                "scenario_validation_id": validated.reference.artifact_id,
                "scenario_hash": validated.scenario_hash,
                "assumptions": list(validated.assumptions),
            },
            dependencies=(
                ArtifactDependency(
                    role="environment",
                    artifact_id=environment.artifact_id,
                    content_hash=environment.content_hash,
                ),
                ArtifactDependency(
                    role="scenario_validation",
                    artifact_id=validated.reference.artifact_id,
                    content_hash=validated.reference.content_hash,
                ),
            ),
            worker_count=worker_count,
        )
        return SimulationRunResult(
            reference=reference,
            results=tuple(results),
            elapsed_s=time.perf_counter() - started,
            estimated_working_bytes=validated.estimated_working_bytes,
        )

    def allocate_exposure(
        self,
        environment: PreparedEnvironment,
        simulation: ArtifactRef,
        config: ExposureConfig,
    ) -> ArtifactRef:
        if config.sensing_geometry_id != environment.metadata.sensing_hash:
            raise ValueError(
                "default M07 exposure requires the prepared environment sensing geometry"
            )
        return publish_exposure_artifact(
            artifact_root=self.artifact_root,
            simulation=simulation,
            environment=environment.reference,
            road_edges=environment.road_edges,
            grid_cells=environment.grid_cells,
            working_crs=environment.metadata.working_crs,
            config=config,
        )

    def preview_portfolios(
        self,
        exposure: ArtifactRef,
        config: PortfolioConfig,
        *,
        limits: PortfolioResourceLimits | None = None,
    ) -> PortfolioPreview:
        """Resolve M08A enumeration and resource bounds without drawing samples."""

        return build_portfolio_plan(
            artifact_root=self.artifact_root,
            exposure=exposure,
            config=config,
            limits=limits,
        ).preview

    def evaluate_portfolio_samples(
        self,
        exposure: ArtifactRef,
        config: PortfolioConfig,
        weights: UtilityWeightResource,
        *,
        limits: PortfolioResourceLimits | None = None,
    ) -> PortfolioEvaluationResult:
        """Publish exactly J random-allocation samples without invoking mobility."""

        return publish_portfolio_samples(
            artifact_root=self.artifact_root,
            exposure=exposure,
            config=config,
            weights=weights,
            limits=limits,
        )

    def summarize_portfolios(
        self,
        samples: ArtifactRef,
        config: PortfolioConfig,
    ) -> PortfolioAnalysisResult:
        """Publish M08B statistics/frontiers from immutable M08A samples only."""

        return publish_portfolio_analysis(
            artifact_root=self.artifact_root,
            samples=samples,
            config=config,
        )
