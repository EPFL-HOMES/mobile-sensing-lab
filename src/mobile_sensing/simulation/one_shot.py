"""Bounded deterministic single-depot VRP plans for the common task executor."""

import math
import time
from dataclasses import dataclass
from decimal import Decimal

import numpy as np

from mobile_sensing.contracts import (
    AssignmentPlan,
    AssignmentRow,
    Task,
    TaskStep,
    stable_id,
)

ONE_SHOT_PLANNER_VERSION = "ortools-one-shot@3"


class PlanningTimeout(RuntimeError):
    pass


class PlanningFailure(ValueError):
    pass


@dataclass(frozen=True)
class OneShotPlan:
    tasks: tuple[Task, ...]
    assignment: AssignmentPlan
    report: dict


def assignment_alias(fleet_id):
    return stable_id("one_shot_plan", {"fleet": fleet_id})


def plan_one_shot(
    fleet,
    tasks,
    specs,
    availability,
    locations,
    routing,
    replication_id,
    *,
    cancellation=None,
    progress=None,
    end_s=None,
    location_area_ids=None,
):
    try:
        import ortools
        from ortools.constraint_solver import pywrapcp, routing_enums_pb2
    except ImportError as exc:
        raise PlanningFailure("Install the optimization extra to enable One-shot") from exc
    if fleet.demand.generation_timing != "offline" or fleet.demand.task_type != "location":
        raise PlanningFailure("One-shot requires offline location tasks")
    if fleet.supply.capacity_mode == "occupancy" or any(
        len(task.steps) != 1 or task.kind != "service" for task in tasks
    ):
        raise PlanningFailure(
            "One-shot supports delivery/location tasks without pickup-delivery pairs"
        )
    if any(task.steps[0].scheduled_time_s is not None for task in tasks):
        raise PlanningFailure("One-shot does not support hard customer visit windows")
    ordered_specs = tuple(sorted(specs, key=lambda spec: spec.key.vehicle_id))
    by_vehicle = {row.vehicle: row for row in availability}
    if not ordered_specs or any(not by_vehicle[spec.key].active for spec in ordered_specs):
        raise PlanningFailure("One-shot requires a fixed active fleet for the replication")
    depots = {spec.depot_location_id for spec in ordered_specs}
    if len(depots) != 1 or None in depots:
        raise PlanningFailure("One-shot requires exactly one shared explicit depot")
    depot = next(iter(depots))
    if any(by_vehicle[spec.key].initial_location_id != depot for spec in ordered_specs):
        raise PlanningFailure("One-shot vehicles must start at the depot")
    ordered_tasks = tuple(
        sorted(
            tasks,
            key=lambda task: (
                locations[task.steps[0].location_id].node_id,
                task.steps[0].location_id,
                task.task_id,
            ),
        )
    )
    count = len(ordered_tasks)
    reload_enabled = (
        fleet.dispatch.allow_replenishment and fleet.supply.capacity_mode == "consumable"
    )
    reload_owners = (
        [
            v
            for v in range(len(ordered_specs))
            for _ in range(min(count, fleet.dispatch.max_replenishments_per_vehicle))
        ]
        if reload_enabled and count
        else []
    )
    pair_count = (count + 1 + len(reload_owners)) ** 2
    if pair_count > fleet.dispatch.max_cost_pairs:
        raise PlanningFailure(
            f"One-shot needs {pair_count} task/depot cost entries, above the configured limit {fleet.dispatch.max_cost_pairs}"
        )
    if count == 0:
        return OneShotPlan(
            (),
            AssignmentPlan(assignment_plan_id=assignment_alias(fleet.fleet_id), rows=()),
            {
                "status": "empty_plan",
                "objective_travel_seconds": 0.0,
                "cost_pairs": pair_count,
            },
        )
    base = min(by_vehicle[spec.key].availability_start_s for spec in ordered_specs)
    upper = max(by_vehicle[spec.key].availability_end_s for spec in ordered_specs)
    if end_s is not None:
        upper = min(upper, end_s)
    scale = 1_000_000
    horizon = int(math.floor((upper - base) * scale))
    if horizon <= 0 or horizon > 10**15:
        raise PlanningFailure("One-shot working windows exceed the supported integer time range")
    point_ids = (
        [depot]
        + [task.steps[0].location_id for task in ordered_tasks]
        + [depot] * len(reload_owners)
    )
    nodes = [locations[identifier].node_id for identifier in point_ids]
    node_targets = tuple(sorted(set(nodes)))
    costs = np.zeros((len(point_ids), len(point_ids)), dtype=np.int64)
    seconds = np.zeros_like(costs, dtype=float)
    unreachable = set()

    def source_costs(node):
        if cancellation:
            cancellation.raise_if_cancelled()
        cost_operation = getattr(routing, "travel_times_from", None)
        operation = getattr(routing, "routes_from", None)
        routes = (
            {}
            if cost_operation
            else (
                operation(fleet.routing_profile, node, node_targets)
                if operation
                else {
                    target: routing.route(fleet.routing_profile, node, target)
                    for target in node_targets
                }
            )
        )
        durations = (
            cost_operation(fleet.routing_profile, node, node_targets)
            if cost_operation
            else {
                target: (route.total_duration_s if route.reachable else None)
                for target, route in routes.items()
            }
        )
        return node, durations

    from mobile_sensing.environment.cost_cache import bounded_source_costs

    for completed, (node, durations) in enumerate(
        bounded_source_costs(source_costs, node_targets, getattr(routing, "cost_workers", 1)),
        1,
    ):
        for i in [index for index, value in enumerate(nodes) if value == node]:
            for j, target in enumerate(nodes):
                duration = durations[target]
                if duration is None:
                    unreachable.add((i, j))
                    costs[i, j] = horizon + 1
                else:
                    seconds[i, j] = duration
                    costs[i, j] = math.ceil(duration * scale)
        if progress:
            progress.update(
                phase=f"planning.costs.{fleet.fleet_id}",
                completed=completed,
                total=len(node_targets),
            )
    services = [0] + [math.ceil(task.steps[0].service_duration_s * scale) for task in ordered_tasks]
    quantities = [Decimal(str(task.required_capacity or 0)) for task in ordered_tasks]
    capacities = [Decimal(str(spec.capacity or 0)) for spec in ordered_specs]
    precision = max([0] + [-value.as_tuple().exponent for value in quantities + capacities])
    if precision > 6:
        raise PlanningFailure("One-shot capacity quantities support at most six decimal places")
    quantity_scale = 10**precision
    demands = [0] + [int(value * quantity_scale) for value in quantities]
    integer_capacities = [int(value * quantity_scale) for value in capacities]
    if (
        fleet.supply.capacity_mode == "consumable"
        and not reload_enabled
        and sum(demands) > sum(integer_capacities)
    ):
        raise PlanningFailure(
            "Infeasible capacity: mandatory delivery quantity exceeds total vehicle capacity"
        )
    reload_nodes = list(range(count + 1, len(point_ids)))
    refill_s = fleet.supply.depot_min_stay_minutes * 60
    services += [math.ceil(refill_s * scale)] * len(reload_owners)
    demands += [-integer_capacities[v] for v in reload_owners]
    if fleet.supply.capacity_mode == "consumable":
        if any(q > max(integer_capacities) for q in demands[1 : count + 1]):
            raise PlanningFailure("A mandatory task exceeds every vehicle's full capacity")
        capacity_bound = sum(integer_capacities) * (
            1 + (fleet.dispatch.max_replenishments_per_vehicle if reload_enabled else 0)
        )
        if sum(demands[1 : count + 1]) > capacity_bound:
            raise PlanningFailure(
                "Mandatory quantity exceeds capacity within the configured replenishment-visit limit"
            )
    manager = pywrapcp.RoutingIndexManager(len(point_ids), len(ordered_specs), 0)
    model = pywrapcp.RoutingModel(manager)
    if fleet.supply.service_area_mode != "none" or fleet.supply.service_area_input is not None:
        for rank, task in enumerate(ordered_tasks, start=1):
            membership = set((location_area_ids or {}).get(task.steps[0].location_id, ()))
            allowed = [
                i
                for i, spec in enumerate(ordered_specs)
                if membership.intersection(spec.assigned_area_ids)
            ]
            if not allowed:
                raise PlanningFailure(
                    f"No vehicle is assigned to the service area of mandatory task {task.task_id}"
                )
            model.SetAllowedVehiclesForIndex(allowed, manager.NodeToIndex(rank))
    # The bounded matrix already exists. Native evaluators avoid a Python call
    # for every search arc, while retaining the same integer costs and ordering.
    travel_index = model.RegisterTransitMatrix(costs.tolist())
    model.SetArcCostEvaluatorOfAllVehicles(travel_index)
    transit_index = model.RegisterTransitMatrix((costs + np.asarray(services)[:, None]).tolist())
    model.AddDimension(transit_index, horizon, horizon, False, "Time")
    dimension = model.GetDimensionOrDie("Time")
    for vehicle, spec in enumerate(ordered_specs):
        available = by_vehicle[spec.key]
        start = math.ceil((min(available.availability_start_s, upper) - base) * scale)
        end = math.floor((min(available.availability_end_s, upper) - base) * scale)
        dimension.CumulVar(model.Start(vehicle)).SetRange(start, start)
        dimension.CumulVar(model.End(vehicle)).SetRange(start, end)
    if fleet.supply.capacity_mode == "consumable":
        demand_index = model.RegisterUnaryTransitVector(demands)
        model.AddDimensionWithVehicleCapacity(
            demand_index,
            max(integer_capacities) if reload_enabled else 0,
            integer_capacities,
            True,
            "Capacity",
        )
        capacity_dimension = model.GetDimensionOrDie("Capacity")
        for index in range(model.Size()):
            if manager.IndexToNode(index) not in reload_nodes:
                capacity_dimension.SlackVar(index).SetValue(0)
    solver = model.solver()
    for node, owner in zip(reload_nodes, reload_owners):
        index = manager.NodeToIndex(node)
        model.AddDisjunction([index], 0)
        model.SetAllowedVehiclesForIndex([owner], index)
        capacity = integer_capacities[owner]
        capacity_dimension.SlackVar(index).SetRange(0, capacity)
        solver.Add(
            capacity_dimension.CumulVar(index) + capacity_dimension.SlackVar(index) == capacity
        )
        for vehicle in range(len(ordered_specs)):
            model.NextVar(model.Start(vehicle)).RemoveValue(index)
            model.NextVar(index).RemoveValue(model.End(vehicle))
        for other in reload_nodes:
            if other != node:
                model.NextVar(index).RemoveValue(manager.NodeToIndex(other))
        # Refill only when the next service cannot fit the remaining stock.
        next_demands = [
            max(0, demands[manager.IndexToNode(i)])
            for i in range(model.Size() + len(ordered_specs))
        ]
        solver.Add(
            capacity_dimension.CumulVar(index) + solver.Element(next_demands, model.NextVar(index))
            >= capacity + 1 - (2 * max(integer_capacities) + 1) * (1 - model.ActiveVar(index))
        )
    # Departure to a task cannot precede its release: the executor waits at the
    # current location before assigning it. No hard customer visit windows exist.
    for target, task in enumerate(ordered_tasks, start=1):
        release = math.ceil((task.release_s - base) * scale)
        if release <= 0:
            continue
        if release > horizon:
            raise PlanningFailure("A mandatory task is released after all vehicle working windows")
        target_index = manager.NodeToIndex(target)
        for source in range(model.Size()):
            if source == target_index:
                continue
            active = solver.IsEqualCstVar(model.NextVar(source), target_index)
            departure = (
                dimension.CumulVar(source)
                + services[manager.IndexToNode(source)]
                + dimension.SlackVar(source)
            )
            solver.Add(departure >= release - horizon * (1 - active))
    for source, target in unreachable:
        if source == target:
            continue
        sources = (
            [model.Start(v) for v in range(len(ordered_specs))]
            if source == 0
            else [manager.NodeToIndex(source)]
        )
        targets = (
            [model.End(v) for v in range(len(ordered_specs))]
            if target == 0
            else [manager.NodeToIndex(target)]
        )
        for i in sources:
            for j in targets:
                model.NextVar(i).RemoveValue(j)
    parameters = pywrapcp.DefaultRoutingSearchParameters()
    parameters.first_solution_strategy = (
        routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION
    )
    parameters.local_search_metaheuristic = (
        routing_enums_pb2.LocalSearchMetaheuristic.GREEDY_DESCENT
    )
    parameters.solution_limit = fleet.dispatch.solution_limit
    parameters.use_full_propagation = True
    parameters.sat_parameters.num_search_workers = 1
    parameters.time_limit.FromMilliseconds(
        math.ceil(fleet.dispatch.planning_timeout_seconds * 1000)
    )
    stopped = {"cancelled": False}

    def checkpoint():
        if cancellation and cancellation.is_cancelled():
            stopped["cancelled"] = True
            model.solver().FinishCurrentSearch()

    model.AddAtSolutionCallback(checkpoint)
    if progress:
        progress.update(phase=f"planning.search.{fleet.fleet_id}", completed=0, total=1)
    started = time.perf_counter()
    seed_assignment = None
    if reload_enabled:
        from mobile_sensing.simulation.reload_seed import build_reload_seed

        seed_routes = build_reload_seed(
            tasks=ordered_tasks,
            specs=ordered_specs,
            availability=by_vehicle,
            locations=locations,
            fleet=fleet,
            travel=costs,
            services=services,
            demands=demands,
            capacities=integer_capacities,
            reload_nodes=reload_nodes,
            reload_owners=reload_owners,
            upper=upper,
            base=base,
            scale=scale,
            location_area_ids=location_area_ids,
            cancellation=cancellation,
        )
        if seed_routes is not None:
            seed_assignment = model.ReadAssignmentFromRoutes(seed_routes, True)
    solution = (
        model.SolveFromAssignmentWithParameters(seed_assignment, parameters)
        if seed_assignment is not None
        else model.SolveWithParameters(parameters)
    )
    elapsed = time.perf_counter() - started
    status = model.status()
    if cancellation:
        cancellation.raise_if_cancelled()
    if stopped["cancelled"]:
        raise RuntimeError("One-shot planning cancelled")
    if status == 4 or elapsed >= fleet.dispatch.planning_timeout_seconds * 0.999:
        raise PlanningTimeout(
            "One-shot search reached its wall-time guard; no timing-dependent incumbent was accepted"
        )
    if solution is None:
        raise PlanningFailure(
            "One-shot model is infeasible"
            if status == 6
            else f"One-shot search found no feasible mandatory-task plan (status {status})"
        )
    rows, returns, route_reports, served = [], [], [], []
    objective = 0.0
    for vehicle, spec in enumerate(ordered_specs):
        index = model.Start(vehicle)
        sequence = []
        while not model.IsEnd(index):
            next_index = solution.Value(model.NextVar(index))
            source, target = manager.IndexToNode(index), manager.IndexToNode(next_index)
            objective += float(seconds[source, target])
            if 0 < target <= count:
                sequence.append(ordered_tasks[target - 1])
                served.append(ordered_tasks[target - 1].task_id)
            elif target > count:
                sequence.append(
                    Task(
                        task_id=stable_id(
                            "one_shot_reload",
                            {
                                "replication": replication_id,
                                "vehicle": spec.key,
                                "node": target,
                            },
                        ),
                        fleet_id=fleet.fleet_id,
                        release_s=base,
                        kind="depot_return",
                        source_policy="dispatch.one_shot.reload@1",
                        capacity_reset_at_end=True,
                        steps=(
                            TaskStep(
                                step_index=1,
                                location_id=depot,
                                service_duration_s=refill_s,
                            ),
                        ),
                    )
                )
            index = next_index
        if sequence:
            final = Task(
                task_id=stable_id(
                    "one_shot_return",
                    {"replication": replication_id, "vehicle": spec.key},
                ),
                fleet_id=fleet.fleet_id,
                release_s=base,
                kind="depot_return",
                source_policy="dispatch.one_shot.return@1",
                capacity_reset_at_end=False,
                steps=(TaskStep(step_index=1, location_id=depot),),
            )
            returns.extend([task for task in sequence if task.kind == "depot_return"])
            returns.append(final)
            for rank, task in enumerate([*sequence, final]):
                rows.append(AssignmentRow(vehicle=spec.key, order_index=rank, task_id=task.task_id))
        route_reports.append(
            {
                "vehicle_id": spec.key.vehicle_id,
                "task_ids": [task.task_id for task in sequence if task.kind == "service"],
                "reload_count": sum(bool(task.capacity_reset_at_end) for task in sequence),
                "planned_end_s": base
                + solution.Value(dimension.CumulVar(model.End(vehicle))) / scale,
            }
        )
    if sorted(served) != sorted(task.task_id for task in ordered_tasks):
        raise AssertionError("One-shot plan does not own every task exactly once")
    return OneShotPlan(
        tuple([*ordered_tasks, *returns]),
        AssignmentPlan(assignment_plan_id=assignment_alias(fleet.fleet_id), rows=tuple(rows)),
        {
            "planner_version": ONE_SHOT_PLANNER_VERSION,
            "replenishment_enabled": reload_enabled,
            "feasible_seed_used": seed_assignment is not None,
            "max_replenishments_per_vehicle": (
                fleet.dispatch.max_replenishments_per_vehicle if reload_enabled else 0
            ),
            "depot_min_stay_seconds": refill_s,
            "reload_count": sum(bool(task.capacity_reset_at_end) for task in returns),
            "same_location_preference": "Stable location ordering and zero-cost consecutive same-node transitions; tasks retain independent capacity/service milestones",
            "status": status,
            "objective_travel_seconds": objective,
            "integer_objective": solution.ObjectiveValue(),
            "time_scale_per_second": scale,
            "capacity_scale": quantity_scale,
            "cost_pairs": pair_count,
            "search_seconds": elapsed,
            "ortools_version": ortools.__version__,
            "solution_limit": fleet.dispatch.solution_limit,
            "search": "PARALLEL_CHEAPEST_INSERTION + GREEDY_DESCENT; single worker; deterministic solution limit",
            "optimality": "heuristic; no global optimality guarantee",
            "routes": route_reports,
            "time_rounding": "Travel/service rounded up to microseconds; vehicle end rounded down; original times used by executor",
        },
    )
