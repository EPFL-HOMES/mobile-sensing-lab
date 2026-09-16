from pathlib import Path
import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Point, LineString, box

from mobile_sensing.application.environment_editor import (
    aggregate_geometry,
    aggregate_osm_category,
    build_environment,
    metric_crs,
    parse_osm_speed,
)
from mobile_sensing.application.services import HeadlessApplication
from mobile_sensing.application.studio_models import EnvironmentEditor, FeatureSelection
from mobile_sensing.application.resource_tables import read_table
from mobile_sensing.datasets.inputs import snapshot_input, InputRegistration
from mobile_sensing.environment import PreparedEnvironmentReader
from mobile_sensing.api.workbench_maps import environment_preview
from tests.v2.environment_fixtures import write_analytic_sources, RecordedProgress, X0, Y0


class Cancellation:
    def is_cancelled(self):
        return False

    def raise_if_cancelled(self):
        pass


def register(root, path, role, crs=None):
    return snapshot_input(
        root, InputRegistration(path=str(path), name=role, role=role, source_crs=crs)
    )["input_id"]


@pytest.mark.parametrize("feature_role", ["feature", "population", "weight"])
def test_editor_mixed_crs_speed_population_and_real_map(tmp_path: Path, feature_role: str):
    root = tmp_path / "artifacts"
    sources = write_analytic_sources(tmp_path / "sources")
    boundary = gpd.read_file(sources.resources["analytic_boundary"].path).to_crs(4326)
    path = tmp_path / "boundary.geojson"
    boundary.to_file(path)
    population_path = tmp_path / "population.csv"
    pd.DataFrame(
        {"easting": [X0 + 1, X0 + 1000], "northing": [Y0 + 1, Y0], "residents": [7, 9]}
    ).to_csv(population_path, index=False)
    config = EnvironmentEditor(
        boundary_input=register(root, path, "boundary"),
        network_input=register(root, sources.resources["analytic_roads"].path, "network"),
        working_crs="EPSG:2056",
        speed_kph=7.2,
        grid_size_m=10,
        features=(
            FeatureSelection(
                input_id=register(root, population_path, feature_role, "EPSG:2056"),
                name="population",
            ),
        ),
    )
    progress = RecordedProgress()
    result = build_environment(
        root,
        config,
        application=HeadlessApplication(root),
        cancellation=Cancellation(),
        progress=progress,
    )
    phases = [phase for phase, _, _ in progress.updates]
    assert phases[0] == "environment.validate"
    assert "environment.boundary.local" in phases
    assert "environment.network.local" in phases
    assert "environment.prepare.prepare_network" in phases
    assert "environment.prepare.prepare_grid" in phases
    assert "environment.features.local" in phases
    assert phases[-1] == "environment.complete"
    prepared = PreparedEnvironmentReader(root).read(result.artifact)
    assert prepared.metadata.sensing_boundary_selected_rows == 1
    features = read_table(root, result.features, "grid_features")
    assert features.loc[features.feature == "population", "value"].sum() == 7
    assert len(features.loc[features.feature == "population"]) == len(prepared.grid_cells)
    assert (features.value == 0).any()
    preview = environment_preview(str(root), result.artifact.artifact_id)
    assert preview["roads"]["features"]
    assert preview["boundary"]["crs"] == "EPSG:4326"
    assert preview["cell_count"] == len(prepared.grid_cells)
    from mobile_sensing.api.workbench_maps import feature_preview
    from mobile_sensing.jobs import JobStoreLimits

    feature_map = feature_preview(root, result.features.artifact_id, "population", JobStoreLimits())
    assert feature_map["returned_count"] == len(prepared.grid_cells)
    assert sum(f["properties"]["value"] for f in feature_map["features"]) == 7
    assert feature_map["zero_cell_count"] > 0
    assert feature_map["unit"] == config.features[0].unit
    with pytest.raises(ValueError, match="unavailable"):
        feature_preview(root, result.features.artifact_id, "missing", JobStoreLimits())
    repeat = build_environment(
        root,
        config,
        application=HeadlessApplication(root),
        cancellation=Cancellation(),
        progress=RecordedProgress(),
    )
    assert repeat == result


def test_feature_units_boundary_points_and_clipped_geometry():
    grid = gpd.GeoDataFrame(
        {"cell_id": ["b", "a"]}, geometry=[box(10, 0, 20, 10), box(0, 0, 10, 10)], crs=2056
    )
    source = gpd.GeoDataFrame(
        geometry=[Point(10, 5), LineString([(0, 5), (20, 5)]), box(5, 0, 15, 10)], crs=2056
    )
    tables, audits = aggregate_geometry(grid, source, name="test")
    values = pd.concat(tables)
    assert values.loc[values.feature == "test:count", "value"].tolist() == [1, 0]
    assert values.loc[values.feature == "test:length_m", "value"].sum() == 20
    assert values.loc[values.feature == "test:area_m2", "value"].sum() == 100
    assert {row["unit"] for row in audits} == {"count", "m", "m2"}


def test_osm_categories_publish_one_normalized_indicator():
    grid = gpd.GeoDataFrame(
        {"cell_id": ["b", "a"]},
        geometry=[box(10, 0, 20, 10), box(0, 0, 10, 10)],
        crs=2056,
    )
    mixed = gpd.GeoDataFrame(
        geometry=[Point(2, 2), LineString([(0, 5), (20, 5)]), box(5, 0, 15, 10)],
        crs=2056,
    )
    for category in ("transportation", "commercial", "leisure"):
        values, audit = aggregate_osm_category(grid, mixed, category=category)
        assert values.feature.unique().tolist() == [category]
        assert values.unit.unique().tolist() == ["dimensionless"]
        assert values.value.sum() == pytest.approx(1)
        assert audit["feature"] == category
    polygons = mixed.loc[mixed.geom_type.isin(["Polygon", "MultiPolygon"])]
    for category in ("residential", "industrial"):
        values, _ = aggregate_osm_category(grid, polygons, category=category)
        assert values.feature.unique().tolist() == [category]
        assert values.value.sum() == pytest.approx(1)


def test_osm_feature_batch_merges_tags_and_recovers_exact_categories():
    from mobile_sensing.environment.acquisition import (
        merge_feature_tags,
        select_feature_category,
    )

    tags = merge_feature_tags(("public_services", "commercial", "transportation"))
    assert tags["public_transport"] is True
    assert tags["shop"] is True
    assert tags["amenity"] == [
        "bus_station",
        "clinic",
        "hospital",
        "library",
        "parking",
        "school",
        "townhall",
        "university",
    ]
    source = gpd.GeoDataFrame(
        {
            "element": ["node"] * 5,
            "id": [1, 2, 3, 4, 5],
            "amenity": ["parking", "school", None, None, None],
            "public_transport": [None, None, "platform", None, None],
            "shop": [None, None, None, "bakery", None],
            "landuse": [None, None, None, "retail", "residential"],
        },
        geometry=[Point(index, 0) for index in range(5)],
        crs=4326,
    )
    assert select_feature_category(source, "transportation").id.tolist() == [1, 3]
    assert select_feature_category(source, "public_services").id.tolist() == [2]
    assert select_feature_category(source, "commercial").id.tolist() == [4]
    assert select_feature_category(source, "residential").id.tolist() == [5]


def test_crs_and_osm_speed_semantics():
    boundary = gpd.GeoDataFrame(geometry=[box(6.5, 46.5, 6.8, 46.7)], crs=4326)
    assert metric_crs(boundary, "auto") == "EPSG:32632"
    with pytest.raises(ValueError, match="Web Mercator"):
        metric_crs(boundary, "EPSG:3857")
    with pytest.raises(ValueError, match="explicit suitable"):
        metric_crs(gpd.GeoDataFrame(geometry=[box(-5, 40, 15, 60)], crs=4326), "auto")
    assert parse_osm_speed("36") == 10
    assert parse_osm_speed("30 mph") == pytest.approx(13.4112)
    assert pd.isna(parse_osm_speed("50;70"))


def test_editor_runs_in_durable_worker_and_exposes_result(tmp_path):
    from fastapi.testclient import TestClient
    from mobile_sensing.api import create_app
    from mobile_sensing.jobs import JobStore, LocalCoordinator

    sources = write_analytic_sources(tmp_path / "sources")
    root = tmp_path / "artifacts"
    client = TestClient(create_app(root))
    body = {
        "config": {
            "boundary_input": register(
                root, sources.resources["analytic_boundary"].path, "boundary"
            ),
            "network_input": register(root, sources.resources["analytic_roads"].path, "network"),
            "working_crs": "EPSG:2056",
            "grid_size_m": 10,
            "features": [],
        }
    }
    response = client.post("/api/v1/workbench/environments", json=body)
    assert response.status_code == 202, response.text
    with LocalCoordinator(root, max_workers=1) as coordinator:
        coordinator.run_once()
    job = JobStore(root).get_job(response.json()["job_id"])
    assert job.status == "completed", job.error_message
    result = client.get(f"/api/v1/workbench/environments/{job.resource_id}/result")
    assert result.status_code == 200, result.text
    assert result.json()["feature_names"] == ["uniform"]


def test_osm_cached_acquisition_and_local_contract_equivalence(tmp_path, monkeypatch):
    import osmnx as ox
    import networkx as nx
    from mobile_sensing.environment.acquisition import acquire_osm

    graph = nx.MultiDiGraph(crs="EPSG:4326")
    graph.add_node(1, x=6.6, y=46.5)
    graph.add_node(2, x=6.601, y=46.5)
    graph.add_edge(1, 2, key=0, maxspeed="30")
    calls = []
    monkeypatch.setattr(
        ox, "graph_from_polygon", lambda polygon, **kwargs: calls.append(kwargs) or graph
    )
    polygon = box(6.59, 46.49, 6.61, 46.51)
    first, receipt = acquire_osm(
        tmp_path, operation="network", query="fixture", polygon=polygon, cancellation=Cancellation()
    )
    second, cached = acquire_osm(
        tmp_path, operation="network", query="fixture", polygon=polygon, cancellation=Cancellation()
    )
    assert len(calls) == 1 and receipt == cached
    assert first.one_way.all() and second.crs == first.crs
    assert {"u", "v", "key", "geometry"} <= set(first)


def test_osm_feature_categories_use_one_cached_union_query(tmp_path, monkeypatch):
    import osmnx as ox
    from mobile_sensing.environment.acquisition import acquire_osm

    index = pd.MultiIndex.from_tuples([("node", 1), ("way", 2)], names=["element", "id"])
    frame = gpd.GeoDataFrame(
        {
            "amenity": ["school", None],
            "landuse": [None, "residential"],
        },
        geometry=[Point(6.6, 46.5), box(6.6, 46.5, 6.601, 46.501)],
        index=index,
        crs=4326,
    )
    calls = []

    def features_from_polygon(polygon, tags):
        calls.append(tags)
        ox._overpass._make_overpass_polygon_coord_strs(polygon)
        return frame

    monkeypatch.setattr(ox, "features_from_polygon", features_from_polygon)
    arguments = {
        "operation": "features",
        "query": "fixture",
        "polygon": box(6.59, 46.49, 6.61, 46.51),
        "categories": ("residential", "public_services"),
        "cancellation": Cancellation(),
    }
    first, receipt = acquire_osm(tmp_path, **arguments)
    second, cached = acquire_osm(tmp_path, **arguments)
    assert len(calls) == 1
    assert calls[0] == {
        "amenity": ["clinic", "hospital", "library", "school", "townhall", "university"],
        "landuse": ["residential"],
    }
    assert receipt == cached
    assert receipt["categories"] == ["public_services", "residential"]
    assert receipt["overpass_attempts"][0] == {
        "endpoint": "query planner",
        "status": "planned",
        "tiles": 1,
    }
    assert {"element", "id", "amenity", "landuse", "geometry"} <= set(first)
    assert len(second) == 2


def test_editor_downloads_selected_osm_features_once(tmp_path, monkeypatch):
    import mobile_sensing.application.environment_editor as editor_module

    root = tmp_path / "artifacts"
    sources = write_analytic_sources(tmp_path / "sources")
    boundary = gpd.read_file(sources.resources["analytic_boundary"].path).to_crs(4326)
    point = boundary.geometry.union_all().representative_point()
    combined = gpd.GeoDataFrame(
        {
            "element": ["node", "node"],
            "id": [1, 2],
            "public_transport": ["platform", None],
            "amenity": [None, "school"],
        },
        geometry=[point, point],
        crs=4326,
    )
    calls = []

    def acquire(root, **kwargs):
        calls.append(kwargs)
        return combined, {
            "operation": "features",
            "categories": sorted(kwargs["categories"]),
            "rows": len(combined),
        }

    monkeypatch.setattr(editor_module, "acquire_osm", acquire)
    config = EnvironmentEditor(
        boundary_input=register(root, sources.resources["analytic_boundary"].path, "boundary"),
        network_input=register(root, sources.resources["analytic_roads"].path, "network"),
        working_crs="EPSG:2056",
        grid_size_m=10,
        osm_features=("transportation", "public_services"),
    )
    result = build_environment(
        root,
        config,
        application=HeadlessApplication(root),
        cancellation=Cancellation(),
        progress=RecordedProgress(),
    )
    assert len(calls) == 1
    assert calls[0]["category"] is None
    assert calls[0]["categories"] == ("transportation", "public_services")
    features = read_table(root, result.features, "grid_features")
    assert set(features.feature) == {"uniform", "transportation", "public_services"}
    assert features.loc[features.feature == "transportation", "value"].sum() == pytest.approx(1)
    assert features.loc[features.feature == "public_services", "value"].sum() == pytest.approx(1)


def test_osm_connection_failure_names_remote_endpoint_and_local_alternative(tmp_path, monkeypatch):
    import osmnx as ox
    import requests

    from mobile_sensing.environment.acquisition import OSMConnectionError, acquire_osm

    monkeypatch.setattr(
        ox,
        "graph_from_polygon",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            requests.ConnectionError("connection refused")
        ),
    )
    with pytest.raises(OSMConnectionError) as failure:
        acquire_osm(
            tmp_path,
            operation="network",
            query="fixture",
            polygon=box(6.59, 46.49, 6.61, 46.51),
            cancellation=Cancellation(),
        )
    message = str(failure.value)
    assert "OpenStreetMap road-network download" in message
    assert "https://overpass-api.de/api/interpreter" in message
    assert "register a local road-network file" in message


def test_osm_transport_uses_bounded_fallback_and_reports_server(tmp_path, monkeypatch):
    from collections import OrderedDict

    import networkx as nx
    import osmnx as ox
    import requests

    from mobile_sensing.environment.acquisition import acquire_osm

    graph = nx.MultiDiGraph(crs="EPSG:4326")
    graph.add_node(1, x=6.6, y=46.5)
    graph.add_node(2, x=6.601, y=46.5)
    graph.add_edge(1, 2, key=0, maxspeed="30")
    urls = []

    class Response:
        status_code = 200
        reason = "OK"
        ok = True

        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {"elements": []}

    def post(url, **kwargs):
        urls.append(url)
        if len(urls) == 1:
            raise requests.ConnectionError("first server refused")
        return Response()

    def graph_from_polygon(*args, **kwargs):
        ox._overpass._overpass_request(OrderedDict(data="fixture query"))
        return graph

    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(ox, "graph_from_polygon", graph_from_polygon)
    monkeypatch.setattr(ox._http, "_retrieve_from_cache", lambda url: None)
    monkeypatch.setattr(ox._http, "_save_to_cache", lambda *args: None)
    original_timeout = ox.settings.requests_timeout
    progress = RecordedProgress()
    _, receipt = acquire_osm(
        tmp_path,
        operation="network",
        query="fixture",
        polygon=box(6.59, 46.49, 6.61, 46.51),
        cancellation=Cancellation(),
        progress=progress,
        progress_phase="environment.network.osm",
    )

    assert urls == [
        "https://overpass-api.de/api/interpreter",
        "https://overpass.kumi.systems/api/interpreter",
    ]
    assert [attempt["status"] for attempt in receipt["overpass_attempts"]] == [
        "failed",
        "completed",
    ]
    assert receipt["endpoint"] == "https://overpass.kumi.systems/api/interpreter"
    assert [phase for phase, _, _ in progress.updates] == [
        "environment.network.osm.server_1_of_3.overpass-api_de",
        "environment.network.osm.server_2_of_3.overpass_kumi_systems",
        "environment.network.osm.assemble",
    ]
    assert ox.settings.requests_timeout == original_timeout


def test_road_timeout_subdivides_and_reuses_complete_responses(monkeypatch):
    import osmnx as ox
    import requests
    from mobile_sensing.environment.acquisition import _bounded_overpass_transport

    query = {
        "data": "[out:json];(way[highway](poly:'46.49 6.59 46.49 6.63 46.53 6.63 46.53 6.59 46.49 6.59');>;);out;"
    }
    cache, calls, audit = {}, [], []
    urls = []

    class Response:
        status_code = 200
        reason = "OK"
        ok = True

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "elements": [
                    {"type": "node", "id": 1, "lat": 46.5, "lon": 6.6},
                    {"type": "way", "id": 2, "nodes": [1, 3]},
                ]
            }

    def post(url, **kwargs):
        calls.append(kwargs["data"])
        urls.append(url)
        assert kwargs["timeout"][1] <= 45
        if kwargs["data"] == query:
            response = Response()
            response.status_code = 504
            raise requests.HTTPError("Gateway Timeout", response=response)
        assert "[highway]" in kwargs["data"]["data"] and ">;" in kwargs["data"]["data"]
        return Response()

    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(ox._http, "_retrieve_from_cache", cache.get)
    monkeypatch.setattr(
        ox._http, "_save_to_cache", lambda key, value, ok: cache.__setitem__(key, value)
    )
    original = ox._overpass._overpass_request
    with _bounded_overpass_transport(
        ox,
        cancellation=Cancellation(),
        progress=None,
        progress_phase=None,
        audit=audit,
        adaptive_network=True,
    ):
        result = ox._overpass._overpass_request(query)
        assert len(calls) == 3
        assert "overpass-api.de" in urls[0]
        assert all("overpass.kumi.systems" in url for url in urls[1:])
        assert len(result["elements"]) == 2
        assert ox._overpass._overpass_request(query) == result
        assert len(calls) == 3
    assert ox._overpass._overpass_request is original
    assert any(item["status"] == "subdivided" for item in audit)


def test_feature_timeout_subdivides_every_repeated_polygon_clause(monkeypatch):
    import osmnx as ox
    import requests
    from mobile_sensing.environment.acquisition import _bounded_overpass_transport

    coordinates = "46.49 6.59 46.49 6.63 46.53 6.63 46.53 6.59 46.49 6.59"
    query = {
        "data": (
            f"[out:json];(node[shop](poly:'{coordinates}');"
            f"way[landuse](poly:'{coordinates}'););out;"
        )
    }
    calls, audit = [], []

    class Response:
        status_code = 200
        reason = "OK"
        ok = True
        text = ""

        def raise_for_status(self):
            pass

        def json(self):
            return {"elements": []}

    def post(url, **kwargs):
        calls.append(kwargs["data"])
        if kwargs["data"] == query:
            response = Response()
            response.status_code = 504
            raise requests.HTTPError("Gateway Timeout", response=response)
        assert kwargs["data"]["data"].count("(poly:") == 2
        assert coordinates not in kwargs["data"]["data"]
        return Response()

    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(ox._http, "_retrieve_from_cache", lambda *args: None)
    monkeypatch.setattr(ox._http, "_save_to_cache", lambda *args: None)
    with _bounded_overpass_transport(
        ox,
        cancellation=Cancellation(),
        progress=None,
        progress_phase=None,
        audit=audit,
        adaptive_network=True,
        download_label="Spatial-feature download",
    ):
        assert ox._overpass._overpass_request(query) == {"elements": []}
    assert len(calls) == 3
    assert any(item["status"] == "subdivided" for item in audit)


def test_large_road_region_plans_bounded_initial_tiles(tmp_path, monkeypatch):
    import osmnx as ox
    import networkx as nx
    from mobile_sensing.environment.acquisition import acquire_osm

    graph = nx.MultiDiGraph(crs="EPSG:4326")
    graph.add_node(1, x=116.6, y=23.3)
    graph.add_node(2, x=116.601, y=23.3)
    graph.add_edge(1, 2, key=0)
    original = ox._overpass._make_overpass_polygon_coord_strs
    original_area = ox.settings.max_query_area_size
    original_memory = ox.settings.overpass_memory

    def build(polygon, **kwargs):
        assert kwargs["network_type"] == "drive"
        assert ox.settings.overpass_memory == 64 * 1024 * 1024
        coordinates = ox._overpass._make_overpass_polygon_coord_strs(polygon)
        assert 1 < len(coordinates) <= 32
        return graph

    monkeypatch.setattr(ox, "graph_from_polygon", build)
    _, receipt = acquire_osm(
        tmp_path,
        operation="network",
        query="Shantou extent fixture",
        polygon=box(116.24, 22.83, 117.54, 23.65),
    )
    assert receipt["overpass_attempts"][0]["status"] == "planned"
    assert ox._overpass._make_overpass_polygon_coord_strs is original
    assert ox.settings.max_query_area_size == original_area
    assert ox.settings.overpass_memory == original_memory


def test_road_admission_failure_switches_server_without_subdivision(monkeypatch):
    import osmnx as ox
    import requests
    from mobile_sensing.environment.acquisition import _bounded_overpass_transport

    calls, audit = [], []

    class Response:
        status_code = 200
        reason = "OK"
        ok = True
        text = ""

        def raise_for_status(self):
            pass

        def json(self):
            return {"elements": []}

    def post(url, **kwargs):
        calls.append(url)
        response = Response()
        if len(calls) == 1:
            response.status_code = 504
            response.text = "<p>Dispatcher_Client::request_read_and_idx::timeout. The server is probably too busy</p>"
        return response

    monkeypatch.setattr(requests, "post", post)
    monkeypatch.setattr(ox._http, "_retrieve_from_cache", lambda *a: None)
    monkeypatch.setattr(ox._http, "_save_to_cache", lambda *a: None)
    with _bounded_overpass_transport(
        ox,
        cancellation=None,
        progress=None,
        progress_phase=None,
        audit=audit,
        adaptive_network=True,
    ):
        result = ox._overpass._overpass_request(
            {
                "data": "[out:json];way[highway](poly:'46.49 6.59 46.49 6.63 46.53 6.63 46.53 6.59 46.49 6.59');out;"
            }
        )
    assert result == {"elements": []}
    assert len(calls) == 2
    assert [a["status"] for a in audit] == ["failed", "completed"]
    assert "request_read_and_idx" in audit[0]["error"]
