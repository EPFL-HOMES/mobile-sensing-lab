from __future__ import annotations

from datetime import datetime, timezone

import pytest

from mobile_sensing.contracts import (
    ClockConfig,
    FleetConfig,
    LocationRef,
    ResolutionStatus,
    RouteEdge,
    RouteResult,
    Task,
    TaskStep,
    VehicleAvailability,
    VehicleKey,
    VehicleSpec,
)
from mobile_sensing.simulation import (
    DEPOT_RETURN_VERSION,
    RANDOM_CRUISE_VERSION,
    ExecutionLifecycleStatus,
    ScenarioDispatchError,
    SemanticRngStreams,
    SimulationDecisionAdapter,
    TaskLifecycleStatus,
    run_event_kernel,
)


HASH = "2" * 64


def _location(node_id: str) -> LocationRef:
    x = float(sum(ord(char) for char in node_id))
    return LocationRef(
        location_id=node_id,
        original_x=x,
        original_y=0.0,
        original_crs="EPSG:2056",
        node_id=node_id,
        snapped_x=x,
        snapped_y=0.0,
        snap_distance_m=0.0,
        resolution_status=ResolutionStatus.RESOLVED,
    )


class GraphRouting:
    def __init__(self, durations: dict[tuple[str, str], float]) -> None:
        self.durations = durations

    def route(self, profile_id: str, source_node_id: str, target_node_id: str) -> RouteResult:
        if source_node_id == target_node_id:
            edges = ()
        elif (source_node_id, target_node_id) in self.durations:
            duration = self.durations[(source_node_id, target_node_id)]
            edges = (
                RouteEdge(
                    edge_id=f"edge_{source_node_id}_{target_node_id}",
                    length_m=duration,
                    duration_s=duration,
                ),
            )
        else:
            return RouteResult(
                reachable=False,
                edges=(),
                total_duration_s=0.0,
                total_distance_m=0.0,
                network_hash=HASH,
                profile_hash=HASH,
                reason="no_path",
            )
        return RouteResult(
            reachable=True,
            edges=edges,
            total_duration_s=sum(edge.duration_s for edge in edges),
            total_distance_m=sum(edge.length_m for edge in edges),
            network_hash=HASH,
            profile_hash=HASH,
        )

    def routes_from(
        self, profile_id: str, source_node_id: str, target_node_ids: tuple[str, ...]
    ) -> dict[str, RouteResult]:
        return {
            target: self.route(profile_id, source_node_id, target) for target in target_node_ids
        }

    def outgoing_neighbor_node_ids(self, profile_id: str, source_node_id: str) -> tuple[str, ...]:
        return tuple(
            sorted(target for source, target in self.durations if source == source_node_id)
        )

    def location_for_node(self, node_id: str) -> LocationRef:
        return _location(node_id)


class RouteOnlyRouting:
    def route(self, profile_id: str, source_node_id: str, target_node_id: str) -> RouteResult:
        return GraphRouting({}).route(profile_id, source_node_id, target_node_id)


class FirstChoiceRng:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def stream(self, *semantic_labels: str):
        self.calls.append(semantic_labels)
        return self

    def integers(self, low: int, high: int) -> int:
        assert low == 0 and high > 0
        return 0


def _fleet(
    *,
    idle_policy: str = "stationary",
    depot_policy: bool = False,
    capacity_mode: str = "none",
    areas: bool = False,
) -> FleetConfig:
    capacity: dict[str, object] = {"mode": capacity_mode}
    if capacity_mode != "none":
        capacity["unit"] = "units"
    if capacity_mode == "consumable":
        capacity["replenishment_duration_s"] = 5.0
    return FleetConfig.model_validate(
        {
            "fleet_id": "fleet",
            "label": "Operational fleet",
            "demand": {
                "source": "generator",
                "structure": "location",
                "adapter": "demand.poisson_piecewise_constant@1",
                "generation_timing": "offline",
                "parameters": {
                    "intervals": ({"start_s": 0.0, "end_s": 20.0, "rate_tasks_per_s": 0.0},),
                    "location_weights_ref": "weights",
                },
            },
            "supply": {
                "source": "generated",
                "catalog_size": 1,
                "catalog_id_namespace": "fleet",
                "availability": {"kind": "simultaneous", "start_s": 0.0, "end_s": 20.0},
                "initial_locations_ref": "initials",
                "capacity": capacity,
                "capacity_value": None if capacity_mode == "none" else 10.0,
                "area_definitions_ref": "areas" if areas else None,
                "area_assignments_ref": "area_assignments" if areas else None,
                "idle_policy": {"policy": idle_policy},
                "depot_policy": {"policy": "return_when_blocked"} if depot_policy else None,
            },
            "dispatch": {"policy": "nearest_matching"},
            "routing": {"profile_id": "profile"},
        }
    )


def _vehicle(
    *,
    end_s: float,
    capacity_mode: str = "none",
    depot: str | None = None,
    areas: tuple[str, ...] = (),
) -> VehicleSpec:
    return VehicleSpec(
        key=VehicleKey(fleet_id="fleet", vehicle_id="vehicle"),
        availability_start_s=0.0,
        availability_end_s=end_s,
        initial_location_id="A",
        depot_location_id=depot,
        capacity_mode=capacity_mode,
        capacity=10.0 if capacity_mode != "none" else None,
        quantity_unit="units" if capacity_mode != "none" else None,
        assigned_area_ids=areas,
        identity_provenance="uploaded",
    )


def _availability(spec: VehicleSpec) -> VehicleAvailability:
    return VehicleAvailability(
        replication_id="rep",
        vehicle=spec.key,
        active=True,
        availability_start_s=0.0,
        availability_end_s=spec.availability_end_s,
        initial_location_id=spec.initial_location_id,
    )


def _task(
    task_id: str,
    *,
    location: str = "A",
    release_s: float = 0.0,
    service_s: float = 1.0,
    required: float | None = None,
) -> Task:
    return Task(
        task_id=task_id,
        fleet_id="fleet",
        release_s=release_s,
        steps=(
            TaskStep(
                step_index=1,
                location_id=location,
                service_duration_s=service_s,
                quantity_delta=-required if required is not None else None,
            ),
        ),
        required_capacity=required,
    )


def _clock(end_s: float) -> ClockConfig:
    return ClockConfig(
        origin_utc=datetime(2026, 1, 1, tzinfo=timezone.utc),
        display_timezone="UTC",
        simulation_start_s=0.0,
        observation_start_s=0.0,
        end_s=end_s,
    )


def _adapter(
    *,
    fleet: FleetConfig,
    tasks: tuple[Task, ...],
    spec: VehicleSpec,
    locations: dict[str, LocationRef],
    routing: GraphRouting,
    end_s: float,
    seed: int = 7,
) -> tuple[SimulationDecisionAdapter, SemanticRngStreams]:
    rng = SemanticRngStreams(seed)
    adapter = SimulationDecisionAdapter(
        fleets=(fleet,),
        tasks=tasks,
        vehicle_specs=(spec,),
        locations=locations,
        routing=routing,
        location_area_ids={"A": ("north",)} if spec.assigned_area_ids else {},
        replication_id="rep",
        simulation_end_s=end_s,
        availability=(_availability(spec),),
        rng=rng,
    )
    return adapter, rng


def _run(
    *,
    fleet: FleetConfig,
    tasks: tuple[Task, ...],
    spec: VehicleSpec,
    locations: dict[str, LocationRef],
    routing: GraphRouting,
    end_s: float,
    seed: int = 7,
):
    adapter, rng = _adapter(
        fleet=fleet,
        tasks=tasks,
        spec=spec,
        locations=locations,
        routing=routing,
        end_s=end_s,
        seed=seed,
    )
    result = run_event_kernel(
        replication_id="rep",
        clock=_clock(end_s),
        tasks=tasks,
        vehicle_specs=(spec,),
        availability=(_availability(spec),),
        decisions=adapter,
    )
    return result, adapter, rng


def test_stationary_idle_has_one_closed_interval_and_no_periodic_events() -> None:
    fleet = _fleet()
    spec = _vehicle(end_s=10.0)
    result, adapter, _ = _run(
        fleet=fleet,
        tasks=(),
        spec=spec,
        locations={"A": _location("A")},
        routing=GraphRouting({}),
        end_s=10.0,
    )
    assert [
        (row.start_s, row.end_s, row.location_id) for row in result.idle_activity_intervals
    ] == [(0.0, 10.0, "A")]
    assert [row.event_type for row in result.lifecycle_events] == [
        "vehicle_entry",
        "vehicle_exit",
    ]
    assert adapter.operational_diagnostics[-1].code == "STATIONARY_IDLE"


def test_random_cruise_is_reproducible_avoids_immediate_reversal_and_leaves_area() -> None:
    fleet = _fleet(idle_policy="random_cruise", areas=True)
    spec = _vehicle(end_s=5.0, areas=("north",))
    locations = {"A": _location("A")}
    routes = {("A", "B"): 1.0, ("B", "A"): 1.0, ("B", "C"): 1.0}
    first, first_adapter, first_rng = _run(
        fleet=fleet,
        tasks=(),
        spec=spec,
        locations=locations,
        routing=GraphRouting(routes),
        end_s=5.0,
    )
    second, _, second_rng = _run(
        fleet=fleet,
        tasks=(),
        spec=spec,
        locations=locations,
        routing=GraphRouting(routes),
        end_s=5.0,
    )
    assert first == second
    cruise = sorted(first.execution_outcomes, key=lambda row: row.assigned_at_s)
    assert [
        first_adapter.locations[row.realized_position.location_id].node_id for row in cruise
    ] == ["C"]
    assert first.task_outcomes == ()
    assert not any(row.task_id for row in first.lifecycle_events)
    assert all(
        interval.movement_kind == "cruise"
        for row in cruise
        for interval in row.realized_intervals
        if interval.kind == "movement"
    )
    assert first_rng.manifest == second_rng.manifest
    assert len(first_rng.manifest.streams) == 2
    assert {row.semantic_labels for row in first_rng.manifest.streams} == {
        ("rep", "fleet", "vehicle", RANDOM_CRUISE_VERSION, "0"),
        ("rep", "fleet", "vehicle", RANDOM_CRUISE_VERSION, "1"),
    }
    assert first.idle_activity_intervals == ()
    assert len(cruise[0].realized_intervals) == 3
    assert cruise[0].realized_intervals[-1].kind == "wait"


def test_mid_cruise_release_waits_until_nonpreemptive_leg_completion() -> None:
    fleet = _fleet(idle_policy="random_cruise")
    spec = _vehicle(end_s=10.0)
    task = _task("service", location="B", release_s=2.0, service_s=1.0)
    result, _, _ = _run(
        fleet=fleet,
        tasks=(task,),
        spec=spec,
        locations={node: _location(node) for node in "AB"},
        routing=GraphRouting({("A", "B"): 5.0}),
        end_s=10.0,
    )
    service = next(row for row in result.task_outcomes if row.task_id == "service")
    cruise = next(
        row
        for row in result.execution_outcomes
        if row.fleet_id == "fleet" and row.task_id != "service"
    )
    assert cruise.realized_end_s == 5.0
    assert service.assigned_at_s == 5.0
    assert service.status == TaskLifecycleStatus.COMPLETED


def test_service_relocation_invalidates_stale_cruise_reversal_context() -> None:
    fleet = _fleet(idle_policy="random_cruise")
    spec = _vehicle(end_s=3.0)
    task = _task("service", location="C", release_s=0.5, service_s=0.0)
    locations = {node: _location(node) for node in "ACD"}
    routing = GraphRouting(
        {
            ("A", "B"): 1.0,
            ("B", "C"): 1.0,
            ("C", "A"): 1.0,
            ("C", "D"): 1.0,
        }
    )
    rng = FirstChoiceRng()
    adapter = SimulationDecisionAdapter(
        fleets=(fleet,),
        tasks=(task,),
        vehicle_specs=(spec,),
        locations=locations,
        routing=routing,
        replication_id="rep",
        simulation_end_s=3.0,
        availability=(_availability(spec),),
        rng=rng,
    )
    result = run_event_kernel(
        replication_id="rep",
        clock=_clock(3.0),
        tasks=(task,),
        vehicle_specs=(spec,),
        availability=(_availability(spec),),
        decisions=adapter,
    )
    cruise = [row for row in result.execution_outcomes if row.task_id != "service"]
    assert [adapter.locations[row.realized_position.location_id].node_id for row in cruise] == [
        "B",
        "A",
    ]
    assert len(rng.calls) == 2


def test_random_cruise_requires_operational_routing_extension_pre_run() -> None:
    fleet = _fleet(idle_policy="random_cruise")
    spec = _vehicle(end_s=5.0)
    with pytest.raises(ScenarioDispatchError, match="routing neighbor queries"):
        SimulationDecisionAdapter(
            fleets=(fleet,),
            tasks=(),
            vehicle_specs=(spec,),
            locations={"A": _location("A")},
            routing=RouteOnlyRouting(),
            replication_id="rep",
            simulation_end_s=5.0,
            availability=(_availability(spec),),
            rng=SemanticRngStreams(7),
        )


@pytest.mark.parametrize(
    ("routes", "expected_code"),
    [({}, "CRUISE_NO_FEASIBLE_NEIGHBOR"), ({("A", "B"): 10.0}, "CRUISE_NO_FEASIBLE_NEIGHBOR")],
)
def test_cruise_dead_end_or_leg_beyond_window_stays_stationary_without_spin(
    routes: dict[tuple[str, str], float], expected_code: str
) -> None:
    fleet = _fleet(idle_policy="random_cruise")
    spec = _vehicle(end_s=5.0)
    result, adapter, rng = _run(
        fleet=fleet,
        tasks=(),
        spec=spec,
        locations={node: _location(node) for node in "AB"},
        routing=GraphRouting(routes),
        end_s=5.0,
    )
    assert result.execution_outcomes == ()
    assert result.processed_heap_events == 1
    assert [(row.start_s, row.end_s) for row in result.idle_activity_intervals] == [(0.0, 5.0)]
    assert adapter.operational_diagnostics[-2].code == expected_code
    assert rng.manifest.streams == ()


def test_same_depot_replenishment_is_positive_resets_only_at_completion_and_has_priority() -> None:
    fleet = _fleet(idle_policy="random_cruise", depot_policy=True, capacity_mode="consumable")
    spec = _vehicle(end_s=10.0, capacity_mode="consumable", depot="A")
    tasks = (
        _task("a_consume", required=8.0),
        _task("b_blocked", required=5.0),
    )
    result, _, _ = _run(
        fleet=fleet,
        tasks=tasks,
        spec=spec,
        locations={node: _location(node) for node in "AB"},
        routing=GraphRouting({("A", "B"): 1.0}),
        end_s=10.0,
    )
    depot = next(
        row for row in result.execution_outcomes if row.task_id not in {"a_consume", "b_blocked"}
    )
    assert depot.assigned_at_s == 1.0
    assert depot.realized_end_s == 6.0
    assert [row.kind for row in depot.realized_intervals] == ["service"]
    assert depot.realized_intervals[0].start_s == 1.0
    assert depot.applied_capacity_milestones[0].reset_to_capacity
    depot_task = next(row for row in result.task_outcomes if row.task_id == depot.task_id)
    assert depot_task.source_policy == DEPOT_RETURN_VERSION
    blocked = next(row for row in result.task_outcomes if row.task_id == "b_blocked")
    assert blocked.assigned_at_s == 6.0
    assert result.vehicle_outcomes[0].state.remaining_capacity == 5.0


def test_reachable_depot_return_routes_and_replenishes_through_generic_executor() -> None:
    fleet = _fleet(depot_policy=True, capacity_mode="consumable")
    spec = _vehicle(end_s=20.0, capacity_mode="consumable", depot="D")
    tasks = (
        _task("a_consume", required=8.0),
        _task("b_blocked", location="B", required=5.0),
    )
    result, _, _ = _run(
        fleet=fleet,
        tasks=tasks,
        spec=spec,
        locations={node: _location(node) for node in "ABD"},
        routing=GraphRouting({("A", "B"): 4.0, ("A", "D"): 2.0, ("D", "B"): 1.0}),
        end_s=20.0,
    )
    depot = next(
        row for row in result.execution_outcomes if row.task_id not in {"a_consume", "b_blocked"}
    )
    assert [row.movement_kind for row in depot.realized_intervals if row.kind == "movement"] == [
        "depot_return"
    ]
    assert [row.kind for row in depot.realized_intervals] == ["movement", "service"]
    assert depot.realized_end_s == 8.0
    blocked = next(row for row in result.task_outcomes if row.task_id == "b_blocked")
    assert blocked.assigned_at_s == 8.0


def test_interrupted_replenishment_does_not_reset_capacity() -> None:
    fleet = _fleet(depot_policy=True, capacity_mode="consumable")
    spec = _vehicle(end_s=4.0, capacity_mode="consumable", depot="A")
    tasks = (
        _task("a_consume", required=8.0),
        _task("b_blocked", required=5.0),
    )
    result, _, _ = _run(
        fleet=fleet,
        tasks=tasks,
        spec=spec,
        locations={"A": _location("A")},
        routing=GraphRouting({}),
        end_s=10.0,
    )
    depot = next(
        row for row in result.execution_outcomes if row.task_id not in {"a_consume", "b_blocked"}
    )
    assert depot.status == ExecutionLifecycleStatus.INTERRUPTED_SHIFT
    assert depot.realized_end_s == 4.0
    assert depot.applied_capacity_milestones == ()
    assert result.vehicle_outcomes[0].state.remaining_capacity == 2.0
    blocked = next(row for row in result.task_outcomes if row.task_id == "b_blocked")
    assert blocked.status == TaskLifecycleStatus.UNSERVED_HORIZON


def test_unreachable_depot_and_missing_replenishment_are_explicit_diagnostics() -> None:
    fleet = _fleet(depot_policy=True, capacity_mode="consumable")
    spec = _vehicle(end_s=10.0, capacity_mode="consumable", depot="D")
    tasks = (
        _task("a_consume", required=8.0),
        _task("b_blocked", location="B", required=5.0),
    )
    result, adapter, _ = _run(
        fleet=fleet,
        tasks=tasks,
        spec=spec,
        locations={node: _location(node) for node in "ABD"},
        routing=GraphRouting({("A", "B"): 1.0}),
        end_s=10.0,
    )
    assert not any(row.source_policy == DEPOT_RETURN_VERSION for row in result.task_outcomes)
    assert any(row.code == "DEPOT_UNREACHABLE" for row in adapter.operational_diagnostics)

    no_depot_fleet = _fleet(capacity_mode="consumable")
    no_depot_spec = _vehicle(end_s=10.0, capacity_mode="consumable")
    no_depot, _ = _adapter(
        fleet=no_depot_fleet,
        tasks=(),
        spec=no_depot_spec,
        locations={"A": _location("A")},
        routing=GraphRouting({}),
        end_s=10.0,
    )
    assert no_depot.operational_diagnostics[0].code == "REPLENISHMENT_NOT_CONFIGURED"


def test_full_stock_never_returns_without_demand_and_oversized_task_fails_pre_run() -> None:
    fleet = _fleet(depot_policy=True, capacity_mode="consumable")
    spec = _vehicle(end_s=10.0, capacity_mode="consumable", depot="A")
    result, _, _ = _run(
        fleet=fleet,
        tasks=(),
        spec=spec,
        locations={"A": _location("A")},
        routing=GraphRouting({}),
        end_s=10.0,
    )
    assert result.execution_outcomes == ()

    with pytest.raises(ScenarioDispatchError, match="full-capacity bounds"):
        _adapter(
            fleet=fleet,
            tasks=(_task("too_large", required=11.0),),
            spec=spec,
            locations={"A": _location("A")},
            routing=GraphRouting({}),
            end_s=10.0,
        )
