"""Deterministic feasible starting routes for the bounded multi-trip VRP."""

import numpy as np


def build_reload_seed(
    *,
    tasks,
    specs,
    availability,
    locations,
    fleet,
    travel,
    services,
    demands,
    capacities,
    reload_nodes,
    reload_owners,
    upper,
    base,
    scale,
    location_area_ids,
    cancellation,
):
    count = len(tasks)
    remaining = np.ones(count, dtype=bool)
    routes = [[] for _ in specs]
    current = np.zeros(len(specs), dtype=int)
    starts = np.array([availability[s.key].availability_start_s for s in specs])
    now = starts.copy()
    ends = np.array([min(availability[s.key].availability_end_s, upper) for s in specs])
    stock = np.array(capacities, dtype=np.int64)
    q = np.array(demands[1 : count + 1], dtype=np.int64)
    releases = np.array([t.release_s for t in tasks])
    service = np.asarray(services[1 : count + 1]) / scale
    distance = travel / scale
    slots = [
        [node for node, owner in zip(reload_nodes, reload_owners) if owner == v]
        for v in range(len(specs))
    ]
    used = np.zeros(len(specs), dtype=int)
    closed = np.zeros(len(specs), dtype=bool)
    allowed = np.ones((len(specs), count), dtype=bool)
    if fleet.supply.service_area_mode != "none" or fleet.supply.service_area_input is not None:
        for v, spec in enumerate(specs):
            allowed[v] = [
                bool(
                    set(spec.assigned_area_ids).intersection(
                        (location_area_ids or {}).get(t.steps[0].location_id, ())
                    )
                )
                for t in tasks
            ]
    for iteration in range(count + len(specs)):
        if cancellation and iteration % 32 == 0:
            cancellation.raise_if_cancelled()
        if not remaining.any():
            return routes
        candidates = [v for v in range(len(specs)) if not closed[v]]
        if not candidates:
            return None
        v = min(candidates, key=lambda i: ((now[i] - starts[i]) / max(1, ends[i] - starts[i]), i))
        feasible = remaining & allowed[v] & (q <= stock[v])
        refill = False
        departure = now[v]
        origin = current[v]
        if not feasible.any() and used[v] < len(slots[v]):
            refill = True
            feasible = remaining & allowed[v] & (q <= capacities[v]) & (q > stock[v])
            departure += distance[origin, 0] + fleet.supply.depot_min_stay_minutes * 60
            origin = 0
        finish = np.maximum(departure, releases) + distance[origin, 1 : count + 1] + service
        feasible &= finish + distance[1 : count + 1, 0] <= ends[v] + 1e-9
        if not feasible.any():
            closed[v] = True
            continue
        indices = np.flatnonzero(feasible)
        # Stable location-sorted tasks make same-node deliveries consecutive.
        i = min(indices, key=lambda rank: (distance[origin, rank + 1], finish[rank], rank))
        if refill:
            routes[v].append(slots[v][used[v]])
            used[v] += 1
            stock[v] = capacities[v]
        routes[v].append(int(i + 1))
        stock[v] -= q[i]
        current[v] = i + 1
        now[v] = finish[i]
        remaining[i] = False
    return routes if not remaining.any() else None
