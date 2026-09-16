"""Auditable topology-preserving repair of supplied road records."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Sequence

import geopandas as gpd
import pandas as pd
import shapely
from shapely import force_2d
from shapely.geometry import LineString, MultiLineString

from mobile_sensing.contracts import scientific_hash, stable_id


NETWORK_REPAIR_ALGORITHM_VERSION = "network-topology-repair@1"
NetworkRepairPolicy = Literal["strict@1", "conservative_repair@1", "quarantine_invalid@1"]


class NetworkRepairError(ValueError):
    """Raised when the requested policy cannot produce a valid derived network."""


@dataclass(frozen=True, slots=True)
class NetworkDiagnosis:
    row_diagnostics: pd.DataFrame
    endpoint_conflicts: pd.DataFrame
    summary: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class NetworkRepairPlan:
    policy: NetworkRepairPolicy
    actions: pd.DataFrame
    readiness_grade: Literal["strict_ready", "scenario_ready_with_quarantine", "not_ready"]
    repair_hash: str


@dataclass(frozen=True, slots=True)
class NetworkRepairResult:
    repaired_roads: gpd.GeoDataFrame
    repair_actions: pd.DataFrame
    node_lineage: pd.DataFrame
    quarantined_edges: gpd.GeoDataFrame
    repair_issues: pd.DataFrame
    repair_summary: pd.DataFrame
    repair_hash: str
    policy: NetworkRepairPolicy
    readiness_grade: Literal["strict_ready", "scenario_ready_with_quarantine", "not_ready"]


@dataclass(slots=True)
class _SourceRow:
    source_record_id: str
    source_u: str
    source_v: str
    source_key: str
    one_way: bool
    geometry: Any
    attributes: dict[str, Any]
    status: str
    reason: str
    parts: tuple[LineString, ...]
    endpoint_group: tuple[int, ...]
    group_coordinates: tuple[tuple[float, float], ...]
    terminals: tuple[int, int] | None
    terminal_sources: dict[int, str]


def _canonical_scalar(value: Any, *, field: str) -> str:
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        raise NetworkRepairError(f"road {field} cannot be missing")
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    text = str(value)
    if not text:
        raise NetworkRepairError(f"road {field} cannot be empty")
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
    raise NetworkRepairError(f"invalid one_way value: {value!r}")


def _diameter_bounded_clusters(
    coordinates: Sequence[tuple[float, float]], tolerance_m: float
) -> tuple[tuple[int, ...], tuple[tuple[float, float], ...]]:
    """Deterministic complete-linkage clusters with diameter at most tolerance."""

    ordered = sorted(enumerate(coordinates), key=lambda item: (item[1], item[0]))
    members: list[list[tuple[int, tuple[float, float]]]] = []
    for original_index, coordinate in ordered:
        candidates = []
        for cluster_index, cluster in enumerate(members):
            if all(math.dist(coordinate, observed) <= tolerance_m for _, observed in cluster):
                candidates.append((cluster[0][1], cluster_index))
        if candidates:
            _, chosen = min(candidates)
            members[chosen].append((original_index, coordinate))
        else:
            members.append([(original_index, coordinate)])
    members.sort(key=lambda cluster: min(coordinate for _, coordinate in cluster))
    assignment = [-1] * len(coordinates)
    canonical = []
    for cluster_index, cluster in enumerate(members):
        canonical.append(min(coordinate for _, coordinate in cluster))
        for original_index, _ in cluster:
            assignment[original_index] = cluster_index
    return tuple(assignment), tuple(canonical)


def _component_graph(
    parts: Sequence[LineString], tolerance_m: float
) -> tuple[tuple[int, ...], tuple[tuple[float, float], ...], tuple[int, ...], int]:
    endpoints = [
        tuple(coordinate) for part in parts for coordinate in (part.coords[0], part.coords[-1])
    ]
    assignment, canonical = _diameter_bounded_clusters(endpoints, tolerance_m)
    adjacency: dict[int, set[int]] = defaultdict(set)
    degree = [0] * len(canonical)
    for index in range(len(parts)):
        left, right = assignment[2 * index], assignment[2 * index + 1]
        adjacency[left].add(right)
        adjacency[right].add(left)
        degree[left] += 1
        degree[right] += 1
    remaining = set(range(len(canonical)))
    components = 0
    while remaining:
        components += 1
        stack = [min(remaining)]
        remaining.remove(stack[0])
        while stack:
            node = stack.pop()
            for neighbor in adjacency[node]:
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    stack.append(neighbor)
    return assignment, canonical, tuple(degree), components


def _normalize_rows(roads: gpd.GeoDataFrame, endpoint_tolerance_m: float) -> list[_SourceRow]:
    required = {"u", "v", "key", "one_way"}
    if not required <= set(roads.columns):
        raise NetworkRepairError(f"supplied roads lack fields: {sorted(required-set(roads))}")
    reserved = {
        "raw_source_u",
        "raw_source_v",
        "raw_source_key",
        "source_record_id",
        "source_component_index",
        "repair_action",
        "travel_time_fraction",
    }
    collisions = sorted(reserved & set(roads.columns))
    if collisions:
        raise NetworkRepairError(f"supplied roads use reserved repair fields: {collisions}")
    rows: list[_SourceRow] = []
    for _, series in roads.iterrows():
        source_u = _canonical_scalar(series["u"], field="u")
        source_v = _canonical_scalar(series["v"], field="v")
        source_key = _canonical_scalar(series["key"], field="key")
        source_record_id = f"{source_u}/{source_v}/{source_key}"
        attributes = {
            name: value
            for name, value in series.items()
            if name not in {"geometry", "u", "v", "key", "one_way"}
        }
        geometry = series.geometry
        status = "invalid_geometry"
        reason = "road geometry must be a nonempty positive LineString"
        parts: tuple[LineString, ...] = ()
        assignment: tuple[int, ...] = ()
        canonical: tuple[tuple[float, float], ...] = ()
        terminals: tuple[int, int] | None = None
        if geometry is not None and not geometry.is_empty:
            geometry = force_2d(geometry)
            if isinstance(geometry, LineString):
                candidate_parts = (geometry,)
            elif isinstance(geometry, MultiLineString):
                candidate_parts = tuple(geometry.geoms)
            else:
                candidate_parts = ()
                reason = f"road geometry is nonlineal ({geometry.geom_type})"
            if candidate_parts and all(
                len(part.coords) >= 2 and math.isfinite(part.length) and part.length > 0
                for part in candidate_parts
            ):
                parts = candidate_parts
                merged = (
                    shapely.line_merge(geometry)
                    if isinstance(geometry, MultiLineString)
                    else geometry
                )
                if isinstance(merged, LineString):
                    source_start = candidate_parts[0].coords[0]
                    if math.dist(merged.coords[-1], source_start) < math.dist(
                        merged.coords[0], source_start
                    ):
                        merged = LineString(tuple(reversed(merged.coords)))
                    status = "mergeable" if len(parts) > 1 else "valid"
                    reason = ""
                    parts = (merged,)
                    assignment, canonical, _, _ = _component_graph(parts, endpoint_tolerance_m)
                    terminals = (assignment[0], assignment[1])
                else:
                    assignment, canonical, degree, component_count = _component_graph(
                        parts, endpoint_tolerance_m
                    )
                    leaves = tuple(index for index, value in enumerate(degree) if value == 1)
                    if component_count > 1:
                        status = "disconnected"
                        reason = f"component linework has {component_count} disconnected groups"
                    elif _one_way(series["one_way"]):
                        status = "ambiguous_one_way_branch"
                        reason = "one-way branching linework has ambiguous component direction"
                    elif len(leaves) != 2 or any(
                        value not in {1} and value % 2 for value in degree
                    ):
                        status = "ambiguous_branch"
                        reason = "connected linework does not have exactly two terminal leaves"
                    else:
                        status = "repairable_branch"
                        reason = ""
                        terminals = (leaves[0], leaves[1])
        rows.append(
            _SourceRow(
                source_record_id=source_record_id,
                source_u=source_u,
                source_v=source_v,
                source_key=source_key,
                one_way=_one_way(series["one_way"]),
                geometry=geometry,
                attributes=attributes,
                status=status,
                reason=reason,
                parts=parts,
                endpoint_group=assignment,
                group_coordinates=canonical,
                terminals=terminals,
                terminal_sources={},
            )
        )
    rows.sort(key=lambda row: (row.source_u, row.source_v, row.source_key))
    keys = [(row.source_u, row.source_v, row.source_key) for row in rows]
    if len(keys) != len(set(keys)):
        raise NetworkRepairError("(u, v, key) must uniquely identify supplied road records")
    return rows


def _assign_branch_terminals(rows: Sequence[_SourceRow], tolerance_m: float) -> None:
    observed: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for row in rows:
        if row.status in {"valid", "mergeable"}:
            observed[row.source_u].append(tuple(row.parts[0].coords[0]))
            observed[row.source_v].append(tuple(row.parts[0].coords[-1]))
    for row in rows:
        if row.status != "repairable_branch" or row.terminals is None:
            continue
        first, second = row.terminals
        a, b = row.group_coordinates[first], row.group_coordinates[second]
        u_candidates, v_candidates = observed[row.source_u], observed[row.source_v]
        if not u_candidates or not v_candidates:
            row.status = "unresolved_branch_terminals"
            row.reason = "branch terminals cannot be matched to both source node identities"
            continue
        costs = []
        for u_coordinate, v_coordinate, mapping in (
            (a, b, {first: row.source_u, second: row.source_v}),
            (b, a, {second: row.source_u, first: row.source_v}),
        ):
            du = min(math.dist(u_coordinate, candidate) for candidate in u_candidates)
            dv = min(math.dist(v_coordinate, candidate) for candidate in v_candidates)
            costs.append((max(du, dv), du + dv, mapping))
        maximum, _, mapping = min(costs, key=lambda item: (item[0], item[1]))
        if maximum > tolerance_m:
            row.status = "unresolved_branch_terminals"
            row.reason = "branch terminals disagree with source node endpoint observations"
            continue
        row.terminal_sources.update(mapping)
        observed[row.source_u].append(a if mapping.get(first) == row.source_u else b)
        observed[row.source_v].append(a if mapping.get(first) == row.source_v else b)


def _source_node_clusters(
    rows: Sequence[_SourceRow], tolerance_m: float, source_content_hash: str
) -> tuple[
    dict[str, list[tuple[tuple[float, float], str]]], list[dict[str, Any]], list[dict[str, Any]]
]:
    observations: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for row in rows:
        if row.status in {"valid", "mergeable"}:
            observations[row.source_u].append(tuple(row.parts[0].coords[0]))
            observations[row.source_v].append(tuple(row.parts[0].coords[-1]))
        elif row.status == "repairable_branch" and row.terminals is not None:
            for group, source_node in row.terminal_sources.items():
                observations[source_node].append(row.group_coordinates[group])
    lookup: dict[str, list[tuple[tuple[float, float], str]]] = {}
    lineage: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for source_node, coordinates in sorted(observations.items()):
        assignment, canonical = _diameter_bounded_clusters(coordinates, tolerance_m)
        derived = []
        for cluster_index, coordinate in enumerate(canonical):
            node_id = (
                source_node
                if len(canonical) == 1
                else stable_id(
                    "split_node",
                    {
                        "source_content_hash": source_content_hash,
                        "source_node_id": source_node,
                        "cluster_coordinate": coordinate,
                        "endpoint_tolerance_m": tolerance_m,
                    },
                )
            )
            count = Counter(assignment)[cluster_index]
            derived.append((coordinate, node_id))
            lineage.append(
                {
                    "derived_node_id": node_id,
                    "source_node_id": source_node,
                    "node_kind": "retained" if len(canonical) == 1 else "split_conflict",
                    "cluster_index": cluster_index,
                    "x_m": coordinate[0],
                    "y_m": coordinate[1],
                    "observation_count": count,
                }
            )
        lookup[source_node] = derived
        if len(canonical) > 1:
            conflicts.append(
                {
                    "issue_code": "inconsistent_node_endpoint",
                    "source_record_id": "",
                    "source_node_id": source_node,
                    "severity": "warning",
                    "message": f"source node split into {len(canonical)} coordinate clusters",
                }
            )
    return lookup, lineage, conflicts


def _lookup_source_node(
    lookup: Mapping[str, Sequence[tuple[tuple[float, float], str]]],
    source_node: str,
    coordinate: tuple[float, float],
    tolerance_m: float,
) -> str:
    candidates = [
        (math.dist(coordinate, canonical), node_id)
        for canonical, node_id in lookup[source_node]
        if math.dist(coordinate, canonical) <= tolerance_m
    ]
    if not candidates:
        raise NetworkRepairError(
            f"source node {source_node!r} has no endpoint cluster within tolerance"
        )
    return min(candidates)[1]


def diagnose_network(
    roads: gpd.GeoDataFrame, *, endpoint_tolerance_m: float = 0.05
) -> NetworkDiagnosis:
    """Diagnose source-record geometry and reused endpoint identities without mutation."""

    if endpoint_tolerance_m <= 0:
        raise ValueError("endpoint_tolerance_m must be positive")
    rows = _normalize_rows(roads, endpoint_tolerance_m)
    _assign_branch_terminals(rows, endpoint_tolerance_m)
    _, _, conflicts = _source_node_clusters(rows, endpoint_tolerance_m, "0" * 64)
    row_diagnostics = pd.DataFrame(
        [
            {
                "source_record_id": row.source_record_id,
                "source_u": row.source_u,
                "source_v": row.source_v,
                "source_key": row.source_key,
                "one_way": row.one_way,
                "status": row.status,
                "component_count": len(row.parts),
                "reason": row.reason,
            }
            for row in rows
        ]
    )
    endpoint_conflicts = pd.DataFrame(
        conflicts,
        columns=("issue_code", "source_record_id", "source_node_id", "severity", "message"),
    )
    if not endpoint_conflicts.empty:
        endpoint_conflicts.loc[:, "severity"] = "error"
    counts = Counter(row.status for row in rows)
    return NetworkDiagnosis(
        row_diagnostics=row_diagnostics,
        endpoint_conflicts=endpoint_conflicts,
        summary={
            "source_row_count": len(rows),
            "status_counts": dict(sorted(counts.items())),
            "endpoint_conflict_node_count": len(conflicts),
        },
    )


def plan_network_repair(
    roads: gpd.GeoDataFrame,
    *,
    policy: NetworkRepairPolicy,
    source_content_hash: str,
    endpoint_tolerance_m: float = 0.05,
) -> NetworkRepairPlan:
    """Return deterministic row actions and the anticipated readiness grade."""

    if policy == "strict@1":
        diagnosis = diagnose_network(roads, endpoint_tolerance_m=endpoint_tolerance_m)
        blocked = (
            any(status not in {"valid", "mergeable"} for status in diagnosis.row_diagnostics.status)
            or not diagnosis.endpoint_conflicts.empty
        )
        actions = diagnosis.row_diagnostics[
            [
                "source_record_id",
                "source_u",
                "source_v",
                "source_key",
                "component_count",
                "reason",
            ]
        ].copy()
        actions["action"] = diagnosis.row_diagnostics.status.map(
            {"valid": "retained", "mergeable": "merged_contiguous"}
        ).fillna("rejected")
        actions["emitted_component_count"] = actions.action.isin(
            {"retained", "merged_contiguous"}
        ).astype(int)
        actions = actions[
            [
                "source_record_id",
                "source_u",
                "source_v",
                "source_key",
                "action",
                "component_count",
                "emitted_component_count",
                "reason",
            ]
        ].sort_values("source_record_id", kind="stable")
        repair_hash = scientific_hash(
            {
                "algorithm_version": NETWORK_REPAIR_ALGORITHM_VERSION,
                "policy": policy,
                "source_content_hash": source_content_hash,
                "endpoint_tolerance_m": endpoint_tolerance_m,
                "diagnosis": diagnosis.summary,
                "actions": actions.to_dict(orient="records"),
            }
        )
        return NetworkRepairPlan(
            policy=policy,
            actions=actions.reset_index(drop=True),
            readiness_grade="not_ready" if blocked else "strict_ready",
            repair_hash=repair_hash,
        )
    result = apply_network_repair(
        roads,
        policy=policy,
        source_content_hash=source_content_hash,
        endpoint_tolerance_m=endpoint_tolerance_m,
    )
    return NetworkRepairPlan(
        policy=policy,
        actions=result.repair_actions.copy(),
        readiness_grade=result.readiness_grade,
        repair_hash=result.repair_hash,
    )


def apply_network_repair(
    roads: gpd.GeoDataFrame,
    *,
    policy: NetworkRepairPolicy,
    source_content_hash: str,
    endpoint_tolerance_m: float = 0.05,
) -> NetworkRepairResult:
    """Create a traceable derived road table; never bridge geometric gaps."""

    if policy not in {"strict@1", "conservative_repair@1", "quarantine_invalid@1"}:
        raise ValueError(f"unsupported network repair policy: {policy}")
    if endpoint_tolerance_m <= 0:
        raise ValueError("endpoint_tolerance_m must be positive")
    rows = _normalize_rows(roads, endpoint_tolerance_m)
    _assign_branch_terminals(rows, endpoint_tolerance_m)
    lookup, node_lineage, issues = _source_node_clusters(
        rows, endpoint_tolerance_m, source_content_hash
    )
    invalid = [row for row in rows if row.status not in {"valid", "mergeable", "repairable_branch"}]
    branching = [row for row in rows if row.status == "repairable_branch"]
    conflict_nodes = {item["source_node_id"] for item in issues}
    if policy == "strict@1" and (invalid or branching or conflict_nodes):
        details = []
        nonmergeable = len(invalid) + len(branching)
        if nonmergeable:
            details.append(f"{nonmergeable} disconnected MultiLineStrings")
        if conflict_nodes:
            details.append(
                f"{len(conflict_nodes)} node IDs have inconsistent endpoints beyond "
                f"{endpoint_tolerance_m} m"
            )
        raise NetworkRepairError("; ".join(details))

    repaired_rows: list[dict[str, Any]] = []
    action_rows: list[dict[str, Any]] = []
    quarantined_rows: list[dict[str, Any]] = []
    internal_coordinates = [
        coordinate
        for row in rows
        if policy == "conservative_repair@1" and row.status == "repairable_branch"
        for group, coordinate in enumerate(row.group_coordinates)
        if group not in row.terminal_sources
    ]
    internal_lookup: dict[tuple[float, float], str] = {}
    internal_nodes: list[dict[str, Any]] = []
    if internal_coordinates:
        assignment, canonical = _diameter_bounded_clusters(
            internal_coordinates, endpoint_tolerance_m
        )
        for cluster_index, cluster_coordinate in enumerate(canonical):
            members = [
                coordinate
                for index, coordinate in enumerate(internal_coordinates)
                if assignment[index] == cluster_index
            ]
            nearby_ids = {
                node_id
                for clusters in lookup.values()
                for source_coordinate, node_id in clusters
                if all(
                    math.dist(member, source_coordinate) <= endpoint_tolerance_m
                    for member in members
                )
            }
            if len(nearby_ids) == 1:
                node_id = next(iter(nearby_ids))
            else:
                node_id = stable_id(
                    "repair_junction",
                    {
                        "source_content_hash": source_content_hash,
                        "coordinate": cluster_coordinate,
                        "endpoint_tolerance_m": endpoint_tolerance_m,
                    },
                )
                internal_nodes.append(
                    {
                        "derived_node_id": node_id,
                        "source_node_id": "",
                        "node_kind": "component_junction",
                        "cluster_index": cluster_index,
                        "x_m": cluster_coordinate[0],
                        "y_m": cluster_coordinate[1],
                        "observation_count": len(members),
                    }
                )
            for member in members:
                internal_lookup[member] = node_id

    for row in rows:
        allowed_branch = policy == "conservative_repair@1" and row.status == "repairable_branch"
        accepted = row.status in {"valid", "mergeable"} or allowed_branch
        if not accepted:
            reason = row.reason or "non-mergeable geometry quarantined by policy"
            action_rows.append(
                {
                    "source_record_id": row.source_record_id,
                    "source_u": row.source_u,
                    "source_v": row.source_v,
                    "source_key": row.source_key,
                    "action": "quarantined",
                    "component_count": len(row.parts),
                    "emitted_component_count": 0,
                    "reason": reason,
                }
            )
            quarantined_rows.append(
                {
                    "source_record_id": row.source_record_id,
                    "source_u": row.source_u,
                    "source_v": row.source_v,
                    "source_key": row.source_key,
                    "issue_code": row.status,
                    "reason": reason,
                    "geometry": row.geometry,
                }
            )
            issues.append(
                {
                    "issue_code": row.status,
                    "source_record_id": row.source_record_id,
                    "source_node_id": "",
                    "severity": "warning",
                    "message": reason,
                }
            )
            continue

        if row.status == "repairable_branch":
            action = "split_branching"
            parts = row.parts
        else:
            action = "merged_contiguous" if row.status == "mergeable" else "retained"
            parts = row.parts
        total_length = math.fsum(part.length for part in parts)
        for component_index, part in enumerate(parts):
            left_coordinate = tuple(part.coords[0])
            right_coordinate = tuple(part.coords[-1])
            if row.status == "repairable_branch":
                left_group = row.endpoint_group[2 * component_index]
                right_group = row.endpoint_group[2 * component_index + 1]
                left_source = row.terminal_sources.get(left_group)
                right_source = row.terminal_sources.get(right_group)
                u_node = (
                    _lookup_source_node(lookup, left_source, left_coordinate, endpoint_tolerance_m)
                    if left_source is not None
                    else internal_lookup[row.group_coordinates[left_group]]
                )
                v_node = (
                    _lookup_source_node(
                        lookup, right_source, right_coordinate, endpoint_tolerance_m
                    )
                    if right_source is not None
                    else internal_lookup[row.group_coordinates[right_group]]
                )
            else:
                u_node = _lookup_source_node(
                    lookup, row.source_u, left_coordinate, endpoint_tolerance_m
                )
                v_node = _lookup_source_node(
                    lookup, row.source_v, right_coordinate, endpoint_tolerance_m
                )
            repaired_rows.append(
                {
                    "u": u_node,
                    "v": v_node,
                    "key": f"{row.source_record_id}:component:{component_index}",
                    "one_way": row.one_way,
                    "raw_source_u": row.source_u,
                    "raw_source_v": row.source_v,
                    "raw_source_key": row.source_key,
                    "source_record_id": row.source_record_id,
                    "source_component_index": component_index,
                    "repair_action": action,
                    "travel_time_fraction": float(part.length / total_length),
                    **row.attributes,
                    "geometry": part,
                }
            )
        action_rows.append(
            {
                "source_record_id": row.source_record_id,
                "source_u": row.source_u,
                "source_v": row.source_v,
                "source_key": row.source_key,
                "action": action,
                "component_count": len(row.parts),
                "emitted_component_count": len(parts),
                "reason": "",
            }
        )

    node_lineage.extend(internal_nodes)
    if not repaired_rows:
        raise NetworkRepairError("network repair policy quarantined every selected road record")
    repaired = gpd.GeoDataFrame(repaired_rows, geometry="geometry", crs=roads.crs)
    quarantined = gpd.GeoDataFrame(
        quarantined_rows,
        columns=(
            "source_record_id",
            "source_u",
            "source_v",
            "source_key",
            "issue_code",
            "reason",
            "geometry",
        ),
        geometry="geometry",
        crs=roads.crs,
    )
    action_frame = pd.DataFrame(action_rows).sort_values("source_record_id").reset_index(drop=True)
    lineage_frame = (
        pd.DataFrame(node_lineage)
        .sort_values("derived_node_id", kind="stable")
        .reset_index(drop=True)
    )
    issue_frame = pd.DataFrame(
        issues,
        columns=("issue_code", "source_record_id", "source_node_id", "severity", "message"),
    ).sort_values(["issue_code", "source_record_id", "source_node_id"], kind="stable")
    readiness = "not_ready" if len(quarantined) else "strict_ready"
    summary_values = {
        "algorithm_version": NETWORK_REPAIR_ALGORITHM_VERSION,
        "policy": policy,
        "endpoint_tolerance_m": endpoint_tolerance_m,
        "source_row_count": len(rows),
        "retained_source_row_count": int((action_frame.action == "retained").sum()),
        "merged_source_row_count": int((action_frame.action == "merged_contiguous").sum()),
        "split_source_row_count": int((action_frame.action == "split_branching").sum()),
        "quarantined_source_row_count": len(quarantined),
        "emitted_component_count": len(repaired),
        "split_source_node_count": len(conflict_nodes),
        "derived_node_count": len(lineage_frame),
        "readiness_grade": readiness,
    }
    repair_hash = scientific_hash(
        {
            **summary_values,
            "source_content_hash": source_content_hash,
            "actions": action_frame.to_dict(orient="records"),
            "repaired": [
                [
                    item["source_record_id"],
                    item["source_component_index"],
                    item["u"],
                    item["v"],
                    item["geometry"].wkb_hex,
                ]
                for item in sorted(
                    repaired_rows,
                    key=lambda value: (value["source_record_id"], value["source_component_index"]),
                )
            ],
            "node_lineage": lineage_frame.to_dict(orient="records"),
            "quarantined": [
                [item["source_record_id"], item["issue_code"], item["geometry"].wkb_hex]
                for item in quarantined_rows
            ],
        }
    )
    summary_values["repair_hash"] = repair_hash
    summary = pd.DataFrame([summary_values])
    return NetworkRepairResult(
        repaired_roads=repaired.sort_values(
            ["source_record_id", "source_component_index"], kind="stable"
        ).reset_index(drop=True),
        repair_actions=action_frame,
        node_lineage=lineage_frame,
        quarantined_edges=quarantined.sort_values("source_record_id").reset_index(drop=True),
        repair_issues=issue_frame.reset_index(drop=True),
        repair_summary=summary,
        repair_hash=repair_hash,
        policy=policy,
        readiness_grade=readiness,
    )


@dataclass(frozen=True, slots=True)
class ScenarioImpactResult:
    required_pair_count: int
    reachable_pair_count: int
    unreachable_pairs: tuple[tuple[str, str, str], ...]
    route_edge_ids: tuple[str, ...]
    readiness_grade: Literal["strict_ready", "scenario_ready_with_quarantine", "not_ready"]

    @property
    def passed(self) -> bool:
        return not self.unreachable_pairs


def validate_scenario_impact(
    routing,
    *,
    profile_id: str,
    required_pairs: Sequence[tuple[str, str]],
    quarantined_source_count: int = 0,
) -> ScenarioImpactResult:
    """Require every directed node pair needed by a bounded scenario to be routable."""

    if quarantined_source_count < 0:
        raise ValueError("quarantined_source_count must be nonnegative")
    pairs = tuple(sorted(set(required_pairs)))
    failures = []
    edge_ids = set()
    for source_node, target_node in pairs:
        route = routing.route(profile_id, source_node, target_node)
        if not route.reachable:
            failures.append((source_node, target_node, route.reason or "unreachable"))
        else:
            edge_ids.update(edge.edge_id for edge in route.edges)
    return ScenarioImpactResult(
        required_pair_count=len(pairs),
        reachable_pair_count=len(pairs) - len(failures),
        unreachable_pairs=tuple(failures),
        route_edge_ids=tuple(sorted(edge_ids)),
        readiness_grade=(
            "not_ready"
            if failures
            else ("scenario_ready_with_quarantine" if quarantined_source_count else "strict_ready")
        ),
    )
