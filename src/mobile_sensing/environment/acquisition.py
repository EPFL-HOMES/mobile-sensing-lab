"""Cached OSM acquisition. Downloads never occur during simulation or result viewing."""

import json
import re
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import geopandas as gpd
import pandas as pd
from shapely.geometry import mapping, Polygon, box

from mobile_sensing.contracts import scientific_hash
from mobile_sensing.environment.provider import file_sha256

FEATURE_TAGS = {
    "transportation": {
        "public_transport": True,
        "railway": ["station", "tram_stop"],
        "amenity": ["bus_station", "parking"],
    },
    "residential": {"landuse": "residential"},
    "commercial": {"landuse": ["commercial", "retail"], "shop": True},
    "industrial": {"landuse": "industrial"},
    "public_services": {
        "amenity": ["school", "university", "hospital", "clinic", "townhall", "library"]
    },
    "leisure": {"leisure": ["park", "playground", "pitch", "sports_centre"]},
}

# These mirrors expose the same Overpass API and therefore do not change the
# requested OSM dataset. The order is fixed so retries remain reproducible.
OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api",
    "https://overpass.kumi.systems/api",
    "https://overpass.private.coffee/api",
)
OVERPASS_QUERY_TIMEOUT_S = 90
OVERPASS_CONNECT_TIMEOUT_S = 5
OVERPASS_READ_TIMEOUT_S = 100
NETWORK_TILE_AREA_M2 = 25_000_000
NETWORK_TARGET_TILES = 4
NETWORK_REQUEST_LIMIT = 128
NETWORK_DOWNLOAD_BUDGET_S = 600


class OSMConnectionError(RuntimeError):
    """A remote OSM provider could not be reached through the current network path."""


def _endpoint_alias(endpoint: str) -> str:
    return urlparse(endpoint).hostname.replace(".", "_")


def merge_feature_tags(categories):
    """Return one deterministic OSMnx OR-filter for selected feature categories."""
    selected = tuple(sorted(set(categories)))
    if not selected:
        raise ValueError("Select at least one OSM feature category")
    unknown = sorted(set(selected) - set(FEATURE_TAGS))
    if unknown:
        raise ValueError(f"Unsupported OSM feature categories: {', '.join(unknown)}")
    if len(selected) == 1:
        return dict(FEATURE_TAGS[selected[0]])
    merged = {}
    for key in sorted({key for category in selected for key in FEATURE_TAGS[category]}):
        values = [
            FEATURE_TAGS[category][key] for category in selected if key in FEATURE_TAGS[category]
        ]
        if any(value is True for value in values):
            merged[key] = True
            continue
        merged[key] = sorted(
            {item for value in values for item in ([value] if isinstance(value, str) else value)}
        )
    return merged


def select_feature_category(source, category):
    """Recover one exact category from a combined OSM feature response."""
    try:
        tags = FEATURE_TAGS[category]
    except KeyError as exc:
        raise ValueError(f"Unsupported OSM feature category: {category}") from exc
    selected = pd.Series(False, index=source.index)
    for key, expected in tags.items():
        if key not in source:
            continue
        values = source[key]
        if expected is True:
            matches = values.notna()
        else:
            accepted = {expected} if isinstance(expected, str) else set(expected)
            matches = values.map(
                lambda value: (
                    any(item in accepted for item in value)
                    if isinstance(value, (list, tuple, set))
                    else value in accepted if pd.notna(value) else False
                )
            )
        selected |= matches
    return source.loc[selected].copy()


@contextmanager
def _bounded_overpass_transport(
    ox,
    *,
    cancellation,
    progress,
    progress_phase,
    audit,
    adaptive_network=False,
    download_label="OSM download",
):
    """Replace OSMnx's unbounded pause/retry transport for one acquisition."""
    import requests

    original_request = ox._overpass._overpass_request

    started_download = time.monotonic()
    request_count = 0
    endpoint_failures = {base: 0 for base in OVERPASS_ENDPOINTS}
    tile_count, tile_index = 0, 0
    original_polygons = ox._overpass._make_overpass_polygon_coord_strs

    def polygon_queries(polygon):
        nonlocal tile_count
        coordinates = original_polygons(polygon)
        tile_count = len(coordinates)
        if adaptive_network:
            audit.append({"endpoint": "query planner", "status": "planned", "tiles": tile_count})
        return coordinates

    def split_queries(data):
        query = data.get("data", "")
        matches = list(re.finditer(r"\(poly:([\"'])(.*?)\1\)", query))
        if not matches or len({match.group(2) for match in matches}) != 1:
            return []
        coords = [float(v) for v in matches[0].group(2).split()]
        polygon = Polygon(list(zip(coords[1::2], coords[::2])))
        metric, _ = ox.projection.project_geometry(polygon)
        if metric.area < 250_000:
            return []
        x0, y0, x1, y1 = polygon.bounds
        if x1 - x0 >= y1 - y0:
            rectangles = [box(x0, y0, (x0 + x1) / 2, y1), box((x0 + x1) / 2, y0, x1, y1)]
        else:
            rectangles = [box(x0, y0, x1, (y0 + y1) / 2), box(x0, (y0 + y1) / 2, x1, y1)]
        children = []
        for rectangle in rectangles:
            clipped = polygon.intersection(rectangle)
            parts = [clipped] if clipped.geom_type == "Polygon" else list(clipped.geoms)
            for part in parts:
                if part.is_empty or part.area <= 0:
                    continue
                coordinates = " ".join(f"{y:.12f} {x:.12f}" for x, y in part.exterior.coords)
                child_query = query
                for match in reversed(matches):
                    child_query = (
                        child_query[: match.start()]
                        + f"(poly:'{coordinates}')"
                        + child_query[match.end() :]
                    )
                children.append(
                    {
                        **data,
                        "data": child_query,
                    }
                )
        return children

    def request(data, depth=0):
        nonlocal request_count, tile_index
        if depth == 0:
            tile_index += 1
        request_phase = progress_phase
        if adaptive_network and tile_count and progress_phase:
            request_phase = f"{progress_phase}.tile_{tile_index}_of_{tile_count}"
            if progress:
                progress.update(phase=request_phase, completed=tile_index - 1, total=tile_count)
        if cancellation:
            cancellation.raise_if_cancelled()
        canonical_url = OVERPASS_ENDPOINTS[0].rstrip("/") + "/interpreter"
        prepared_url = str(requests.Request("GET", canonical_url, params=data).prepare().url)
        cached = ox._http._retrieve_from_cache(prepared_url)
        if isinstance(cached, dict):
            audit.append({"endpoint": "OSMnx HTTP cache", "status": "cached"})
            return cached

        failures = []
        for attempt, base in enumerate(
            sorted(OVERPASS_ENDPOINTS, key=lambda base: endpoint_failures[base]), start=1
        ):
            if cancellation:
                cancellation.raise_if_cancelled()
            endpoint = base.rstrip("/") + "/interpreter"
            if progress and progress_phase:
                progress.update(
                    phase=(
                        f"{request_phase}.server_{attempt}_of_{len(OVERPASS_ENDPOINTS)}."
                        f"{_endpoint_alias(base)}"
                    ),
                    completed=attempt - 1,
                    total=len(OVERPASS_ENDPOINTS),
                )
            remaining = NETWORK_DOWNLOAD_BUDGET_S - (time.monotonic() - started_download)
            if adaptive_network and (remaining <= 0 or request_count >= NETWORK_REQUEST_LIMIT):
                raise requests.ConnectionError(
                    f"{download_label} reached its 600-second/128-request limit; "
                    "completed tiles are cached for retry"
                )
            request_count += 1
            started = time.monotonic()
            try:
                response = requests.post(
                    endpoint,
                    data=data,
                    timeout=(
                        OVERPASS_CONNECT_TIMEOUT_S,
                        min(45, max(1, remaining)) if adaptive_network else OVERPASS_READ_TIMEOUT_S,
                    ),
                    headers=ox._http._get_http_headers(),
                    **ox.settings.requests_kwargs,
                )
                if response.status_code in {406, 408, 425, 429, 500, 502, 503, 504}:
                    raise requests.HTTPError(
                        f"HTTP {response.status_code} {response.reason}: "
                        f"{re.sub(r'<[^>]+>', ' ', getattr(response, 'text', ''))[-1200:]}",
                        response=response,
                    )
                response.raise_for_status()
                response_json = response.json()
                if not isinstance(response_json, dict):
                    raise ValueError("Overpass response was not a JSON object")
                if response_json.get("remark"):
                    raise requests.HTTPError(
                        f"Overpass runtime error: {str(response_json['remark'])[:240]}",
                        response=response,
                    )
            except (requests.RequestException, ValueError) as exc:
                elapsed = round(time.monotonic() - started, 3)
                failures.append(f"{endpoint}: {type(exc).__name__}: {exc}")
                endpoint_failures[base] += 1
                audit.append(
                    {
                        "endpoint": endpoint,
                        "status": "failed",
                        "elapsed_seconds": elapsed,
                        "error": str(exc)[:1000],
                        "query_hash": scientific_hash(data),
                    }
                )
                status = getattr(getattr(exc, "response", None), "status_code", None)
                overloaded = (
                    isinstance(exc, requests.ReadTimeout)
                    or status in {504, 408}
                    or "runtime error" in str(exc).lower()
                )
                admission_refused = "request_read_and_idx" in str(exc)
                if adaptive_network and overloaded and not admission_refused and depth < 3:
                    children = split_queries(data)
                    if children:
                        if progress and progress_phase:
                            progress.update(
                                phase=f"{progress_phase}.subdivide", completed=depth + 1, total=3
                            )
                        audit.append(
                            {
                                "status": "subdivided",
                                "endpoint": endpoint,
                                "depth": depth + 1,
                                "child_count": len(children),
                            }
                        )
                        elements = {}
                        metadata = None
                        for child in children:
                            result = request(child, depth + 1)
                            metadata = result if metadata is None else metadata
                            for element in result["elements"]:
                                elements[(element["type"], element["id"])] = element
                        merged = {**metadata, "elements": list(elements.values())}
                        ox._http._save_to_cache(prepared_url, merged, True)
                        return merged
                continue

            if not isinstance(response_json.get("elements"), list):
                raise requests.ConnectionError("Overpass response lacks a complete elements list")
            elapsed = round(time.monotonic() - started, 3)
            audit.append({"endpoint": endpoint, "status": "completed", "elapsed_seconds": elapsed})
            ox._http._save_to_cache(prepared_url, response_json, response.ok)
            return response_json

        details = "; ".join(failures)
        raise requests.ConnectionError(
            f"all {len(OVERPASS_ENDPOINTS)} configured Overpass servers failed; {details}"
        )

    ox._overpass._overpass_request = request
    if adaptive_network:
        ox._overpass._make_overpass_polygon_coord_strs = polygon_queries
    try:
        yield
    finally:
        ox._overpass._overpass_request = original_request
        ox._overpass._make_overpass_polygon_coord_strs = original_polygons


def acquire_osm(
    root,
    *,
    operation,
    query,
    polygon=None,
    category=None,
    categories=None,
    cancellation=None,
    progress=None,
    progress_phase=None,
):
    """Query a bounded region and publish response bytes plus exact query provenance."""
    import osmnx as ox
    import requests

    if cancellation:
        cancellation.raise_if_cancelled()
    if category is not None and categories is not None:
        raise ValueError("Use category or categories, not both")
    feature_categories = (
        tuple(sorted(set(categories)))
        if categories is not None
        else (category,) if category is not None else ()
    )
    if operation != "features" and feature_categories:
        raise ValueError("OSM feature categories require operation='features'")
    feature_tags = merge_feature_tags(feature_categories) if operation == "features" else None
    endpoint = (
        ox.settings.nominatim_url.rstrip("/")
        if operation == "boundary"
        else OVERPASS_ENDPOINTS[0] + "/interpreter"
    )
    config = {
        "operation": operation,
        "query": query,
        "polygon": mapping(polygon) if polygon is not None else None,
        "category": feature_categories[0] if len(feature_categories) == 1 else None,
        "tags": feature_tags,
        "osmnx": ox.__version__,
    }
    if len(feature_categories) > 1:
        config["categories"] = list(feature_categories)
    config = json.loads(json.dumps(config))
    key = scientific_hash(config)
    cache = Path(root) / "osm_cache"
    cache.mkdir(parents=True, exist_ok=True)
    final = cache / key
    if (final / "receipt.json").exists():
        receipt = json.loads((final / "receipt.json").read_text())
        if file_sha256(final / "data.parquet") != receipt["sha256"]:
            raise ValueError("Cached OSM response failed checksum verification")
        return gpd.read_parquet(final / "data.parquet"), receipt
    settings = {
        "use_cache": ox.settings.use_cache,
        "cache_folder": ox.settings.cache_folder,
        "requests_timeout": ox.settings.requests_timeout,
        "log_console": ox.settings.log_console,
        "max_query_area_size": ox.settings.max_query_area_size,
        "overpass_memory": ox.settings.overpass_memory,
    }
    ox.settings.use_cache = True
    ox.settings.cache_folder = str(cache / "http")
    ox.settings.requests_timeout = 30 if operation == "boundary" else OVERPASS_QUERY_TIMEOUT_S
    ox.settings.log_console = False
    overpass_audit = []
    try:
        if operation == "boundary":
            data = ox.geocode_to_gdf(query)
        elif operation == "network":
            metric_polygon, _ = ox.projection.project_geometry(polygon)
            # OSMnx buffers by another 500 metres and may query the convex hull.
            # Scale the tile area to the full query extent, not a fixed city-size assumption.
            query_area = metric_polygon.buffer(500).convex_hull.area
            ox.settings.max_query_area_size = max(
                NETWORK_TILE_AREA_M2, query_area / NETWORK_TARGET_TILES
            )
            ox.settings.requests_timeout = 30
            ox.settings.overpass_memory = 64 * 1024 * 1024
            with _bounded_overpass_transport(
                ox,
                cancellation=cancellation,
                progress=progress,
                progress_phase=progress_phase,
                audit=overpass_audit,
                adaptive_network=True,
                download_label="Road download",
            ):
                graph = ox.graph_from_polygon(
                    polygon,
                    network_type="drive",
                    simplify=False,
                    retain_all=True,
                    truncate_by_edge=True,
                )
            if progress and progress_phase:
                progress.update(phase=f"{progress_phase}.assemble", completed=1, total=1)
            data = ox.graph_to_gdfs(graph, nodes=False).reset_index()
            # OSMnx already returns directed arcs, including both directions of a two-way road.
            data["one_way"] = True
            data = data[
                [
                    c
                    for c in (
                        "u",
                        "v",
                        "key",
                        "one_way",
                        "maxspeed",
                        "highway",
                        "geometry",
                    )
                    if c in data
                ]
            ].copy()
            for column in ("maxspeed", "highway"):
                if column in data:
                    data[column] = data[column].map(
                        lambda v: (
                            json.dumps(v)
                            if isinstance(v, list)
                            else str(v) if pd.notna(v) else None
                        )
                    )
        elif operation == "features" and feature_categories:
            try:
                ox.settings.requests_timeout = 30
                ox.settings.overpass_memory = 64 * 1024 * 1024
                with _bounded_overpass_transport(
                    ox,
                    cancellation=cancellation,
                    progress=progress,
                    progress_phase=progress_phase,
                    audit=overpass_audit,
                    adaptive_network=True,
                    download_label="Spatial-feature download",
                ):
                    data = ox.features_from_polygon(polygon, feature_tags).reset_index()
                columns = ["element", "id"]
                if len(feature_categories) > 1:
                    columns.extend(feature_tags)
                columns.append("geometry")
                data = data[[c for c in columns if c in data]].copy()
            except ox._errors.InsufficientResponseError:
                data = gpd.GeoDataFrame(geometry=[], crs=4326)
        else:
            raise ValueError("Unsupported OSM acquisition operation")
    except requests.exceptions.RequestException as exc:
        label = {
            "boundary": "boundary lookup",
            "network": "road-network download",
            "features": f"{'/'.join(feature_categories)} feature download",
        }.get(operation, operation)
        alternative = {
            "boundary": "register a local boundary file",
            "network": "register a local road-network file",
            "features": "deselect this OSM feature layer",
        }.get(operation, "use a registered local input")
        raise OSMConnectionError(
            f"OpenStreetMap {label} failed after bounded attempts to the configured Overpass "
            f"servers ({', '.join(base + '/interpreter' for base in OVERPASS_ENDPOINTS)}). "
            "A remote service, internet connection, proxy, or firewall refused, "
            f"timed out, or rejected the request. Retry later or {alternative}. "
            f"Original error: {type(exc).__name__}: {exc}"
        ) from exc
    finally:
        for name, value in settings.items():
            setattr(ox.settings, name, value)
    if cancellation:
        cancellation.raise_if_cancelled()
    with tempfile.TemporaryDirectory(prefix=".osm-", dir=cache) as temporary:
        stage = Path(temporary)
        data.to_parquet(stage / "data.parquet", index=False)
        used_endpoints = list(
            dict.fromkeys(
                attempt["endpoint"]
                for attempt in overpass_audit
                if attempt["status"] in {"completed", "cached"}
            )
        )
        receipt = {
            **config,
            "endpoint": used_endpoints[0] if len(used_endpoints) == 1 else endpoint,
            "endpoints": used_endpoints or [endpoint],
            "overpass_attempts": overpass_audit,
            "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
            "attribution": "© OpenStreetMap contributors, ODbL",
            "rows": len(data),
            "sha256": file_sha256(stage / "data.parquet"),
        }
        (stage / "receipt.json").write_text(json.dumps(receipt) + "\n")
        stage.rename(final)
    return data, receipt
