from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mobile_sensing.contracts import (
    AssignmentPlan,
    AssignmentRow,
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
    VehicleState,
    VehicleStatus,
)
from mobile_sensing.simulation import (
    DecisionSnapshot,
    ExecutionLifecycleStatus,
    GenericTaskExecutor,
    MatchingPairLimitExceeded,
    ScenarioDispatchError,
    SharedRoutingCache,
    SimulationDecisionAdapter,
    TaskLifecycleStatus,
    estimate_task_timing,
    run_event_kernel,
)


FIXTURE = Path(__file__).parent / "fixtures" / "m05b" / "mixed_operation_tables.json"
HASH = "1" * 64


class MatrixRouting:
    def __init__(self, durations: dict[tuple[str, str], float]) -> None:
        self.durations = durations
        self.calls: list[tuple[str, str, str]] = []
        self.bulk_calls: list[tuple[str, str, tuple[str, ...]]] = []

    def route(self, profile_id: str, source_node_id: str, target_node_id: str) -> RouteResult:
        self.calls.append((profile_id, source_node_id, target_node_id))
        if source_node_id == target_node_id:
            edges = ()
        elif (source_node_id, target_node_id) not in self.durations:
            return RouteResult(
                reachable=False,
                edges=(),
                total_duration_s=0.0,
                total_distance_m=0.0,
                network_hash=HASH,
                profile_hash=HASH,
                reason="no_path",
            )
        else:
            duration = self.durations[(source_node_id, target_node_id)]
            edges = (
                RouteEdge(
                    edge_id=f"edge_{source_node_id}_{target_node_id}",
                    length_m=duration * 2.0,
                    duration_s=duration,
                ),
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
        self.bulk_calls.append((profile_id, source_node_id, target_node_ids))
        return {
            target: self.route(profile_id, source_node_id, target) for target in target_node_ids
        }


def _location(name: str) -> LocationRef:
    index = int(name[1:])
    return LocationRef(
        location_id=name,
        original_x=float(index),
        original_y=0.0,
        original_crs="EPSG:2056",
        node_id=name,
        snapped_x=float(index),
        snapped_y=0.0,
        snap_distance_m=0.0,
        resolution_status=ResolutionStatus.RESOLVED,
    )


def _fleet(
    fleet_id: str,
    *,
    policy: str = "nearest_matching",
    plan_ref: str | None = None,
    capacity_mode: str = "none",
    areas: bool = False,
    max_pickup: float | None = None,
) -> FleetConfig:
    dispatch = (
        {"policy": "predefined", "assignment_plan_ref": plan_ref}
        if policy == "predefined"
        else {"policy": "nearest_matching", "max_pickup_time_s": max_pickup}
    )
    capacity = {"mode": capacity_mode}
    if capacity_mode != "none":
        capacity["unit"] = "units"
    if capacity_mode == "consumable":
        capacity["replenishment_duration_s"] = 5.0
    return FleetConfig.model_validate(
        {
            "fleet_id": fleet_id,
            "label": fleet_id,
            "demand": {
                "source": "generator",
                "structure": "location",
                "adapter": "demand.poisson_piecewise_constant@1",
                "generation_timing": "offline",
                "parameters": {
                    "intervals": ({"start_s": 0.0, "end_s": 100.0, "rate_tasks_per_s": 0.0},),
                    "location_weights_ref": "weights",
                },
            },
            "supply": {
                "source": "generated",
                "catalog_size": 1,
                "catalog_id_namespace": fleet_id,
                "availability": {"kind": "simultaneous", "start_s": 0.0, "end_s": 100.0},
                "initial_locations_ref": "initials",
                "capacity": capacity,
                "capacity_value": None if capacity_mode == "none" else 10.0,
                "area_definitions_ref": "areas" if areas else None,
                "area_assignments_ref": "assignments" if areas else None,
                "idle_policy": {"policy": "stationary"},
            },
            "dispatch": dispatch,
            "routing": {"profile_id": "profile"},
        }
    )


def _vehicle(
    fleet_id: str,
    vehicle_id: str,
    *,
    location: str = "L0",
    end_s: float = 100.0,
    capacity_mode: str = "none",
    capacity: float | None = None,
    areas: tuple[str, ...] = (),
    depot: str | None = None,
) -> VehicleSpec:
    return VehicleSpec(
        key=VehicleKey(fleet_id=fleet_id, vehicle_id=vehicle_id),
        availability_start_s=0.0,
        availability_end_s=end_s,
        initial_location_id=location,
        depot_location_id=depot,
        capacity_mode=capacity_mode,
        capacity=capacity,
        quantity_unit="units" if capacity_mode != "none" else None,
        assigned_area_ids=areas,
        identity_provenance="uploaded",
    )


def _state(spec: VehicleSpec, *, remaining: float | None = None) -> VehicleState:
    return VehicleState(
        key=spec.key,
        status=VehicleStatus.IDLE,
        current_execution_id=None,
        committed_location_id=spec.initial_location_id,
        available_at_s=0.0,
        remaining_capacity=spec.capacity if remaining is None else remaining,
        execution_generation=0,
    )


def _availability(spec: VehicleSpec, replication_id: str = "rep") -> VehicleAvailability:
    return VehicleAvailability(
        replication_id=replication_id,
        vehicle=spec.key,
        active=True,
        availability_start_s=0.0,
        availability_end_s=spec.availability_end_s,
        initial_location_id=spec.initial_location_id,
    )


def _clock(end_s: float) -> ClockConfig:
    return ClockConfig(
        origin_utc=datetime(2026, 1, 1, tzinfo=timezone.utc),
        display_timezone="UTC",
        simulation_start_s=0.0,
        observation_start_s=0.0,
        end_s=end_s,
    )


def _task(
    fleet_id: str,
    task_id: str,
    locations: tuple[str, ...],
    *,
    release_s: float = 0.0,
    scheduled: tuple[float | None, ...] | None = None,
    services: tuple[float, ...] | None = None,
    deltas: tuple[float | None, ...] | None = None,
    required: float | None = None,
    kind: str = "service",
) -> Task:
    scheduled = scheduled or (None,) * len(locations)
    services = services or (0.0,) * len(locations)
    deltas = deltas or (None,) * len(locations)
    return Task(
        task_id=task_id,
        fleet_id=fleet_id,
        release_s=release_s,
        steps=tuple(
            TaskStep(
                step_index=index,
                location_id=location,
                scheduled_time_s=target,
                service_duration_s=service,
                quantity_delta=delta,
            )
            for index, (location, target, service, delta) in enumerate(
                zip(locations, scheduled, services, deltas, strict=True), start=1
            )
        ),
        required_capacity=required,
        kind=kind,
        source_policy="gtfs_prescribed_deadhead@1" if kind != "service" else None,
    )


def _locations(count: int = 5) -> dict[str, LocationRef]:
    return {f"L{index}": _location(f"L{index}") for index in range(count)}


def test_one_executor_serves_location_od_and_stop_sequence_with_schedule_semantics() -> None:
    locations = _locations()
    routing = SharedRoutingCache(
        MatrixRouting({("L0", "L1"): 2.0, ("L1", "L2"): 3.0, ("L2", "L3"): 4.0})
    )
    spec = _vehicle("fleet", "v")
    executor = GenericTaskExecutor(
        locations=locations,
        routing=routing,
        profile_ids={"fleet": "profile"},
        capacity_modes={("fleet", "v"): "none"},
    )
    state = _state(spec)
    shapes = (
        _task("fleet", "location", ("L1",), services=(1.0,)),
        _task("fleet", "od", ("L1", "L2"), services=(1.0, 2.0)),
        _task(
            "fleet",
            "stops",
            ("L1", "L2", "L3"),
            scheduled=(5.0, 6.0, 7.0),
            services=(1.0, 1.0, 1.0),
        ),
    )
    plans = [
        executor.plan(state, task, assigned_at_s=0.0, execution_token=index)
        for index, task in enumerate(shapes, start=1)
    ]
    assert [len(plan.task_milestones) for plan in plans] == [1, 2, 3]
    stops = plans[-1]
    assert [row.arrival_s for row in stops.task_milestones] == [2.0, 9.0, 14.0]
    assert [row.service_start_s for row in stops.task_milestones] == [5.0, 9.0, 14.0]
    assert [(row.kind, row.start_s, row.end_s) for row in stops.intervals] == [
        ("movement", 0.0, 2.0),
        ("wait", 2.0, 5.0),
        ("service", 5.0, 6.0),
        ("movement", 6.0, 9.0),
        ("service", 9.0, 10.0),
        ("movement", 10.0, 14.0),
        ("service", 14.0, 15.0),
    ]


def test_nominal_timing_and_runtime_plan_are_identical_and_share_route_queries() -> None:
    locations = _locations(3)
    raw = MatrixRouting({("L0", "L1"): 2.0, ("L1", "L2"): 3.0})
    cache = SharedRoutingCache(raw)
    spec = _vehicle("fleet", "v")
    task = _task("fleet", "scheduled", ("L1", "L2"), scheduled=(5.0, 12.0), services=(1.0, 2.0))
    nominal = estimate_task_timing(
        task,
        start_location_id="L0",
        start_s=0.0,
        locations=locations,
        routing=cache,
        profile_id="profile",
    )
    executor = GenericTaskExecutor(
        locations=locations,
        routing=cache,
        profile_ids={"fleet": "profile"},
        capacity_modes={("fleet", "v"): "none"},
    )
    runtime = executor.plan(_state(spec), task, assigned_at_s=0.0, execution_token=1)
    assert runtime.end_s == nominal.end_s
    assert [row.model_dump() for row in runtime.task_milestones] == [
        {
            "step_index": row.step_index,
            "arrival_s": row.arrival_s,
            "service_start_s": row.service_start_s,
            "departure_s": row.departure_s,
        }
        for row in nominal.steps
    ]
    assert len(raw.calls) == 2


def test_prevalidation_rejects_internal_routes_and_invalid_predefined_ownership() -> None:
    locations = _locations(3)
    fleet = _fleet("fleet")
    task = _task("fleet", "bad", ("L1", "L2"))
    spec = _vehicle("fleet", "v")
    with pytest.raises(ScenarioDispatchError, match="unreachable internal route"):
        SimulationDecisionAdapter(
            fleets=(fleet,),
            tasks=(task,),
            vehicle_specs=(spec,),
            locations=locations,
            routing=MatrixRouting({("L0", "L1"): 1.0}),
        )

    predefined = _fleet("fleet", policy="predefined", plan_ref="plan")
    with pytest.raises(ScenarioDispatchError, match="must own every task"):
        SimulationDecisionAdapter(
            fleets=(predefined,),
            tasks=(_task("fleet", "orphan", ("L1",)),),
            vehicle_specs=(spec,),
            locations=locations,
            routing=MatrixRouting({("L0", "L1"): 1.0}),
            assignment_plans={"plan": AssignmentPlan(assignment_plan_id="plan", rows=())},
        )


def test_area_uses_entry_membership_not_depot_and_capacity_prefixes_are_enforced() -> None:
    locations = _locations(4)
    area_fleet = _fleet("area", areas=True)
    area_vehicle = _vehicle("area", "v", areas=("north",), depot="L3")
    area_task = _task("area", "inside", ("L1",))
    adapter = SimulationDecisionAdapter(
        fleets=(area_fleet,),
        tasks=(area_task,),
        vehicle_specs=(area_vehicle,),
        locations=locations,
        routing=MatrixRouting({("L0", "L1"): 1.0}),
        location_area_ids={"L1": ("north",), "L3": ("south",)},
    )
    assert adapter.eligibility.pair(_state(area_vehicle), area_task, time_s=0.0) is not None

    occupancy_fleet = _fleet("occ", capacity_mode="occupancy")
    occupancy_vehicle = _vehicle("occ", "v", capacity_mode="occupancy", capacity=10.0)
    feasible = _task("occ", "feasible", ("L1", "L2"), deltas=(-8.0, 8.0))
    adapter = SimulationDecisionAdapter(
        fleets=(occupancy_fleet,),
        tasks=(feasible,),
        vehicle_specs=(occupancy_vehicle,),
        locations=locations,
        routing=MatrixRouting({("L0", "L1"): 1.0, ("L1", "L2"): 1.0}),
    )
    plan = adapter.service_assignments(
        DecisionSnapshot(0.0, (_state(occupancy_vehicle),), (feasible,))
    )[0].plan
    assert [row.quantity_delta for row in plan.capacity_milestones] == [-8.0, 8.0]

    impossible = _task("occ", "impossible", ("L1",), deltas=(-11.0,))
    with pytest.raises(ScenarioDispatchError, match="full-capacity bounds"):
        SimulationDecisionAdapter(
            fleets=(occupancy_fleet,),
            tasks=(impossible,),
            vehicle_specs=(occupancy_vehicle,),
            locations=locations,
            routing=MatrixRouting({("L0", "L1"): 1.0}),
        )

    consumable_fleet = _fleet("stock", capacity_mode="consumable")
    consumable_vehicle = _vehicle("stock", "v", capacity_mode="consumable", capacity=10.0)
    stock_task = _task("stock", "needs_six", ("L1", "L2"), deltas=(None, -6.0), required=6.0)
    stock_adapter = SimulationDecisionAdapter(
        fleets=(consumable_fleet,),
        tasks=(stock_task,),
        vehicle_specs=(consumable_vehicle,),
        locations=locations,
        routing=MatrixRouting({("L0", "L1"): 1.0, ("L1", "L2"): 1.0}),
    )
    assert (
        stock_adapter.eligibility.pair(
            _state(consumable_vehicle, remaining=5.0), stock_task, time_s=0.0
        )
        is None
    )
    stock_plan = stock_adapter.service_assignments(
        DecisionSnapshot(0.0, (_state(consumable_vehicle),), (stock_task,))
    )[0].plan
    assert [(row.step_index, row.quantity_delta) for row in stock_plan.capacity_milestones] == [
        (2, -6.0)
    ]


def test_nearest_matching_pair_order_disjointness_cache_and_memory_guard() -> None:
    locations = _locations(3)
    fleet = _fleet("fleet")
    vehicles = (_vehicle("fleet", "a"), _vehicle("fleet", "b"))
    tasks = (_task("fleet", "a", ("L1",)), _task("fleet", "b", ("L1",)))
    raw = MatrixRouting({("L0", "L1"): 1.0})
    adapter = SimulationDecisionAdapter(
        fleets=(fleet,),
        tasks=tasks,
        vehicle_specs=vehicles,
        locations=locations,
        routing=raw,
    )
    assigned = adapter.service_assignments(
        DecisionSnapshot(
            0.0, tuple(_state(spec) for spec in reversed(vehicles)), tuple(reversed(tasks))
        )
    )
    assert [(row.plan.vehicle.vehicle_id, row.task.task_id) for row in assigned] == [
        ("a", "a"),
        ("b", "b"),
    ]
    assert len({row.plan.vehicle for row in assigned}) == 2
    assert len({row.task.task_id for row in assigned}) == 2
    assert raw.calls.count(("profile", "L0", "L1")) == 1
    assert raw.bulk_calls == [("profile", "L0", ("L1",))]

    capped_fleet = _fleet("fleet", max_pickup=0.5)
    capped = SimulationDecisionAdapter(
        fleets=(capped_fleet,),
        tasks=tasks,
        vehicle_specs=vehicles,
        locations=locations,
        routing=MatrixRouting({("L0", "L1"): 1.0}),
    )
    assert (
        capped.service_assignments(
            DecisionSnapshot(0.0, tuple(_state(spec) for spec in vehicles), tasks)
        )
        == ()
    )

    guarded = SimulationDecisionAdapter(
        fleets=(fleet,),
        tasks=tasks,
        vehicle_specs=vehicles,
        locations=locations,
        routing=MatrixRouting({("L0", "L1"): 1.0}),
        max_feasible_pairs=3,
    )
    with pytest.raises(MatchingPairLimitExceeded, match="feasible pair limit exceeded"):
        guarded.service_assignments(
            DecisionSnapshot(0.0, tuple(_state(spec) for spec in vehicles), tasks)
        )


def test_predefined_waits_for_next_unreleased_task_and_executes_prescribed_reposition() -> None:
    locations = _locations(3)
    fleet = _fleet("fixed", policy="predefined", plan_ref="plan")
    spec = _vehicle("fixed", "v", end_s=20.0)
    first = _task("fixed", "first", ("L1",), release_s=5.0)
    second = _task("fixed", "second", ("L2",), release_s=0.0, kind="reposition")
    plan = AssignmentPlan(
        assignment_plan_id="plan",
        rows=(
            AssignmentRow(vehicle=spec.key, order_index=0, task_id="first"),
            AssignmentRow(vehicle=spec.key, order_index=1, task_id="second"),
        ),
    )
    adapter = SimulationDecisionAdapter(
        fleets=(fleet,),
        tasks=(second, first),
        vehicle_specs=(spec,),
        locations=locations,
        routing=MatrixRouting({("L0", "L1"): 1.0, ("L1", "L2"): 1.0}),
        assignment_plans={"plan": plan},
    )
    result = run_event_kernel(
        replication_id="rep",
        clock=_clock(20.0),
        tasks=(second, first),
        vehicle_specs=(spec,),
        availability=(_availability(spec),),
        decisions=adapter,
    )
    outcomes = {row.task_id: row for row in result.task_outcomes}
    assert outcomes["first"].assigned_at_s == 5.0
    assert outcomes["second"].assigned_at_s == 6.0
    assert all(row.status == TaskLifecycleStatus.COMPLETED for row in outcomes.values())
    second_execution = next(row for row in result.execution_outcomes if row.task_id == "second")
    assert second_execution.realized_intervals[0].movement_kind == "reposition"


@pytest.mark.parametrize(
    ("service_s", "expected_kind", "expected_fraction", "expected_location"),
    [(0.0, "movement", 0.4, None), (10.0, "service", None, "L0")],
)
def test_hard_exit_materializes_mid_edge_and_mid_service_without_future_location(
    service_s: float,
    expected_kind: str,
    expected_fraction: float | None,
    expected_location: str | None,
) -> None:
    locations = _locations(2)
    fleet = _fleet("fleet")
    start_location = "L0"
    target = "L1" if service_s == 0.0 else "L0"
    spec = _vehicle("fleet", "v", location=start_location, end_s=4.0)
    task = _task("fleet", "long", (target,), services=(service_s,))
    routing = MatrixRouting({("L0", "L1"): 10.0})
    adapter = SimulationDecisionAdapter(
        fleets=(fleet,),
        tasks=(task,),
        vehicle_specs=(spec,),
        locations=locations,
        routing=routing,
    )
    result = run_event_kernel(
        replication_id="rep",
        clock=_clock(20.0),
        tasks=(task,),
        vehicle_specs=(spec,),
        availability=(_availability(spec),),
        decisions=adapter,
    )
    execution = result.execution_outcomes[0]
    assert execution.status == ExecutionLifecycleStatus.INTERRUPTED_SHIFT
    assert execution.realized_intervals[-1].kind == expected_kind
    assert execution.realized_intervals[-1].end_s == 4.0
    assert execution.realized_position is not None
    assert execution.realized_position.edge_fraction == expected_fraction
    assert execution.realized_position.location_id == expected_location
    state = result.vehicle_outcomes[0].state
    assert state.committed_location_id == start_location


def test_tiny_mixed_operation_handoff_matches_recorded_fixture() -> None:
    locations = _locations(3)
    nearest = _fleet("nearest")
    fixed = _fleet("fixed", policy="predefined", plan_ref="fixed_plan")
    nearest_vehicle = _vehicle("nearest", "n")
    fixed_vehicle = _vehicle("fixed", "p")
    nearest_task = _task("nearest", "location", ("L1",), services=(1.0,))
    fixed_task = _task("fixed", "stops", ("L1", "L2"), scheduled=(3.0, None), services=(1.0, 1.0))
    assignment = AssignmentPlan(
        assignment_plan_id="fixed_plan",
        rows=(AssignmentRow(vehicle=fixed_vehicle.key, order_index=0, task_id="stops"),),
    )
    adapter = SimulationDecisionAdapter(
        fleets=(fixed, nearest),
        tasks=(fixed_task, nearest_task),
        vehicle_specs=(fixed_vehicle, nearest_vehicle),
        locations=locations,
        routing=MatrixRouting({("L0", "L1"): 2.0, ("L1", "L2"): 3.0}),
        assignment_plans={"fixed_plan": assignment},
    )
    result = run_event_kernel(
        replication_id="rep",
        clock=_clock(20.0),
        tasks=(nearest_task, fixed_task),
        vehicle_specs=(nearest_vehicle, fixed_vehicle),
        availability=(_availability(nearest_vehicle), _availability(fixed_vehicle)),
        decisions=adapter,
    )
    projection = {
        "tasks": [
            {
                "fleet_id": row.fleet_id,
                "task_id": row.task_id,
                "status": row.status,
                "assigned_at_s": row.assigned_at_s,
                "terminal_time_s": row.terminal_time_s,
                "vehicle_id": row.vehicle_id,
            }
            for row in result.task_outcomes
        ],
        "executions": [
            {
                "fleet_id": row.fleet_id,
                "vehicle_id": row.vehicle.vehicle_id,
                "task_id": row.task_id,
                "status": row.status,
                "planned_end_s": row.planned_end_s,
                "realized_end_s": row.realized_end_s,
            }
            for row in result.execution_outcomes
        ],
        "activities": [
            {
                "fleet_id": row.fleet_id,
                "vehicle_id": row.vehicle.vehicle_id,
                "task_id": row.task_id,
                "interval_index": index,
                "kind": interval.kind,
                "start_s": interval.start_s,
                "end_s": interval.end_s,
                "edge_id": getattr(interval, "edge_id", None),
                "location_id": getattr(interval, "location_id", None),
            }
            for row in result.execution_outcomes
            for index, interval in enumerate(row.realized_intervals)
        ],
    }
    assert projection == json.loads(FIXTURE.read_text(encoding="utf-8"))
