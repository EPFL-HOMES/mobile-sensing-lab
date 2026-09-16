"""Typed headless resource bindings and completed-use-case results."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

from pydantic import Field, field_validator, model_validator

from mobile_sensing.contracts import (
    ArtifactRef,
    AssignmentPlan,
    CatalogIdentity,
    ContractModel,
    LocationRef,
    ScenarioConfig,
    SchemaVersion,
    Task,
    VehicleAvailability,
    VehicleSpec,
)
from mobile_sensing.simulation import KernelResult, LocationWeight, OdWeight
from mobile_sensing.simulation.rng import SemanticRngStreams

if TYPE_CHECKING:
    from mobile_sensing.environment import PreparedEnvironment


class FleetRuntimeResource(ContractModel):
    """Scientific objects bound to the opaque references in one fleet config."""

    schema_version: SchemaVersion = "2.0"
    fleet_id: str
    demand_artifact: ArtifactRef | None = None
    supply_artifact: ArtifactRef | None = None
    weight_source_id: str | None = None
    initial_locations_source_id: str | None = None
    selected_task_ids: tuple[str, ...] = ()
    selected_vehicle_ids: tuple[str, ...] = ()
    locations: tuple[LocationRef, ...] = ()
    tasks: tuple[Task, ...] = ()
    vehicle_specs: tuple[VehicleSpec, ...] = ()
    generated_initial_location_ids: tuple[str, ...] = ()
    generated_depot_location_ids: tuple[str | None, ...] = ()
    generated_area_assignments: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    location_area_ids: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    location_weights: tuple[LocationWeight, ...] = ()
    od_weights: tuple[OdWeight, ...] = ()
    assignment_plan: AssignmentPlan | None = None
    assumptions: tuple[str, ...] = ()

    @field_validator("selected_task_ids", "selected_vehicle_ids")
    @classmethod
    def canonical_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(item == "" for item in value) or tuple(sorted(set(value))) != value:
            raise ValueError("runtime selections must be nonempty, canonical, and unique")
        return value

    @field_validator("locations")
    @classmethod
    def canonical_locations(cls, value: tuple[LocationRef, ...]) -> tuple[LocationRef, ...]:
        ids = tuple(item.location_id for item in value)
        if len(ids) != len(set(ids)):
            raise ValueError("runtime location IDs must be unique")
        return tuple(sorted(value, key=lambda item: item.location_id))

    @field_validator("tasks")
    @classmethod
    def canonical_tasks(cls, value: tuple[Task, ...]) -> tuple[Task, ...]:
        keys = tuple((item.fleet_id, item.task_id) for item in value)
        if len(keys) != len(set(keys)):
            raise ValueError("runtime task keys must be unique")
        return tuple(sorted(value, key=lambda item: (item.fleet_id, item.task_id)))

    @field_validator("vehicle_specs")
    @classmethod
    def canonical_vehicles(cls, value: tuple[VehicleSpec, ...]) -> tuple[VehicleSpec, ...]:
        keys = tuple((item.key.fleet_id, item.key.vehicle_id) for item in value)
        if len(keys) != len(set(keys)):
            raise ValueError("runtime vehicle keys must be unique")
        return tuple(sorted(value, key=lambda item: (item.key.fleet_id, item.key.vehicle_id)))

    @field_validator("location_weights")
    @classmethod
    def canonical_location_weights(
        cls, value: tuple[LocationWeight, ...]
    ) -> tuple[LocationWeight, ...]:
        ids = tuple(item.location_id for item in value)
        if len(ids) != len(set(ids)):
            raise ValueError("runtime location weights require unique locations")
        return tuple(sorted(value, key=lambda item: item.location_id))

    @field_validator("od_weights")
    @classmethod
    def canonical_od_weights(cls, value: tuple[OdWeight, ...]) -> tuple[OdWeight, ...]:
        keys = tuple((item.origin_location_id, item.destination_location_id) for item in value)
        if len(keys) != len(set(keys)):
            raise ValueError("runtime OD weights require unique pairs")
        return tuple(
            sorted(value, key=lambda item: (item.origin_location_id, item.destination_location_id))
        )

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        if self.fleet_id == "":
            raise ValueError("runtime fleet_id must be nonempty")
        if (
            any(item == "" for item in self.assumptions)
            or tuple(sorted(set(self.assumptions))) != self.assumptions
        ):
            raise ValueError("runtime assumptions must be canonical and unique")
        if any(
            tuple(sorted(set(area_ids))) != area_ids for area_ids in self.location_area_ids.values()
        ):
            raise ValueError("location area memberships must be canonical and unique")
        if any(
            tuple(sorted(set(area_ids))) != area_ids
            for area_ids in self.generated_area_assignments.values()
        ):
            raise ValueError("generated area assignments must be canonical and unique")
        if self.generated_depot_location_ids and len(self.generated_depot_location_ids) != len(
            self.generated_initial_location_ids
        ):
            raise ValueError("generated initial/depot location pairs must have equal length")
        if self.generated_initial_location_ids:
            depots = self.generated_depot_location_ids or (None,) * len(
                self.generated_initial_location_ids
            )
            pairs = tuple(sorted(zip(self.generated_initial_location_ids, depots, strict=True)))
            object.__setattr__(self, "generated_initial_location_ids", tuple(x[0] for x in pairs))
            if self.generated_depot_location_ids:
                object.__setattr__(self, "generated_depot_location_ids", tuple(x[1] for x in pairs))
        if self.assignment_plan is not None:
            rows = tuple(
                sorted(
                    self.assignment_plan.rows,
                    key=lambda row: (
                        row.vehicle.fleet_id,
                        row.vehicle.vehicle_id,
                        row.order_index,
                    ),
                )
            )
            object.__setattr__(
                self,
                "assignment_plan",
                AssignmentPlan(
                    assignment_plan_id=self.assignment_plan.assignment_plan_id,
                    rows=rows,
                ),
            )
        return self


class ScenarioResourceBundle(ContractModel):
    """Serializable bindings used identically by Python and CLI execution."""

    schema_version: SchemaVersion = "2.0"
    scenario: ScenarioConfig
    fleets: tuple[FleetRuntimeResource, ...]

    @field_validator("fleets")
    @classmethod
    def validate_fleets(
        cls, value: tuple[FleetRuntimeResource, ...]
    ) -> tuple[FleetRuntimeResource, ...]:
        ids = tuple(item.fleet_id for item in value)
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("scenario resources require unique fleet IDs")
        return tuple(sorted(value, key=lambda item: item.fleet_id))

    @model_validator(mode="after")
    def validate_alignment(self) -> Self:
        if {item.fleet_id for item in self.fleets} != {
            item.fleet_id for item in self.scenario.fleets
        }:
            raise ValueError("scenario config and runtime resources require identical fleets")
        return self


@dataclass(frozen=True, slots=True)
class ReplicationInput:
    replication_id: str
    joint_scenario_id: str
    tasks: tuple[Task, ...]
    availability: tuple[VehicleAvailability, ...]
    input_hash: str
    rng: SemanticRngStreams
    online_fleet_ids: tuple[str, ...] = ()
    assignment_plans: tuple[AssignmentPlan, ...] = ()


@dataclass(frozen=True, slots=True)
class ValidatedScenario:
    reference: ArtifactRef
    environment: "PreparedEnvironment"
    bundle: ScenarioResourceBundle
    catalog: CatalogIdentity
    vehicle_specs: tuple[VehicleSpec, ...]
    locations: dict[str, LocationRef]
    location_area_ids: dict[str, tuple[str, ...]]
    assignment_plans: dict[str, AssignmentPlan]
    replications: tuple[ReplicationInput, ...]
    scenario_hash: str
    assumptions: tuple[str, ...]
    impact_summaries: tuple[dict[str, object], ...]
    estimated_working_bytes: int


@dataclass(frozen=True, slots=True)
class SimulationRunResult:
    reference: ArtifactRef
    results: tuple[KernelResult, ...]
    elapsed_s: float
    estimated_working_bytes: int
