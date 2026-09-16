"""Allowlisted, bounded projections over checksum-verified immutable artifacts."""

from __future__ import annotations

import base64
import json
import math
import statistics
from collections import OrderedDict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from threading import RLock
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from pyproj import Transformer
from shapely.geometry import box, mapping
from shapely.geometry.base import BaseGeometry
from shapely.ops import substring, transform

from mobile_sensing.artifacts import partition_file, verify_partitioned_artifact
from mobile_sensing.contracts import (
    ArtifactManifest,
    ArtifactRef,
    EnvironmentArtifactRef,
    SensingMatrixQuery,
    VehicleKey,
    scientific_hash,
)
from mobile_sensing.environment import PreparedEnvironmentReader
from mobile_sensing.exposure import ExposureSensingQueryService
from mobile_sensing.jobs import JobStore, JobStoreLimits, Page
from mobile_sensing.portfolio import PortfolioAnalysisArtifactReader

COLLECTIONS = {
    "dataset": ("datasets",),
    "environment": ("environments",),
    "simulation": ("simulations",),
    "exposure": ("exposures",),
    "portfolio": ("portfolios",),
}


class QueryLimitExceeded(ValueError):
    pass


_map_cache = OrderedDict()
_map_cache_lock = RLock()


def _cached_map(key):
    with _map_cache_lock:
        value = _map_cache.pop(key, None)
        if value is not None:
            _map_cache[key] = value
    return json.loads(value) if value is not None else None


def _retain_map(key, value):
    encoded = json.dumps(value, separators=(",", ":")).encode()
    with _map_cache_lock:
        _map_cache[key] = encoded
        while len(_map_cache) > 8 or sum(len(item) for item in _map_cache.values()) > 64 * 1024**2:
            _map_cache.popitem(last=False)
    return value


def _environment_signature(root, reference):
    directory = Path(root) / "environments" / reference.artifact_id
    return tuple(
        (str(path.relative_to(directory)), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        for path in sorted(directory.rglob("*"))
        if path.is_file()
        for stat in (path.stat(),)
    )


@lru_cache(maxsize=2)
def _read_environment_cached(root, reference_json, signature):
    # Immutable artifacts are verified on first access and again after any file
    # metadata change. The bounded cache belongs to the query process only.
    return PreparedEnvironmentReader(root).read(
        EnvironmentArtifactRef.model_validate_json(reference_json)
    )


def query_environment(root, reference):
    return _read_environment_cached(
        str(Path(root).resolve()),
        reference.model_dump_json(),
        _environment_signature(root, reference),
    )


@dataclass(frozen=True)
class LocatedArtifact:
    reference: ArtifactRef
    directory: Path
    manifest: ArtifactManifest
    collection: str


class ArtifactCatalog:
    def __init__(self, artifact_root: str | Path) -> None:
        self.root = Path(artifact_root).resolve()

    def locate(self, artifact_id: str) -> LocatedArtifact:
        requested_id = artifact_id
        metadata_path = self.root / "metadata.sqlite"
        if metadata_path.is_file():
            try:
                record = JobStore(self.root).get_resource(artifact_id)
                artifact = record.metadata.get("artifact")
                if isinstance(artifact, dict):
                    artifact_id = artifact["artifact_id"]
            except KeyError:
                pass
        matches: list[LocatedArtifact] = []
        for kind, collections in COLLECTIONS.items():
            for collection in collections:
                directory = self.root / collection / artifact_id
                manifest_path = directory / "manifest.json"
                if not manifest_path.is_file():
                    continue
                manifest = ArtifactManifest.model_validate_json(manifest_path.read_bytes())
                reference = ArtifactRef(
                    artifact_id=manifest.artifact_id,
                    artifact_kind=manifest.artifact_kind,
                    content_hash=manifest.content_fingerprint,
                )
                if kind == "environment":
                    reference = EnvironmentArtifactRef.model_validate(
                        reference.model_dump(mode="json")
                    )
                    query_environment(self.root, reference)
                else:
                    verify_partitioned_artifact(
                        artifact_root=self.root, collection=collection, reference=reference
                    )
                matches.append(LocatedArtifact(reference, directory, manifest, collection))
        if not matches:
            raise KeyError(requested_id)
        if len(matches) != 1:
            raise ValueError("artifact identifier is ambiguous across managed collections")
        return matches[0]

    def table(self, artifact_id: str, table_name: str) -> tuple[LocatedArtifact, pa.Table]:
        located = self.locate(artifact_id)
        try:
            manifest = next(item for item in located.manifest.tables if item.name == table_name)
        except StopIteration as exc:
            raise KeyError(table_name) from exc
        tables: list[pa.Table] = []
        if located.reference.artifact_kind == "environment":
            if manifest.row_count:
                tables.append(pq.read_table(located.directory / manifest.relative_path))
        else:
            directory = located.directory / manifest.relative_path
            for index, partition in enumerate(manifest.partitions):
                if partition.row_count:
                    tables.append(pq.read_table(partition_file(directory, index)))
        if tables:
            return located, pa.concat_tables(tables)
        return located, pa.table({})


def _decode_cursor(cursor: str | None, fingerprint: str) -> int:
    if cursor is None:
        return 0
    try:
        value = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
        offset = int(value["offset"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid result cursor") from exc
    if offset < 0:
        raise ValueError("invalid result cursor")
    if value.get("fingerprint") != fingerprint:
        raise ValueError("result cursor belongs to a different query")
    return offset


def _cursor(offset: int, fingerprint: str) -> str:
    return base64.urlsafe_b64encode(
        json.dumps({"fingerprint": fingerprint, "offset": offset}, sort_keys=True).encode()
    ).decode()


def query_table(
    catalog: ArtifactCatalog,
    resource_id: str,
    table_name: str,
    *,
    filters: dict[str, str],
    page_size: int,
    cursor: str | None,
    limits: JobStoreLimits,
    time_start_s: float | None = None,
    time_end_s: float | None = None,
) -> Page:
    if page_size < 1 or page_size > limits.max_page_size:
        raise QueryLimitExceeded("page_size exceeds the configured result limit")
    located = catalog.locate(resource_id)
    if located.reference.artifact_kind == "simulation" and table_name in {
        "movements",
        "activity_intervals",
        "task_outcomes",
        "operational_events",
    }:
        clock = (
            located.manifest.scientific_identity.resolved_config.get("simulation", {})
            .get("scenario", {})
            .get("clock", {})
        )
        time_start_s = (
            time_start_s if time_start_s is not None else clock.get("observation_start_s")
        )
        time_end_s = time_end_s if time_end_s is not None else clock.get("end_s")
    paths = _table_paths(located, table_name, filters)
    columns = pq.read_schema(paths[0]).names if paths else []
    unknown = set(filters) - set(columns)
    if unknown:
        raise ValueError("unknown result filters: " + ", ".join(sorted(unknown)))
    if time_start_s is not None and not math.isfinite(time_start_s):
        raise ValueError("time_start_s must be finite")
    if time_end_s is not None and not math.isfinite(time_end_s):
        raise ValueError("time_end_s must be finite")
    if time_start_s is not None and time_end_s is not None and time_start_s >= time_end_s:
        raise ValueError("time_start_s must be less than time_end_s")
    if time_start_s is not None or time_end_s is not None:
        supported = (
            {"start_s", "end_s"} <= set(columns) or "time_s" in columns or "release_s" in columns
        )
        if not supported:
            raise ValueError(f"table {table_name} does not support time filtering")
    fingerprint = scientific_hash(
        {
            "content_hash": located.reference.content_hash,
            "table": table_name,
            "filters": filters,
            "time_start_s": time_start_s,
            "time_end_s": time_end_s,
        }
    )
    offset = _decode_cursor(cursor, fingerprint)
    selected = []
    total = 0
    for path in paths:
        for batch in pq.ParquetFile(path).iter_batches(batch_size=10_000):
            for row in batch.to_pylist():
                if any(str(row[name]) != expected for name, expected in filters.items()):
                    continue
                if {"start_s", "end_s"} <= set(row):
                    if time_start_s is not None and row["end_s"] <= time_start_s:
                        continue
                    if time_end_s is not None and row["start_s"] >= time_end_s:
                        continue
                elif "time_s" in row:
                    if time_start_s is not None and row["time_s"] < time_start_s:
                        continue
                    if time_end_s is not None and row["time_s"] >= time_end_s:
                        continue
                elif "release_s" in row:
                    if time_start_s is not None and row["release_s"] < time_start_s:
                        continue
                    if time_end_s is not None and row["release_s"] >= time_end_s:
                        continue
                if total >= offset and len(selected) < page_size:
                    selected.append(row)
                total += 1
    following = offset + len(selected)
    return Page(
        items=selected,
        returned_count=len(selected),
        total_matching_count=total,
        next_cursor=_cursor(following, fingerprint) if following < total else None,
        is_complete=following >= total,
    )


def _parse_bbox(value: str | None):
    if value is None:
        return None
    try:
        west, south, east, north = (float(item) for item in value.split(","))
    except ValueError as exc:
        raise ValueError("bbox must be west,south,east,north in WGS84") from exc
    if not all(math.isfinite(item) for item in (west, south, east, north)):
        raise ValueError("bbox values must be finite")
    if not west < east or not south < north:
        raise ValueError("bbox bounds are invalid")
    return box(west, south, east, north)


def _bounded_geojson(features: list[dict[str, Any]], *, limits: JobStoreLimits, filters):
    if len(features) > limits.max_map_features:
        raise QueryLimitExceeded(
            "map feature limit exceeded; narrow bbox, time, or vehicle filters"
        )
    response = {
        "type": "FeatureCollection",
        "features": features,
        "returned_count": len(features),
        "total_matching_count": len(features),
        "is_complete": True,
        "crs": "EPSG:4326",
        "aggregation": None,
        "filters": filters,
    }
    if len(json.dumps(response, separators=(",", ":")).encode()) > limits.max_map_bytes:
        raise QueryLimitExceeded("map byte limit exceeded; narrow bbox, time, or vehicle filters")
    return response


def query_map(
    root: Path,
    resource_id: str,
    layer: str,
    *,
    bbox: str | None,
    replication_id: str | None,
    fleet_id: str | None,
    vehicle_id: str | None,
    time_start_s: float | None,
    time_end_s: float | None,
    limits: JobStoreLimits,
    aggregation: str | None = None,
) -> dict[str, Any]:
    """Return bounded WGS84 display geometry without changing scientific geometry."""

    catalog = ArtifactCatalog(root)
    located = catalog.locate(resource_id)
    if aggregation not in {None, "edge_usage", "mean_edge_usage"}:
        raise ValueError("Unsupported movement map aggregation")
    bbox_geometry = _parse_bbox(bbox)
    if time_start_s is not None and not math.isfinite(time_start_s):
        raise ValueError("time_start_s must be finite")
    if time_end_s is not None and not math.isfinite(time_end_s):
        raise ValueError("time_end_s must be finite")
    if time_start_s is not None and time_end_s is not None and time_start_s >= time_end_s:
        raise ValueError("time_start_s must be less than time_end_s")

    cache_key = scientific_hash(
        {
            "root": str(root.resolve()),
            "source": located.reference,
            "environment_files": [
                _environment_signature(
                    root,
                    EnvironmentArtifactRef(
                        artifact_id=dependency.artifact_id,
                        artifact_kind="environment",
                        content_hash=dependency.content_hash,
                    ),
                )
                for dependency in located.manifest.dependencies
                if dependency.role == "environment"
            ],
            "layer": layer,
            "bbox": bbox,
            "replication_id": replication_id,
            "fleet_id": fleet_id,
            "vehicle_id": vehicle_id,
            "time_start_s": time_start_s,
            "time_end_s": time_end_s,
            "aggregation": aggregation,
            "limits": limits.model_dump(mode="json"),
        }
    )
    # locate() has verified the immutable source before a cached view is used.
    cached_view = _cached_map(cache_key)
    if cached_view is not None:
        return cached_view

    if located.reference.artifact_kind == "environment":
        environment_value = query_environment(root, located.reference)
        layers = {
            "boundary": environment_value.boundary,
            "grid": environment_value.grid_cells,
            "roads": environment_value.road_edges,
            "quarantined_roads": environment_value.quarantined_edges,
        }
        if layer not in layers:
            raise KeyError(layer)
        frame = layers[layer].to_crs("EPSG:4326")
        if bbox_geometry is not None:
            frame = frame[frame.geometry.intersects(bbox_geometry)]
        secondary_geometry_columns = [
            name
            for name in frame.columns
            if name != frame.geometry.name
            and any(isinstance(value, BaseGeometry) for value in frame[name])
        ]
        if secondary_geometry_columns:
            frame = frame.drop(columns=secondary_geometry_columns)
        features = json.loads(frame.to_json(drop_id=True, to_wgs84=False))["features"]
        return _retain_map(
            cache_key, _bounded_geojson(features, limits=limits, filters={"bbox": bbox})
        )

    if located.reference.artifact_kind != "simulation" or layer != "movements":
        raise KeyError(layer)
    mean_usage = aggregation == "mean_edge_usage"
    if mean_usage and replication_id is not None:
        raise ValueError("Mean movement maps always use the full replication set")
    if replication_id is None and not mean_usage:
        raise ValueError("simulation movement maps require one replication_id")
    replication_count = 1
    if mean_usage:
        replications = _filtered_rows(located, "replication_status", lambda _: True, limit=10000)
        if not replications or not all(row["complete"] for row in replications):
            raise ValueError("Mean movement maps require complete joint replications")
        replication_count = len(replications)
    clock = (
        located.manifest.scientific_identity.resolved_config.get("simulation", {})
        .get("scenario", {})
        .get("clock", {})
    )
    time_start_s = time_start_s if time_start_s is not None else clock.get("observation_start_s")
    time_end_s = time_end_s if time_end_s is not None else clock.get("end_s")
    dependency = next(
        (item for item in located.manifest.dependencies if item.role == "environment"), None
    )
    if dependency is None:
        raise ValueError("simulation artifact lacks its environment dependency")
    environment_reference = EnvironmentArtifactRef(
        artifact_id=dependency.artifact_id,
        artifact_kind="environment",
        content_hash=dependency.content_hash,
    )
    environment = query_environment(root, environment_reference)
    edge_frame = environment.road_edges
    if edge_frame.crs is None:
        raise ValueError("prepared movement geometry lacks a CRS")
    edge_geometries = dict(zip(edge_frame["edge_id"], edge_frame.geometry, strict=True))
    transformer = Transformer.from_crs(edge_frame.crs, "EPSG:4326", always_xy=True)

    features: list[dict[str, Any]] = []
    usage = {}
    for path in _table_paths(
        located, "movements", {} if mean_usage else {"replication_id": replication_id}
    ):
        for batch in pq.ParquetFile(path).iter_batches(batch_size=10_000):
            for row in batch.to_pylist():
                if not mean_usage and row["replication_id"] != replication_id:
                    continue
                if fleet_id is not None and row["fleet_id"] != fleet_id:
                    continue
                if vehicle_id is not None and row["vehicle_id"] != vehicle_id:
                    continue
                clipped_start = max(
                    row["start_s"], time_start_s if time_start_s is not None else -math.inf
                )
                clipped_end = min(row["end_s"], time_end_s if time_end_s is not None else math.inf)
                if clipped_start >= clipped_end:
                    continue
                geometry = edge_geometries.get(row["edge_id"])
                if geometry is None:
                    raise ValueError(f"movement references unknown edge {row['edge_id']}")
                duration = row["end_s"] - row["start_s"]
                start_ratio = (clipped_start - row["start_s"]) / duration
                end_ratio = (clipped_end - row["start_s"]) / duration
                start_fraction = row["edge_start_fraction"] + start_ratio * (
                    row["edge_end_fraction"] - row["edge_start_fraction"]
                )
                end_fraction = row["edge_start_fraction"] + end_ratio * (
                    row["edge_end_fraction"] - row["edge_start_fraction"]
                )
                if aggregation in {"edge_usage", "mean_edge_usage"}:
                    entry = usage.setdefault(
                        row["edge_id"], {"fractions": [], "duration_s": 0.0, "traversals": 0}
                    )
                    merged_start, merged_end = start_fraction, end_fraction
                    separate = []
                    for left, right in entry["fractions"]:
                        if right < merged_start or left > merged_end:
                            separate.append((left, right))
                        else:
                            merged_start, merged_end = min(left, merged_start), max(
                                right, merged_end
                            )
                    entry["fractions"] = sorted([*separate, (merged_start, merged_end)])
                    entry["duration_s"] += clipped_end - clipped_start
                    entry["traversals"] += 1
                    continue
                segment = substring(geometry, start_fraction, end_fraction, normalized=True)
                wgs84 = transform(transformer.transform, segment)
                if bbox_geometry is not None and not wgs84.intersects(bbox_geometry):
                    continue
                properties = {name: value for name, value in row.items() if name != "geometry"}
                properties["display_start_s"] = clipped_start
                properties["display_end_s"] = clipped_end
                features.append(
                    {"type": "Feature", "geometry": mapping(wgs84), "properties": properties}
                )
                if len(features) > limits.max_map_features:
                    raise QueryLimitExceeded(
                        "map feature limit exceeded; narrow bbox, time, or vehicle filters"
                    )
    if aggregation in {"edge_usage", "mean_edge_usage"}:
        from shapely.geometry import MultiLineString

        for edge_id, entry in sorted(usage.items()):
            merged = []
            for start, end in sorted(entry["fractions"]):
                if merged and start <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
                else:
                    merged.append((start, end))
            segments = [
                substring(edge_geometries[edge_id], start, end, normalized=True)
                for start, end in merged
            ]
            geometry = segments[0] if len(segments) == 1 else MultiLineString(segments)
            wgs84 = transform(transformer.transform, geometry)
            if bbox_geometry is None or wgs84.intersects(bbox_geometry):
                features.append(
                    {
                        "type": "Feature",
                        "geometry": mapping(wgs84),
                        "properties": {
                            "edge_id": edge_id,
                            "duration_s": entry["duration_s"] / replication_count,
                            "traversals": entry["traversals"] / replication_count,
                            "value": entry["duration_s"] / replication_count,
                        },
                    }
                )
    response = _bounded_geojson(
        features,
        limits=limits,
        filters={
            "bbox": bbox,
            "replication_id": replication_id,
            "fleet_id": fleet_id,
            "vehicle_id": vehicle_id,
            "time_start_s": time_start_s,
            "time_end_s": time_end_s,
        },
    )
    response["aggregation"] = aggregation
    response["replications_R"] = replication_count if mean_usage else None
    return _retain_map(cache_key, response)


def query_operation_summary(
    root: Path,
    resource_id: str,
    *,
    replication_id: str,
    fleet_id: str | None,
    vehicle_id: str | None,
    time_start_s: float | None,
    time_end_s: float | None,
) -> dict[str, Any]:
    """Compute bounded-memory operation KPIs with explicit denominators."""

    located = ArtifactCatalog(root).locate(resource_id)
    if located.reference.artifact_kind != "simulation":
        raise ValueError("operation summary requires a simulation artifact")
    clock = (
        located.manifest.scientific_identity.resolved_config.get("simulation", {})
        .get("scenario", {})
        .get("clock", {})
    )
    time_start_s = time_start_s if time_start_s is not None else clock.get("observation_start_s")
    time_end_s = time_end_s if time_end_s is not None else clock.get("end_s")
    if time_start_s is not None and not math.isfinite(time_start_s):
        raise ValueError("time_start_s must be finite")
    if time_end_s is not None and not math.isfinite(time_end_s):
        raise ValueError("time_end_s must be finite")
    if time_start_s is not None and time_end_s is not None and time_start_s >= time_end_s:
        raise ValueError("time_start_s must be less than time_end_s")
    replication_rows = _filtered_rows(
        located,
        "replication_status",
        lambda row: row["replication_id"] == replication_id and row["complete"],
        limit=1,
    )
    if not replication_rows:
        raise KeyError(replication_id)
    catalog_rows = _filtered_rows(
        located,
        "vehicle_catalog",
        lambda row: (fleet_id is None or row["fleet_id"] == fleet_id)
        and (vehicle_id is None or row["vehicle_id"] == vehicle_id),
        limit=2 if vehicle_id is not None else 100_000,
    )
    if (fleet_id is not None or vehicle_id is not None) and not catalog_rows:
        raise KeyError("vehicle filter")
    statuses: dict[str, int] = {}
    released = 0
    assignment_wait_count = 0
    assignment_wait_sum = 0.0
    first_service_wait_count = 0
    first_service_wait_sum = 0.0
    pre_window_task_ids: set[str] = set()
    for path in _table_paths(located, "task_outcomes", {"replication_id": replication_id}):
        for batch in pq.ParquetFile(path).iter_batches(batch_size=10_000):
            for row in batch.to_pylist():
                if row["replication_id"] != replication_id:
                    continue
                if fleet_id is not None and row["fleet_id"] != fleet_id:
                    continue
                if vehicle_id is not None and row["vehicle_id"] != vehicle_id:
                    continue
                if time_start_s is not None and row["release_s"] < time_start_s:
                    pre_window_task_ids.add(row["task_id"])
                    continue
                if time_end_s is not None and row["release_s"] >= time_end_s:
                    continue
                released += 1
                statuses[row["status"]] = statuses.get(row["status"], 0) + 1
                if row["assignment_wait_s"] is not None:
                    assignment_wait_count += 1
                    assignment_wait_sum = math.fsum((assignment_wait_sum, row["assignment_wait_s"]))
                if row["first_service_wait_s"] is not None:
                    first_service_wait_count += 1
                    first_service_wait_sum = math.fsum(
                        (first_service_wait_sum, row["first_service_wait_s"])
                    )
    active_time = 0.0
    busy_time = 0.0
    service_movement_time = 0.0
    operational_movement_time = 0.0
    active_task_ids: set[str] = set()
    for path in _table_paths(located, "activity_intervals", {"replication_id": replication_id}):
        for batch in pq.ParquetFile(path).iter_batches(batch_size=10_000):
            for row in batch.to_pylist():
                if row["replication_id"] != replication_id:
                    continue
                if fleet_id is not None and row["fleet_id"] != fleet_id:
                    continue
                if vehicle_id is not None and row["vehicle_id"] != vehicle_id:
                    continue
                start_s = max(
                    row["start_s"], time_start_s if time_start_s is not None else -math.inf
                )
                end_s = min(row["end_s"], time_end_s if time_end_s is not None else math.inf)
                if start_s >= end_s:
                    continue
                duration = end_s - start_s
                if row["task_id"] is not None:
                    active_task_ids.add(row["task_id"])
                active_time = math.fsum((active_time, duration))
                if row["activity_kind"] != "idle":
                    busy_time = math.fsum((busy_time, duration))
                if row["activity_kind"] == "movement":
                    target = (
                        "operational"
                        if row["movement_kind"] in {"cruise", "depot_return", "reposition"}
                        else "service"
                    )
                    if target == "operational":
                        operational_movement_time = math.fsum((operational_movement_time, duration))
                    else:
                        service_movement_time = math.fsum((service_movement_time, duration))
    return {
        "resource_id": resource_id,
        "replication_id": replication_id,
        "filters": {
            "fleet_id": fleet_id,
            "vehicle_id": vehicle_id,
            "time_start_s": time_start_s,
            "time_end_s": time_end_s,
        },
        "released_task_count": released,
        "carry_in_task_count": len(pre_window_task_ids & active_task_ids),
        "status_counts": statuses,
        "assignment_wait_denominator": assignment_wait_count,
        "mean_assignment_wait_s": (
            assignment_wait_sum / assignment_wait_count if assignment_wait_count else None
        ),
        "first_service_wait_denominator": first_service_wait_count,
        "mean_first_service_wait_s": (
            first_service_wait_sum / first_service_wait_count if first_service_wait_count else None
        ),
        "active_time_s": active_time,
        "busy_time_s": busy_time,
        "active_time_utilization": busy_time / active_time if active_time else None,
        "service_movement_time_s": service_movement_time,
        "operational_movement_time_s": operational_movement_time,
        "complete": True,
    }


def query_portfolio_frontier(
    root: Path,
    analysis_id: str,
    *,
    budget_id: str,
    limits: JobStoreLimits,
) -> dict[str, Any]:
    """Join one budget membership projection to immutable count statistics."""

    catalog = ArtifactCatalog(root)
    located = catalog.locate(analysis_id)
    if located.reference.artifact_kind != "portfolio" or (
        located.manifest.scientific_identity.algorithm_versions.get("storage")
        not in {
            "portfolio-analysis-parquet@1",
            "portfolio-analysis-parquet@2",
            "portfolio-analysis-parquet@3",
        }
    ):
        raise ValueError("frontier query requires a portfolio analysis artifact")
    budgets = _filtered_rows(
        located,
        "budget_levels",
        lambda row: row["budget_id"] == budget_id,
        limit=1,
    )
    if not budgets:
        raise KeyError(budget_id)
    memberships = _filtered_rows(
        located,
        "budget_frontiers",
        lambda row: row["budget_id"] == budget_id,
        limit=limits.max_map_features,
    )
    statistics = {
        row["portfolio_id"]: row
        for row in _filtered_rows(
            located,
            "portfolio_statistics",
            lambda row: True,
            limit=limits.max_map_features,
        )
    }
    saved_config = located.manifest.scientific_identity.resolved_config["portfolio"]
    risk_metric = saved_config.get("risk_metric", "std")
    costs = saved_config["costs"]
    scale = costs["minor_unit_scale"]
    points = []
    for membership in memberships:
        statistic = statistics.get(membership["portfolio_id"])
        if statistic is None:
            raise ValueError("frontier membership lacks portfolio statistics")
        counts = json.loads(statistic["count_by_fleet_json"])
        cost_minor = {
            fleet: count * costs["by_fleet_minor"][fleet] for fleet, count in counts.items()
        }
        if sum(cost_minor.values()) != statistic["total_cost_minor"]:
            raise ValueError("Stored portfolio costs disagree with the source count configuration")
        points.append(
            {
                **membership,
                "count_by_fleet": counts,
                "cost_by_fleet": {fleet: amount / scale for fleet, amount in cost_minor.items()},
                "total_cost": statistic["total_cost_minor"] / scale,
                "utility_mean": statistic["utility_mean"],
                "utility_sample_variance": statistic["utility_sample_variance"],
                "utility_sample_std": statistic["utility_sample_std"],
                "conditional_mean_se": statistic["conditional_mean_se"],
                "utility_min": statistic["utility_min"],
                "utility_max": statistic["utility_max"],
                "utility_p05": statistic["utility_p05"],
                "utility_p50": statistic["utility_p50"],
                "utility_p95": statistic["utility_p95"],
                "sample_count": statistic["sample_count"],
            }
        )
    points.sort(
        key=lambda row: (
            (
                row["utility_p05"]
                if risk_metric == "p05"
                else (
                    row["utility_sample_std"] if row["utility_sample_std"] is not None else math.inf
                )
            ),
            -row["utility_mean"],
            row["portfolio_id"],
        )
    )
    metadata = _filtered_rows(located, "portfolio_analysis_metadata", lambda row: True, limit=1)[0]
    response = {
        "max_mean_utility": max((p["utility_mean"] for p in points), default=None),
        "max_p05_utility": max((p["utility_p05"] for p in points), default=None),
        "frontier_portfolio_count": sum(bool(p["nondominated"]) for p in points),
        "analysis_id": analysis_id,
        "risk_metric": risk_metric,
        "budget": budgets[0],
        "replications_R": metadata["replications_R"],
        "sampling_rounds_J": metadata["sampling_rounds_J"],
        "variability_interpretation": metadata["variability_interpretation"],
        "inference_scope": metadata["inference_scope"],
        "points": points,
        "returned_count": len(points),
        "is_complete": True,
    }
    if len(json.dumps(response, separators=(",", ":")).encode()) > limits.max_map_bytes:
        raise QueryLimitExceeded("frontier response exceeds limits; export the scientific tables")
    return response


def _table_paths(located: LocatedArtifact, table_name: str, filters=None) -> list[Path]:
    try:
        manifest = next(item for item in located.manifest.tables if item.name == table_name)
    except StopIteration as exc:
        raise KeyError(table_name) from exc
    if located.reference.artifact_kind == "environment":
        return [located.directory / manifest.relative_path] if manifest.row_count else []
    return [
        partition_file(located.directory / manifest.relative_path, index)
        for index, partition in enumerate(manifest.partitions)
        if partition.row_count
        and all(
            name not in partition.partition_values
            or str(partition.partition_values[name]) == str(value)
            for name, value in (filters or {}).items()
        )
    ]


def _filtered_rows(
    located: LocatedArtifact,
    table_name: str,
    predicate,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in _table_paths(located, table_name):
        for batch in pq.ParquetFile(path).iter_batches(batch_size=10_000):
            for row in batch.to_pylist():
                if predicate(row):
                    rows.append(row)
                    if len(rows) > limit:
                        raise QueryLimitExceeded(
                            "matrix response exceeds limits; narrow the query or export"
                        )
    return rows


def _summarize_observations(
    observations: dict[str, dict[str, float]],
    *,
    axis_ids: tuple[str, ...],
    statistic: str,
) -> tuple[list[dict[str, Any]], float | None]:
    """Reduce already aggregated cell sums without discarding covariance."""

    observation_ids = tuple(sorted(observations))
    if statistic == "realization":
        rows = [
            {
                "replication_or_round_id": observation_id,
                "time_bin_id": axis_id,
                "value": observations[observation_id].get(axis_id, 0.0),
            }
            for observation_id in observation_ids
            for axis_id in axis_ids
        ]
        overall = (
            math.fsum(observations[observation_ids[0]].values())
            if len(observation_ids) == 1
            else None
        )
        return rows, overall

    reducer = {
        "mean": lambda values: math.fsum(values) / len(values),
        "variance": statistics.variance,
        "std": statistics.stdev,
    }[statistic]
    rows = []
    for axis_id in axis_ids:
        values = [observations[item].get(axis_id, 0.0) for item in observation_ids]
        rows.append({"time_bin_id": axis_id, "value": reducer(values)})
    totals = [math.fsum(observations[item].values()) for item in observation_ids]
    return rows, reducer(totals)


def _exposure_observations(root: Path, located: LocatedArtifact, result: dict[str, Any]):
    service = ExposureSensingQueryService(root)
    reference = located.reference
    replication_ids = tuple(result["replication_ids"])
    vehicle_keys = tuple(VehicleKey.model_validate(item) for item in result["vehicle_keys"])
    selected_cells = frozenset(result["cell_ids"])
    selected_bins = frozenset(result["time_bin_ids"])
    _, chunks = service.reader.read_sparse_chunks(
        reference,
        replication_ids=replication_ids,
        vehicle_keys=vehicle_keys,
    )
    observations = {replication_id: {} for replication_id in replication_ids}
    for chunk in chunks:
        for row in chunk.rows:
            if row.cell_id not in selected_cells or row.time_bin_id not in selected_bins:
                continue
            values = observations[row.replication_id]
            values[row.time_bin_id] = math.fsum((values.get(row.time_bin_id, 0.0), row.duration_s))
    return observations


def _sample_matrix_rows(root, located, matrix_ids, cells, bins, limits):
    if (
        located.manifest.scientific_identity.algorithm_versions.get("storage")
        == "portfolio-samples-reconstruct@2"
    ):
        from mobile_sensing.portfolio.storage import PortfolioArtifactReader
        from mobile_sensing.portfolio.reconstruction import reconstructed_matrix_rows

        return reconstructed_matrix_rows(
            root,
            PortfolioArtifactReader(root, located.reference),
            matrix_ids,
            cells=cells,
            bins=bins,
            max_rows=limits.max_matrix_rows,
        )
    return _filtered_rows(
        located,
        "sample_exposure",
        lambda row: row["matrix_id"] in matrix_ids
        and (not cells or row["cell_id"] in cells)
        and (not bins or row["time_bin_id"] in bins),
        limit=limits.max_matrix_rows,
    )


def _portfolio_observations(
    root,
    sample_located: LocatedArtifact,
    *,
    portfolio_id: str,
    selected_cells: tuple[str, ...],
    selected_bins: tuple[str, ...],
    limits: JobStoreLimits,
):
    samples = _filtered_rows(
        sample_located,
        "portfolio_samples",
        lambda row: row["portfolio_id"] == portfolio_id,
        limit=limits.max_matrix_rows,
    )
    matrix_ids = frozenset(row["matrix_id"] for row in samples)
    selected_cell_set = frozenset(selected_cells)
    selected_bin_set = frozenset(selected_bins)
    by_matrix = {matrix_id: {} for matrix_id in matrix_ids}
    for row in _sample_matrix_rows(
        root, sample_located, matrix_ids, selected_cell_set, selected_bin_set, limits
    ):
        values = by_matrix[row["matrix_id"]]
        values[row["time_bin_id"]] = math.fsum(
            (values.get(row["time_bin_id"], 0.0), row["duration_s"])
        )
    return {str(row["round_id"]): by_matrix[row["matrix_id"]] for row in samples}


def query_matrix(root: Path, request, limits: JobStoreLimits) -> dict[str, Any]:
    if request.temporal_aggregation == "sum":
        from mobile_sensing.api.window_queries import window_matrix

        located = ArtifactCatalog(root).locate(request.resource_id)
        key = scientific_hash(
            {
                "root": str(root.resolve()),
                "kind": "window_matrix@2",
                "source": located.reference,
                "request": request.model_dump(mode="json"),
                "limits": limits.model_dump(mode="json"),
            }
        )
        cached = _cached_map(key)
        return (
            cached if cached is not None else _retain_map(key, window_matrix(root, request, limits))
        )
    catalog = ArtifactCatalog(root)
    located = catalog.locate(request.resource_id)
    selected_cells = request.cell_ids
    selected_bins = request.time_bin_ids
    if request.kind in {"vehicle_exposure", "operational_aggregate"}:
        if located.reference.artifact_kind != "exposure":
            raise ValueError("exposure matrix query requires an exposure artifact")
        statistic = {
            "realization": "raw",
            "mean": "mean",
            "variance": "sample_variance",
            "std": "sample_std",
        }[request.statistic]
        query_service = ExposureSensingQueryService(root, max_nonzero_rows=limits.max_matrix_rows)
        result = query_service.query(
            located.reference,
            SensingMatrixQuery(
                replication_ids=request.replication_ids,
                vehicle_keys=request.vehicle_keys,
                cell_ids=selected_cells,
                time_bin_ids=selected_bins,
                statistic=statistic,
            ),
        ).model_dump(mode="json")
        result["kind"] = request.kind
        result["selected_replication_count"] = result.pop("replication_count")
        result["replications_R"] = len(
            query_service.reader.axes(located.reference)["replication_ids"]
        )
        result["sampling_rounds_J"] = None
        observations = _exposure_observations(root, located, result)
        result["time_summary"], result["overall_value"] = _summarize_observations(
            observations,
            axis_ids=tuple(result["time_bin_ids"]),
            statistic=request.statistic,
        )
    else:
        if located.reference.artifact_kind != "portfolio":
            raise ValueError("portfolio matrix query requires a portfolio artifact")
        storage_version = located.manifest.scientific_identity.algorithm_versions.get("storage")
        if storage_version in {
            "portfolio-analysis-parquet@1",
            "portfolio-analysis-parquet@2",
            "portfolio-analysis-parquet@3",
        }:
            reader = PortfolioAnalysisArtifactReader(root, located.reference)
            metadata = _filtered_rows(
                located, "portfolio_analysis_metadata", lambda row: True, limit=1
            )[0]
            sample_located = catalog.locate(reader.sample_reference.artifact_id)
        elif (
            storage_version in {"portfolio-samples-parquet@1", "portfolio-samples-reconstruct@2"}
            and request.kind == "portfolio_sample"
        ):
            metadata = _filtered_rows(located, "portfolio_metadata", lambda row: True, limit=1)[0]
            sample_located = located
        else:
            raise ValueError("requested portfolio matrix kind is unavailable for this artifact")
        if request.kind == "portfolio_sample":
            matches = _filtered_rows(
                sample_located,
                "portfolio_samples",
                lambda row: row["portfolio_id"] == request.portfolio_id
                and row["round_id"] == request.round_id,
                limit=1,
            )
            if not matches:
                raise KeyError("portfolio sample")
            match = matches[0]
            rows = [
                {
                    "cell_id": row["cell_id"],
                    "time_bin_id": row["time_bin_id"],
                    "value": row["duration_s"],
                }
                for row in _sample_matrix_rows(
                    root,
                    sample_located,
                    {match["matrix_id"]},
                    selected_cells,
                    selected_bins,
                    limits,
                )
            ]
            statistic = "realization"
        else:
            field = {
                "mean": "mean_duration_s",
                "variance": "sample_variance_s2",
                "std": "sample_std_s",
            }[request.statistic]
            if metadata.get("sensing_statistics_mode", "eager") == "on_demand":
                from mobile_sensing.portfolio.reconstruction import reconstructed_statistics
                from mobile_sensing.portfolio.storage import PortfolioArtifactReader

                source_rows = reconstructed_statistics(
                    root,
                    PortfolioArtifactReader(root, sample_located.reference),
                    request.portfolio_id,
                    cells=selected_cells,
                    bins=selected_bins,
                    max_rows=limits.max_matrix_rows,
                )
            else:
                source_rows = _filtered_rows(
                    located,
                    "portfolio_sensing_statistics",
                    lambda row: row["portfolio_id"] == request.portfolio_id
                    and row[field] is not None
                    and row[field] > 0
                    and (not selected_cells or row["cell_id"] in selected_cells)
                    and (not selected_bins or row["time_bin_id"] in selected_bins),
                    limit=limits.max_matrix_rows,
                )
            rows = [
                {"cell_id": row["cell_id"], "time_bin_id": row["time_bin_id"], "value": row[field]}
                for row in source_rows
                if row[field] is not None and row[field] > 0
            ]
            statistic = request.statistic
        exposure_located = catalog.locate(metadata["exposure_id"])
        axes = ExposureSensingQueryService(root).reader.axes(exposure_located.reference)
        cells = tuple(
            cell_id
            for cell_id in axes["cell_ids"]
            if not selected_cells or cell_id in selected_cells
        )
        time_bins = tuple(
            time_bin_id
            for time_bin_id in axes["time_bin_ids"]
            if not selected_bins or time_bin_id in selected_bins
        )
        result = {
            "resource_id": request.resource_id,
            "kind": request.kind,
            "statistic": statistic,
            "grid_axis_hash": located.manifest.scientific_identity.grid_axis_hash,
            "time_axis_hash": located.manifest.scientific_identity.time_axis_hash,
            "cell_ids": cells,
            "time_bin_ids": time_bins,
            "expected_shape": [len(cells), len(time_bins)],
            "values": rows,
            "unit": "s^2" if statistic == "variance" else "s",
            "zero_fill": "absent_sparse_rows_are_zero",
            "complete": True,
            "replications_R": metadata["replications_R"],
            "sampling_rounds_J": metadata["sampling_rounds_J"],
            "portfolio_id": request.portfolio_id,
            "round_id": request.round_id,
        }
        if request.kind == "portfolio_sample":
            observations = {
                str(request.round_id): {
                    time_bin_id: math.fsum(
                        row["value"] for row in rows if row["time_bin_id"] == time_bin_id
                    )
                    for time_bin_id in time_bins
                }
            }
        else:
            observations = _portfolio_observations(
                root,
                sample_located,
                portfolio_id=request.portfolio_id,
                selected_cells=cells,
                selected_bins=time_bins,
                limits=limits,
            )
        result["time_summary"], result["overall_value"] = _summarize_observations(
            observations,
            axis_ids=time_bins,
            statistic=statistic,
        )
    result["summary_semantics"] = "statistic_of_within_observation_cell_sum"
    result["positive_cell_count"] = len({row["cell_id"] for row in result["values"]})
    encoded = json.dumps(result, separators=(",", ":"), allow_nan=False).encode()
    if len(result["values"]) > limits.max_matrix_rows or len(encoded) > limits.max_matrix_bytes:
        raise QueryLimitExceeded("matrix response exceeds limits; narrow the query or export")
    return result
