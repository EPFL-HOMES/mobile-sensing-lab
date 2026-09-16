"""Directed road preparation and deterministic static routing."""

from __future__ import annotations

import heapq
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Mapping, Sequence

import geopandas as gpd
import networkx as nx
import pandas as pd
from pyproj import CRS
from shapely.geometry import Point

from mobile_sensing.contracts import (
    LocationRef,
    ResolutionStatus,
    RouteEdge,
    RouteResult,
    scientific_hash,
    stable_id,
)
from mobile_sensing.contracts.configuration import TravelTimeProfileConfig
from mobile_sensing.environment.geometry import reverse_line
from mobile_sensing.environment.repair import (
    NetworkRepairError,
    NetworkRepairPolicy,
    NetworkRepairResult,
    apply_network_repair,
)


NETWORK_ALGORITHM_VERSION = "directed-network@2"
ROUTING_ALGORITHM_VERSION = "stable-dijkstra@1"


class NetworkPreparationError(ValueError):
    """Raised when supplied roads cannot be normalized without changing topology."""


@dataclass(frozen=True, slots=True)
class NetworkPreparationResult:
    nodes: gpd.GeoDataFrame
    edges: gpd.GeoDataFrame
    routing_weights: pd.DataFrame
    network_hash: str
    profile_hashes: Mapping[str, str]
    source_counts: Mapping[str, Mapping[str, int]]
    source_row_count: int
    selected_row_count: int
    endpoint_max_displacement_m: float
    repair: NetworkRepairResult
    edge_lineage: pd.DataFrame


def _canonical_scalar(value: Any, *, field: str) -> str:
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        raise NetworkPreparationError(f"road {field} cannot be missing")
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    text = str(value)
    if not text:
        raise NetworkPreparationError(f"road {field} cannot be empty")
    return text


def _one_way(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    normalized = str(value).strip().casefold()
    if normalized in {"0", "false", "no"}:
        return False
    if normalized in {"1", "true", "yes"}:
        return True
    raise NetworkPreparationError(f"invalid one_way value: {value!r}")


def _positive_number(value: Any, *, context: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise NetworkPreparationError(f"{context} must be numeric") from exc
    if not math.isfinite(number) or number <= 0:
        raise NetworkPreparationError(f"{context} must be positive and finite")
    return number


def _profile_weight(
    profile: TravelTimeProfileConfig,
    attributes: Mapping[str, Any],
    length_m: float,
    travel_time_fraction: float,
) -> tuple[float, float, str]:
    source = profile.source
    if source.kind == "edge_travel_time":
        source_duration_s = _positive_number(
            attributes.get(source.field), context=f"edge travel time field {source.field}"
        )
        duration_s = source_duration_s * travel_time_fraction
        return duration_s, length_m / duration_s, f"edge_travel_time:{source.field}"
    if source.kind == "edge_speed":
        value = attributes.get(source.field)
        if value is None or (not isinstance(value, str) and pd.isna(value)):
            if source.fallback_speed_mps is None:
                raise NetworkPreparationError(
                    f"edge speed field {source.field} is missing without a fallback"
                )
            speed_mps = float(source.fallback_speed_mps)
            provenance = "explicit_fallback_speed"
        else:
            speed_mps = _positive_number(value, context=f"edge speed field {source.field}")
            provenance = f"edge_speed:{source.field}"
        return length_m / speed_mps, speed_mps, provenance
    if source.kind == "road_class_speed":
        value = attributes.get("road_class")
        if value is not None and str(value) in source.class_speed_mps:
            speed_mps = float(source.class_speed_mps[str(value)])
            provenance = f"road_class:{value}"
        else:
            speed_mps = float(source.fallback_speed_mps)
            provenance = "explicit_fallback_speed"
        return length_m / speed_mps, speed_mps, provenance
    if source.kind == "constant_speed":
        speed_mps = float(source.speed_mps)
        return length_m / speed_mps, speed_mps, "assumed_constant_speed"
    raise NetworkPreparationError(f"unsupported travel-time source: {source.kind}")


def prepare_network(
    roads: gpd.GeoDataFrame,
    routing_extent,
    *,
    working_crs: str,
    profiles: Sequence[TravelTimeProfileConfig],
    source_content_hash: str,
    endpoint_tolerance_m: float = 0.05,
    repair_policy: NetworkRepairPolicy = "strict@1",
) -> NetworkPreparationResult:
    """Normalize a supplied directed network under one explicit topology policy."""

    crs = CRS.from_user_input(working_crs)
    if not crs.is_projected or any(
        axis.unit_name.casefold() not in {"metre", "meter"}
        or not math.isclose(axis.unit_conversion_factor, 1.0, abs_tol=1e-12)
        for axis in crs.axis_info
    ):
        raise NetworkPreparationError("working CRS must be projected in metres")
    if roads.empty or roads.crs is None:
        raise NetworkPreparationError("supplied roads must be nonempty and declare a CRS")
    required = {"u", "v", "key", "one_way"}
    if not required <= set(roads.columns):
        raise NetworkPreparationError(f"supplied roads lack fields: {sorted(required-set(roads))}")
    if endpoint_tolerance_m <= 0:
        raise ValueError("endpoint_tolerance_m must be positive")
    source_row_count = len(roads)
    metric = roads.to_crs(working_crs).copy()
    if "road" in metric.columns:
        metric = metric.loc[metric["road"].astype(bool)].copy()
    metric = metric.loc[metric.geometry.intersects(routing_extent)].copy()
    selected_row_count = len(metric)
    if metric.empty:
        raise NetworkPreparationError("routing extent retained no road records")

    try:
        repair = apply_network_repair(
            metric,
            policy=repair_policy,
            source_content_hash=source_content_hash,
            endpoint_tolerance_m=endpoint_tolerance_m,
        )
    except NetworkRepairError as exc:
        raise NetworkPreparationError(str(exc)) from exc
    normalized: list[dict[str, Any]] = []
    lineage_fields = {
        "geometry",
        "u",
        "v",
        "key",
        "one_way",
        "raw_source_u",
        "raw_source_v",
        "raw_source_key",
        "source_record_id",
        "source_component_index",
        "repair_action",
        "travel_time_fraction",
    }
    for _, values in repair.repaired_roads.iterrows():
        normalized.append(
            {
                "u": _canonical_scalar(values["u"], field="u"),
                "v": _canonical_scalar(values["v"], field="v"),
                "key": _canonical_scalar(values["key"], field="key"),
                "source_u": _canonical_scalar(values["raw_source_u"], field="raw_source_u"),
                "source_v": _canonical_scalar(values["raw_source_v"], field="raw_source_v"),
                "source_key": _canonical_scalar(values["raw_source_key"], field="raw_source_key"),
                "source_record_id": str(values["source_record_id"]),
                "source_component_index": int(values["source_component_index"]),
                "repair_action": str(values["repair_action"]),
                "travel_time_fraction": float(values["travel_time_fraction"]),
                "one_way": _one_way(values["one_way"]),
                "geometry": values.geometry,
                "attributes": {
                    name: value for name, value in values.items() if name not in lineage_fields
                },
            }
        )

    normalized.sort(key=lambda item: (item["u"], item["v"], item["key"]))
    source_keys = [(item["u"], item["v"], item["key"]) for item in normalized]
    if len(source_keys) != len(set(source_keys)):
        raise NetworkPreparationError("(u, v, key) must uniquely identify supplied road records")

    endpoint_candidates: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for item in normalized:
        endpoint_candidates[item["u"]].append(tuple(item["geometry"].coords[0]))
        endpoint_candidates[item["v"]].append(tuple(item["geometry"].coords[-1]))
    node_coordinates: dict[str, tuple[float, float]] = {}
    max_displacement = 0.0
    inconsistent: list[tuple[str, float]] = []
    for node_id, coordinates in sorted(endpoint_candidates.items()):
        canonical = min(coordinates)
        displacement = max(math.dist(canonical, candidate) for candidate in coordinates)
        max_displacement = max(max_displacement, displacement)
        if displacement > endpoint_tolerance_m:
            inconsistent.append((node_id, displacement))
        node_coordinates[node_id] = canonical
    if inconsistent:
        examples = [(node, round(distance, 6)) for node, distance in inconsistent[:5]]
        raise NetworkPreparationError(
            f"{len(inconsistent)} node IDs have inconsistent endpoints beyond "
            f"{endpoint_tolerance_m} m, examples={examples}"
        )

    edge_rows: list[dict[str, Any]] = []
    edge_attributes: dict[str, Mapping[str, Any]] = {}
    for item in normalized:
        directions = [("forward", item["u"], item["v"], item["geometry"], False)]
        if not item["one_way"]:
            directions.append(
                (
                    "reverse",
                    item["v"],
                    item["u"],
                    reverse_line(item["geometry"]),
                    True,
                )
            )
        for direction, u, v, geometry, synthetic_reverse in directions:
            identity = {
                "source_content_hash": source_content_hash,
                "repair_hash": repair.repair_hash,
                "source_u": item["source_u"],
                "source_v": item["source_v"],
                "source_key": item["source_key"],
                "source_component_index": item["source_component_index"],
                "direction": direction,
            }
            edge_id = stable_id("edge", identity)
            edge_rows.append(
                {
                    "edge_id": edge_id,
                    "u_node_id": u,
                    "v_node_id": v,
                    "source_u": item["source_u"],
                    "source_v": item["source_v"],
                    "source_key": item["source_key"],
                    "source_record_id": item["source_record_id"],
                    "source_component_index": item["source_component_index"],
                    "repair_action": item["repair_action"],
                    "direction": direction,
                    "is_synthetic_reverse": synthetic_reverse,
                    "length_m": float(geometry.length),
                    "geometry": geometry,
                }
            )
            edge_attributes[edge_id] = item["attributes"]
            edge_attributes[edge_id] = {
                **item["attributes"],
                "travel_time_fraction": item["travel_time_fraction"],
            }
    edge_rows.sort(key=lambda item: item["edge_id"])
    edges = gpd.GeoDataFrame(edge_rows, geometry="geometry", crs=working_crs)

    node_rows = [
        {"node_id": node_id, "x_m": point[0], "y_m": point[1], "geometry": Point(point)}
        for node_id, point in sorted(node_coordinates.items())
    ]
    nodes = gpd.GeoDataFrame(node_rows, geometry="geometry", crs=working_crs)
    network_hash = scientific_hash(
        {
            "algorithm": NETWORK_ALGORITHM_VERSION,
            "repair_hash": repair.repair_hash,
            "working_crs": working_crs,
            "nodes": [[row.node_id, row.x_m, row.y_m] for row in nodes.itertuples()],
            "edges": [
                [
                    row.edge_id,
                    row.u_node_id,
                    row.v_node_id,
                    row.source_u,
                    row.source_v,
                    row.source_key,
                    row.source_component_index,
                    row.direction,
                    row.length_m,
                    row.geometry.wkb_hex,
                ]
                for row in edges.itertuples()
            ],
        }
    )

    weight_rows: list[dict[str, Any]] = []
    profile_hashes: dict[str, str] = {}
    source_counts: dict[str, Mapping[str, int]] = {}
    for profile in sorted(profiles, key=lambda item: item.profile_id):
        counts: Counter[str] = Counter()
        vector: list[list[Any]] = []
        for edge in edges.itertuples():
            duration_s, speed_mps, provenance = _profile_weight(
                profile,
                edge_attributes[edge.edge_id],
                float(edge.length_m),
                float(edge_attributes[edge.edge_id]["travel_time_fraction"]),
            )
            counts[provenance] += 1
            weight_rows.append(
                {
                    "profile_id": profile.profile_id,
                    "edge_id": edge.edge_id,
                    "duration_s": duration_s,
                    "speed_mps": speed_mps,
                    "source_provenance": provenance,
                }
            )
            vector.append([edge.edge_id, duration_s, speed_mps, provenance])
        profile_hashes[profile.profile_id] = scientific_hash(
            {
                "algorithm": ROUTING_ALGORITHM_VERSION,
                "network_hash": network_hash,
                "profile": profile,
                "weights": vector,
            }
        )
        source_counts[profile.profile_id] = dict(sorted(counts.items()))
    routing_weights = (
        pd.DataFrame(weight_rows)
        .sort_values(["profile_id", "edge_id"], kind="stable")
        .reset_index(drop=True)
    )
    edge_lineage = edges[
        [
            "edge_id",
            "source_record_id",
            "source_u",
            "source_v",
            "source_key",
            "source_component_index",
            "direction",
            "repair_action",
        ]
    ].copy()
    return NetworkPreparationResult(
        nodes=nodes,
        edges=edges,
        routing_weights=routing_weights,
        network_hash=network_hash,
        profile_hashes=profile_hashes,
        source_counts=source_counts,
        source_row_count=source_row_count,
        selected_row_count=selected_row_count,
        endpoint_max_displacement_m=max_displacement,
        repair=repair,
        edge_lineage=edge_lineage,
    )


class PreparedRoutingService:
    """Bounded-cache deterministic Dijkstra over a directed NetworkX multigraph."""

    def __init__(
        self,
        nodes: gpd.GeoDataFrame,
        edges: gpd.GeoDataFrame,
        routing_weights: pd.DataFrame,
        *,
        network_hash: str,
        profile_hashes: Mapping[str, str],
        cache_size: int = 4096,
    ) -> None:
        if cache_size <= 0:
            raise ValueError("routing cache_size must be positive")
        self.network_hash = network_hash
        self.profile_hashes = dict(profile_hashes)
        if nodes.crs is None:
            raise ValueError("routing nodes require a CRS")
        self.working_crs = nodes.crs.to_string()
        if not self.profile_hashes:
            raise ValueError("routing service requires at least one profile")
        if (
            nodes.node_id.astype(str).duplicated().any()
            or edges.edge_id.astype(str).duplicated().any()
        ):
            raise ValueError("routing nodes and edges require unique IDs")
        graph = nx.MultiDiGraph()
        for row in nodes.itertuples():
            graph.add_node(row.node_id, x=float(row.x_m), y=float(row.y_m))
        weight_rows = {
            (row.profile_id, row.edge_id): float(row.duration_s)
            for row in routing_weights.itertuples()
        }
        expected_pairs = {
            (profile_id, edge_id)
            for profile_id in self.profile_hashes
            for edge_id in edges.edge_id.astype(str)
        }
        if (
            len(routing_weights) != len(expected_pairs)
            or len(weight_rows) != len(routing_weights)
            or set(weight_rows) != expected_pairs
        ):
            raise ValueError("routing weights do not cover every profile/edge pair")
        if any(not math.isfinite(value) or value <= 0 for value in weight_rows.values()):
            raise ValueError("routing weights must be positive and finite")
        for edge in edges.itertuples():
            if edge.u_node_id not in graph or edge.v_node_id not in graph:
                raise ValueError("routing edge references an unknown node")
            if not math.isfinite(float(edge.length_m)) or float(edge.length_m) <= 0:
                raise ValueError("routing edge lengths must be positive and finite")
            durations = {
                profile_id: weight_rows[(profile_id, edge.edge_id)]
                for profile_id in self.profile_hashes
            }
            graph.add_edge(
                edge.u_node_id,
                edge.v_node_id,
                key=edge.edge_id,
                edge_id=edge.edge_id,
                length_m=float(edge.length_m),
                durations=durations,
            )
        self._graph = graph
        self._edge_data = {key: data for _, _, key, data in graph.edges(keys=True, data=True)}
        self._adjacency = {
            profile: {
                node: tuple(
                    (target, edge_id, float(data["durations"][profile]))
                    for _, target, edge_id, data in sorted(
                        graph.out_edges(node, keys=True, data=True), key=lambda row: row[2]
                    )
                )
                for node in graph
            }
            for profile in self.profile_hashes
        }
        self._node_index = {node: index for index, node in enumerate(sorted(graph))}
        self._edge_ids = tuple(sorted(self._edge_data))
        edge_index = {edge: index for index, edge in enumerate(self._edge_ids)}
        self._indexed_adjacency = {
            profile: tuple(
                tuple(
                    (self._node_index[target], edge_index[edge], weight)
                    for target, edge, weight in self._adjacency[profile][node]
                )
                for node in self._node_index
            )
            for profile in self.profile_hashes
        }
        self._incoming_indexed = {}
        for profile, adjacency in self._indexed_adjacency.items():
            incoming = [[] for _ in self._node_index]
            for source, edges in enumerate(adjacency):
                for target, edge, weight in edges:
                    incoming[target].append((source, edge, weight))
            self._incoming_indexed[profile] = tuple(tuple(values) for values in incoming)
        self._bound_graphs = {}
        self._bounds_cached = lru_cache(maxsize=128)(self._lower_bound_tree)
        components = sorted(
            (tuple(sorted(group)) for group in nx.strongly_connected_components(graph)),
            key=lambda group: group[0],
        )
        self._component = {node: index for index, group in enumerate(components) for node in group}
        self._components = components
        self._component_graph = nx.condensation(graph, scc=components)
        self._reachable_components = lru_cache(maxsize=128)(
            lambda component: frozenset(nx.descendants(self._component_graph, component))
            | {component}
        )
        self._route_cached = lru_cache(maxsize=cache_size)(self._route_uncached)

    def reachable(self, source_node_id, target_node_id):
        if source_node_id not in self._component or target_node_id not in self._component:
            return False
        source, target = self._component[source_node_id], self._component[target_node_id]
        return source == target or target in self._reachable_components(source)

    def roundtrip_node_ids(self, node_id):
        return frozenset(self._components[self._component[node_id]])

    def _native_distances(self, profile_id, source):
        from scipy.sparse import csr_matrix
        from scipy.sparse.csgraph import dijkstra

        if profile_id not in self._bound_graphs:
            minimum = {}
            for node, edges in self._adjacency[profile_id].items():
                for target, _, duration in edges:
                    key = (self._node_index[node], self._node_index[target])
                    minimum[key] = min(minimum.get(key, math.inf), duration)
            rows, cols, weights = zip(*((a, b, value) for (a, b), value in minimum.items()))
            self._bound_graphs[profile_id] = csr_matrix(
                (weights, (rows, cols)), shape=(len(self._node_index), len(self._node_index))
            )
        return dijkstra(
            self._bound_graphs[profile_id], directed=True, indices=self._node_index[source]
        )

    def _lower_bound_tree(self, profile_id, source):
        """Native costs only bound candidate order; accepted pairs use exact routes."""
        import numpy as np

        distances = self._native_distances(profile_id, source)
        # Positive-weight simple paths have at most |V|-1 edges. A generous
        # forward-error margin makes native sums conservative relative to fsum.
        finite = np.isfinite(distances)
        margin = np.maximum(1.0, distances[finite]) * (
            64 * np.finfo(float).eps * len(self._node_index)
        )
        distances[finite] = np.maximum(0.0, distances[finite] - margin)
        return distances

    def travel_time_lower_bounds_from(self, profile_id, source, targets):
        if profile_id not in self.profile_hashes:
            raise KeyError(profile_id)
        if source not in self._node_index:
            return {target: math.inf for target in targets}
        distances = self._bounds_cached(profile_id, source)
        return {
            target: (
                float(distances[self._node_index[target]])
                if target in self._node_index
                else math.inf
            )
            for target in targets
        }

    def route(self, profile_id: str, source_node_id: str, target_node_id: str) -> RouteResult:
        if profile_id not in self.profile_hashes:
            raise KeyError(f"unknown routing profile: {profile_id}")
        return self._route_cached(profile_id, source_node_id, target_node_id)

    def cache_info(self):
        return self._route_cached.cache_info()

    def outgoing_neighbor_node_ids(self, profile_id: str, source_node_id: str) -> tuple[str, ...]:
        """Return canonical immediate directed neighbors for operational cruising."""

        if profile_id not in self.profile_hashes:
            raise KeyError(f"unknown routing profile: {profile_id}")
        if source_node_id not in self._graph:
            return ()
        return tuple(
            sorted(
                {
                    str(target)
                    for _, target, _, data in self._graph.out_edges(
                        source_node_id, keys=True, data=True
                    )
                    if float(data["durations"][profile_id]) > 0.0
                }
            )
        )

    def location_for_node(self, node_id: str) -> LocationRef:
        """Create a stable resolved location reference for a prepared network node."""

        if node_id not in self._graph:
            raise KeyError(f"unknown routing node: {node_id}")
        data = self._graph.nodes[node_id]
        x = float(data["x"])
        y = float(data["y"])
        return LocationRef(
            location_id=stable_id(
                "network_node_location",
                {"network_hash": self.network_hash, "node_id": node_id},
            ),
            original_x=x,
            original_y=y,
            original_crs=self.working_crs,
            node_id=node_id,
            snapped_x=x,
            snapped_y=y,
            snap_distance_m=0.0,
            resolution_status=ResolutionStatus.RESOLVED,
        )

    def _route_uncached(
        self, profile_id: str, source_node_id: str, target_node_id: str
    ) -> RouteResult:
        return self.routes_from(profile_id, source_node_id, (target_node_id,))[target_node_id]

    def routes_from(
        self, profile_id: str, source_node_id: str, target_node_ids: Sequence[str]
    ) -> Mapping[str, RouteResult]:
        """Resolve a bounded target set with one deterministic source-tree search."""

        if profile_id not in self.profile_hashes:
            raise KeyError(f"unknown routing profile: {profile_id}")
        targets = tuple(sorted(set(target_node_ids)))
        if not targets:
            return {}
        best = self._search_from(profile_id, source_node_id, targets)
        return {
            target: self._route_result(profile_id, source_node_id, target, best.get(target))
            for target in targets
        }

    def travel_times_from(self, profile_id, source_node_id, target_node_ids):
        """Exact route totals without allocating every RouteEdge object for costs."""
        targets = tuple(sorted(set(target_node_ids)))
        best = self._search_from(profile_id, source_node_id, targets)
        return {
            target: (
                math.fsum(
                    self._edge_data[edge]["durations"][profile_id] for edge in best[target][1]
                )
                if target in best
                else None
            )
            for target in targets
        }

    def _search_from(self, profile_id, source_node_id, targets):
        # Integer ranks preserve exactly the original string lexicographic ties.
        source = self._node_index.get(source_node_id)
        if source is None:
            return {}
        requested = {
            target: self._node_index[target]
            for target in targets
            if self.reachable(source_node_id, target)
        }
        if len(requested) >= 32:
            native = self._native_lexicographic_paths(profile_id, source_node_id, requested)
            if native is not None:
                return native
        remaining = set(requested.values())
        adjacency = self._indexed_adjacency[profile_id]
        best = [None] * len(self._node_index)
        best[source] = (0.0, ())
        heap = [(0.0, (), source)]
        while heap and remaining:
            duration, path, node = heapq.heappop(heap)
            if best[node] != (duration, path):
                continue
            remaining.discard(node)
            for target, edge, weight in adjacency[node]:
                candidate = (duration + weight, path + (edge,))
                current = best[target]
                if current is None or candidate < current:
                    best[target] = candidate
                    heapq.heappush(heap, (candidate[0], candidate[1], target))
        return {
            target: (best[index][0], tuple(self._edge_ids[edge] for edge in best[index][1]))
            for target, index in requested.items()
            if best[index] is not None
        }

    def _native_lexicographic_paths(self, profile_id, source_node_id, requested):
        """Recover exact lexicographic paths on the strictly increasing shortest-path DAG."""
        import numpy as np

        distances = self._native_distances(profile_id, source_node_id)
        maximum = max(float(distances[index]) for index in requested.values())
        if not math.isfinite(maximum):
            return None
        numeric = distances.tolist()
        paths = [None] * len(numeric)
        source = self._node_index[source_node_id]
        paths[source] = ()
        incoming = self._incoming_indexed[profile_id]
        for node in np.argsort(distances, kind="stable"):
            duration = numeric[node]
            if duration > maximum:
                break
            if node == source:
                continue
            selected = None
            for predecessor, edge, weight in incoming[node]:
                if numeric[predecessor] + weight != duration:
                    continue
                # Float64 can erase an extremely small positive increment. Such
                # graphs need the original heap ordering instead of a DAG pass.
                if numeric[predecessor] >= duration or paths[predecessor] is None:
                    return None
                candidate = paths[predecessor] + (edge,)
                if selected is None or candidate < selected:
                    selected = candidate
            if selected is None:
                return None
            paths[node] = selected
        return {
            target: (numeric[index], tuple(self._edge_ids[edge] for edge in paths[index]))
            for target, index in requested.items()
        }

    def _route_result(self, profile_id, source_node_id, target_node_id, chosen):
        profile_hash = self.profile_hashes[profile_id]
        if source_node_id not in self._graph or target_node_id not in self._graph:
            reason = "unknown_node"
        elif chosen is None:
            reason = "no_path"
        else:
            reason = None
        if reason is not None:
            return RouteResult(
                reachable=False,
                edges=(),
                total_duration_s=0.0,
                total_distance_m=0.0,
                network_hash=self.network_hash,
                profile_hash=profile_hash,
                reason=reason,
            )
        assert chosen is not None
        route_edges = tuple(
            RouteEdge(
                edge_id=edge_id,
                length_m=float(self._edge_data[edge_id]["length_m"]),
                duration_s=float(self._edge_data[edge_id]["durations"][profile_id]),
            )
            for edge_id in chosen[1]
        )
        return RouteResult(
            reachable=True,
            edges=route_edges,
            total_duration_s=math.fsum(edge.duration_s for edge in route_edges),
            total_distance_m=math.fsum(edge.length_m for edge in route_edges),
            network_hash=self.network_hash,
            profile_hash=profile_hash,
        )
