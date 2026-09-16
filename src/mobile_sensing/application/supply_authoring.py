"""Freeze physical vehicle catalogs before realizing replication availability."""

import numpy as np
from shapely.geometry import Point
from mobile_sensing.application.civil_time import input_seconds
from mobile_sensing.application.temporal_authoring import group_windows
from mobile_sensing.application.environment_editor import read_input
from mobile_sensing.application.task_authoring import table_input
from mobile_sensing.contracts import VehicleSpec, VehicleKey, VehicleAvailability, stable_id
from mobile_sensing.datasets.parsing import LocationResolver, parse_float
from mobile_sensing.application.service_areas import automatic_service_areas


def resolve_depot(fleet, support):
    supply = fleet.supply
    if supply.depot_cell_id:
        return support.cell(supply.depot_cell_id)
    resolver = LocationResolver(
        environment=support.environment.reference, snapper=support.environment.snapping
    )
    if supply.depot_longitude is not None:
        location = resolver.by_coordinates(
            location_id=stable_id(
                "depot",
                {
                    "fleet": fleet.fleet_id,
                    "longitude": supply.depot_longitude,
                    "latitude": supply.depot_latitude,
                },
            ),
            x=supply.depot_longitude,
            y=supply.depot_latitude,
            source_crs="EPSG:4326",
        )
    elif supply.synthetic_depot:
        locations, probabilities = support.weighted_mix(
            supply.spatial_weights or ((supply.spatial_feature, 1.0),)
        )
        x = float(np.dot(probabilities, [support.locations[item].snapped_x for item in locations]))
        y = float(np.dot(probabilities, [support.locations[item].snapped_y for item in locations]))
        # The synthetic facility is a routable node nearest the weighted centre.
        nodes = support.environment.nodes.sort_values("node_id")
        nearest = ((nodes.x_m - x) ** 2 + (nodes.y_m - y) ** 2).idxmin()
        node = nodes.loc[nearest]
        location = resolver.by_coordinates(
            location_id=stable_id(
                "synthetic_depot", {"fleet": fleet.fleet_id, "node": str(node.node_id)}
            ),
            x=float(node.x_m),
            y=float(node.y_m),
            source_crs=support.environment.metadata.working_crs,
        )
    else:
        return None
    support.locations[location.location_id] = location
    return location.location_id


def build_catalog(root, fleet, support, clock, rng):
    supply = fleet.supply
    depot = resolve_depot(fleet, support)
    if supply.initial_location == "depot" and depot is None:
        raise ValueError("Select or create a depot before using depot initial locations")
    specs = []
    if supply.source == "generated":
        windows = [
            (start, latest, duration)
            for count, start, latest, duration in group_windows(supply, clock)
            for _ in range(count)
        ]
        if supply.initial_location == "input":
            raise ValueError("Input initial locations require an imported vehicle catalog")
        if supply.initial_location == "depot":
            initials = [depot] * supply.fleet_size
        else:
            ids, probabilities = support.weighted_mix(
                supply.spatial_weights or ((supply.spatial_feature, 1.0),)
            )
            choices = rng.stream("studio.catalog.initial_locations", fleet.fleet_id).choice(
                len(ids), supply.fleet_size, p=probabilities
            )
            initials = [ids[int(choice)] for choice in choices]
        for index, initial in enumerate(initials):
            start, latest, duration = windows[index]
            specs.append(
                VehicleSpec(
                    key=VehicleKey(fleet_id=fleet.fleet_id, vehicle_id=f"vehicle-{index+1:04d}"),
                    availability_start_s=start,
                    availability_end_s=latest + duration,
                    initial_location_id=initial,
                    capacity_mode=supply.capacity_mode,
                    capacity=None if supply.capacity_mode == "none" else supply.capacity,
                    quantity_unit=None if supply.capacity_mode == "none" else supply.capacity_unit,
                    depot_location_id=depot,
                    identity_provenance="generated",
                )
            )
    elif supply.source == "import":
        if not supply.input_id:
            raise ValueError("Select a vehicle catalog input")
        data, metadata = table_input(root, supply.input_id, {"supply"})
        resolver = LocationResolver(
            environment=support.environment.reference,
            known_locations=support.locations,
            snapper=support.environment.snapping,
        )
        for row_number, row in enumerate(data.to_dict("records"), start=2):

            def value(name, default=None):
                return row.get(supply.columns.get(name, name), default)

            try:
                vehicle_id = value("vehicle_id")
                if not isinstance(vehicle_id, str) or not vehicle_id:
                    raise ValueError("Vehicle IDs must be nonempty strings")
                if value("initial_cell_id"):
                    initial = support.cell(value("initial_cell_id"))
                elif value("initial_x") is not None:
                    if not metadata["source_crs"]:
                        raise ValueError("Confirm vehicle-coordinate CRS in the input preview")
                    resolved = resolver.by_coordinates(
                        location_id=stable_id(
                            "vehicle_initial", {"input": supply.input_id, "vehicle": vehicle_id}
                        ),
                        x=value("initial_x"),
                        y=value("initial_y"),
                        source_crs=metadata["source_crs"],
                    )
                    support.locations[resolved.location_id] = resolved
                    initial = resolved.location_id
                elif depot:
                    initial = depot
                else:
                    raise ValueError("Map initial_cell_id or initial_x/initial_y for each vehicle")
                start = input_seconds(value("start_time", supply.operating_start), "clock", clock)
                end = input_seconds(value("end_time", supply.operating_end), "clock", clock)
                specs.append(
                    VehicleSpec(
                        key=VehicleKey(fleet_id=fleet.fleet_id, vehicle_id=vehicle_id),
                        availability_start_s=start,
                        availability_end_s=end,
                        initial_location_id=initial,
                        depot_location_id=depot,
                        capacity_mode=supply.capacity_mode,
                        capacity=(
                            None
                            if supply.capacity_mode == "none"
                            else parse_float(value("capacity", supply.capacity), nonnegative=True)
                        ),
                        quantity_unit=(
                            None if supply.capacity_mode == "none" else supply.capacity_unit
                        ),
                        identity_provenance="uploaded",
                    )
                )
            except (ValueError, TypeError) as exc:
                raise ValueError(
                    f"Vehicle input {metadata['name']}, source row {row_number}: {exc}"
                ) from exc
    else:
        raise ValueError("Timetable catalogs are resolved by duty inference")
    if not specs or len({spec.key.vehicle_id for spec in specs}) != len(specs):
        raise ValueError("Physical catalog must contain unique vehicles")
    return assign_areas(root, fleet, specs, support)


def assign_areas(root, fleet, specs, support):
    supply = fleet.supply
    if supply.service_area_mode == "none":
        return tuple(specs), {}, None
    if supply.service_area_mode == "auto":
        allowed_locations = None
        if fleet.demand.location_condition == "depot_roundtrip":
            depots = {spec.depot_location_id for spec in specs}
            if None in depots or len(depots) != 1:
                raise ValueError("Auto service areas need one resolved depot for round-trip demand")
            depot = support.locations[next(iter(depots))]
            nodes = support.environment.routing.roundtrip_node_ids(depot.node_id)
            allowed_locations = frozenset(
                identifier
                for identifier, location in support.locations.items()
                if location.node_id in nodes
            )
        locations, probabilities = support.weighted_mix(
            fleet.demand.spatial_weights or ((fleet.demand.spatial_feature, 1.0),),
            allowed_locations,
        )
        probability_by_location = dict(zip(locations, probabilities, strict=True))
        candidates = allowed_locations or support.locations
        result = automatic_service_areas(
            fleet_id=fleet.fleet_id,
            area_count=supply.auto_service_area_count,
            location_rows=(
                (
                    identifier,
                    support.locations[identifier].snapped_x,
                    support.locations[identifier].snapped_y,
                    probability_by_location.get(identifier, 0.0),
                )
                for identifier in sorted(candidates)
            ),
            vehicle_ids=(spec.key.vehicle_id for spec in specs),
        )
        assigned = tuple(
            spec.model_copy(
                update={"assigned_area_ids": result.vehicle_area_ids[spec.key.vehicle_id]}
            )
            for spec in specs
        )
        return assigned, result.location_area_ids, result.report
    if supply.service_area_input is None or supply.area_assignment_input is None:
        raise ValueError("Uploaded service areas require geometry and assignments")
    areas, _ = read_input(root, supply.service_area_input, role={"service_area"})
    if "area_id" not in areas or areas.area_id.duplicated().any():
        raise ValueError("Service-area geometry requires unique area_id")
    areas = areas.to_crs(support.environment.metadata.working_crs)
    assignments, _ = table_input(root, supply.area_assignment_input, {"area_assignment"})
    if (
        not {"vehicle_id", "area_id"} <= set(assignments)
        or assignments.duplicated(["vehicle_id", "area_id"]).any()
    ):
        raise ValueError("Area assignments require unique vehicle_id/area_id rows")
    ids = {spec.key.vehicle_id for spec in specs}
    if set(assignments.vehicle_id) != ids or not set(assignments.area_id) <= set(areas.area_id):
        raise ValueError(
            "Explicit area assignments must cover the physical catalog and reference defined areas"
        )

    def scoped(value):
        return f"{supply.service_area_input}:{value}"

    by_vehicle = {
        vehicle: tuple(sorted(scoped(area) for area in rows.area_id.astype(str)))
        for vehicle, rows in assignments.groupby("vehicle_id")
    }
    specs = tuple(
        spec.model_copy(update={"assigned_area_ids": by_vehicle[spec.key.vehicle_id]})
        for spec in specs
    )
    membership = {}
    for identifier, location in support.locations.items():
        point = Point(location.snapped_x, location.snapped_y)
        membership[identifier] = tuple(
            sorted(
                scoped(area)
                for area in areas.iloc[
                    areas.sindex.query(point, predicate="intersects")
                ].area_id.astype(str)
            )
        )
    return (
        specs,
        membership,
        {
            "algorithm": "uploaded-service-areas@1",
            "area_count": len(areas),
            "allocation_rule": "uploaded explicit vehicle-area rows",
        },
    )


def realize_supply(fleet, specs, clock, rng, replication_id):
    rows = []
    supply = fleet.supply
    stream = rng.stream("studio.supply.activation", replication_id, fleet.fleet_id)
    windows = {}
    if supply.source == "generated":
        schedules = [
            (start, latest, duration)
            for count, start, latest, duration in group_windows(supply, clock)
            for _ in range(count)
        ]
        windows = {f"vehicle-{i+1:04d}": row for i, row in enumerate(schedules)}
    for spec in sorted(specs, key=lambda item: item.key.vehicle_id):
        start, end = spec.availability_start_s, spec.availability_end_s
        if supply.source == "generated":
            first, latest, duration = windows[spec.key.vehicle_id]
            start = float(stream.uniform(first, latest)) if latest > first else first
            end = start + duration
        rows.append(
            VehicleAvailability(
                replication_id=replication_id,
                vehicle=spec.key,
                active=True,
                availability_start_s=start,
                availability_end_s=end,
                initial_location_id=spec.initial_location_id,
            )
        )
    return tuple(rows)
