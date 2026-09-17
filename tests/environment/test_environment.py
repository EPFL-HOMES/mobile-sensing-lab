from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import LineString, MultiLineString, box

from mobile_sensing.contracts import scientific_hash
from mobile_sensing.contracts.configuration import (
    ClassSpeedSourceConfig,
    EdgeSpeedSourceConfig,
    EdgeTravelTimeSourceConfig,
    PopulationFeaturesConfig,
    TravelTimeProfileConfig,
    UploadedGridConfig,
)
from mobile_sensing.environment import (
    GridPreparationError,
    LocalDatasetResource,
    LocalDatasetCatalog,
    LocalEnvironmentBuilder,
    LocalFileEnvironmentProvider,
    NetworkPreparationError,
    PreparedEnvironmentReader,
    environment_capability_registry,
    prepare_grid,
    prepare_network,
    prepare_population,
)
from tests.support.environment_fixtures import (
    NoCancellation,
    RecordedProgress,
    X0,
    Y0,
    analytic_roads,
    build_config,
    provider_request,
    write_analytic_sources,
)


def _acquire(catalog):
    return LocalFileEnvironmentProvider(catalog).acquire(
        provider_request(), cancellation=NoCancellation(), progress=RecordedProgress()
    )


def test_local_provider_bundle_and_reserved_osm_are_explicit(tmp_path: Path) -> None:
    catalog = write_analytic_sources(tmp_path / "sources")
    request = provider_request()
    bundle = _acquire(catalog)
    assert bundle.query_hash == scientific_hash(request)
    assert bundle.boundary.dataset_id == "analytic_boundary"
    assert bundle.network.dataset_id == "analytic_roads"
    assert bundle.features == ()
    assert bundle.attribution == ("Local project data",)

    registry = environment_capability_registry()
    assert registry.require("environment.local_files@1").available
    assert registry.require("grid.regular@1").available
    with pytest.raises(LookupError, match="Live OSM acquisition is deferred"):
        registry.require("environment.osm@1")

    missing = LocalDatasetResource(
        "missing_roads", "network", tmp_path / "absent.gpkg", "EPSG:2056"
    )
    with pytest.raises(ValueError, match="network_dataset_id"):
        type(catalog)(
            tuple(catalog.resources.values()) + (missing,), network_dataset_id="analytic_boundary"
        )
    missing_catalog = type(catalog)(
        (catalog.resources["analytic_boundary"], missing),
        network_dataset_id="missing_roads",
    )
    with pytest.raises(FileNotFoundError, match="missing_roads"):
        LocalFileEnvironmentProvider(missing_catalog).acquire(
            request, cancellation=NoCancellation(), progress=RecordedProgress()
        )


def test_directed_parallel_reverse_routing_and_immutable_round_trip(tmp_path: Path) -> None:
    catalog = write_analytic_sources(tmp_path / "sources")
    bundle = _acquire(catalog)
    artifact_root = tmp_path / "artifacts"
    builder = LocalEnvironmentBuilder(catalog, artifact_root)
    reference = builder.prepare(
        bundle,
        build_config(),
        cancellation=NoCancellation(),
        progress=RecordedProgress(),
    )
    environment = PreparedEnvironmentReader(artifact_root).read(reference)
    assert (environment.directory / "_SUCCESS").is_file()
    manifest = json.loads((environment.directory / "manifest.json").read_text())
    assert tuple(table["name"] for table in manifest["tables"]) == (
        "boundary",
        "edge_lineage",
        "grid_cells",
        "node_lineage",
        "nodes",
        "population_features",
        "quarantined_edges",
        "repair_actions",
        "repair_issues",
        "repair_summary",
        "repaired_roads",
        "road_edges",
        "routing_weights",
    )
    assert environment.metadata.network_repair_policy == "strict@1"
    assert environment.metadata.network_readiness_grade == "strict_ready"
    assert environment.quarantined_edges.empty
    assert len(environment.edge_lineage) == len(environment.road_edges)
    assert len(environment.road_edges) == 5
    assert len(environment.road_edges.query("source_u == 'A' and source_v == 'B'")) == 3

    reverse = environment.road_edges.query("is_synthetic_reverse").iloc[0]
    assert tuple(reverse.geometry.coords[0]) == (X0 + 10, Y0)
    assert tuple(reverse.geometry.coords[-1]) == (X0, Y0)

    route = environment.routing.route("constant_2mps", "A", "C")
    assert route.reachable and len(route.edges) == 2
    assert route.total_distance_m == pytest.approx(20.0)
    assert route.total_duration_s == pytest.approx(10.0)
    assert environment.routing.route("constant_2mps", "B", "A").reachable
    assert not environment.routing.route("constant_2mps", "C", "A").reachable
    same = environment.routing.route("constant_2mps", "A", "A")
    assert same.reachable and same.edges == () and same.total_duration_s == 0
    unknown = environment.routing.route("constant_2mps", "unknown", "unknown")
    assert not unknown.reachable and unknown.reason == "unknown_node"
    assert environment.metadata.profile_source_counts == {
        "constant_2mps": {"assumed_constant_speed": 5}
    }
    environment.routing.route("constant_2mps", "A", "C")
    assert environment.routing.cache_info().maxsize == 4096
    assert environment.routing.cache_info().hits >= 1

    assert (
        builder.prepare(
            bundle,
            build_config(),
            cancellation=NoCancellation(),
            progress=RecordedProgress(),
        )
        == reference
    )


def test_factored_hashes_and_snap_tolerance(tmp_path: Path) -> None:
    catalog = write_analytic_sources(tmp_path / "sources")
    bundle = _acquire(catalog)
    builder = LocalEnvironmentBuilder(catalog, tmp_path / "artifacts")
    first_ref = builder.prepare(
        bundle,
        build_config(cell_size_m=10.0),
        cancellation=NoCancellation(),
        progress=RecordedProgress(),
    )
    second_ref = builder.prepare(
        bundle,
        build_config(cell_size_m=5.0),
        cancellation=NoCancellation(),
        progress=RecordedProgress(),
    )
    reader = PreparedEnvironmentReader(tmp_path / "artifacts")
    first, second = reader.read(first_ref), reader.read(second_ref)
    assert first.metadata.mobility_hash == second.metadata.mobility_hash
    assert first.metadata.sensing_hash != second.metadata.sensing_hash

    resolved = first.snapping.resolve(location_id="near_A", x=X0 + 1, y=Y0, source_crs="EPSG:2056")
    assert resolved.node_id == "A" and resolved.snap_distance_m == pytest.approx(1.0)
    tie = first.snapping.resolve(location_id="tie", x=X0 + 5, y=Y0, source_crs="EPSG:2056")
    assert tie.node_id == "A"
    rejected = first.snapping.resolve(location_id="far", x=X0 + 1000, y=Y0, source_crs="EPSG:2056")
    assert rejected.resolution_status.value == "rejected_distance"
    assert rejected.node_id is None and rejected.snap_distance_m is None


def test_population_feature_flows_through_provider_and_artifact(tmp_path: Path) -> None:
    base_catalog = write_analytic_sources(tmp_path / "sources")
    population_path = tmp_path / "sources" / "population.csv"
    pd.DataFrame({"year": [2024], "easting": [X0], "northing": [Y0], "residents": [11]}).to_csv(
        population_path, index=False
    )
    population_resource = LocalDatasetResource(
        "analytic_population",
        "population",
        population_path,
        "EPSG:2056",
        population_cell_size_m=10.0,
    )
    catalog = LocalDatasetCatalog(
        (*base_catalog.resources.values(), population_resource),
        network_dataset_id="analytic_roads",
        feature_dataset_ids={"population": "analytic_population"},
        region_selections=base_catalog.region_selections,
    )
    request = provider_request().model_copy(update={"requested_features": ("population",)})
    bundle = LocalFileEnvironmentProvider(catalog).acquire(
        request, cancellation=NoCancellation(), progress=RecordedProgress()
    )
    config = build_config().model_copy(
        update={
            "population_features": PopulationFeaturesConfig(
                dataset_id="analytic_population", year=2024, missing_policy="zero"
            )
        }
    )
    root = tmp_path / "artifacts"
    reference = LocalEnvironmentBuilder(catalog, root).prepare(
        bundle, config, cancellation=NoCancellation(), progress=RecordedProgress()
    )
    prepared = PreparedEnvironmentReader(root).read(reference)
    assert len(prepared.population_features) == len(prepared.grid_cells)
    assert prepared.population_features.residents.sum() == 11.0
    assert prepared.population_features.population_observed.sum() == 1
    assert prepared.metadata.population_matched_rows == 1
    assert prepared.grid_spatial_index is not None


def test_geometry_and_endpoint_defects_fail_without_fabricated_links() -> None:
    extent = box(X0 - 10, Y0 - 10, X0 + 50, Y0 + 50)
    disconnected = gpd.GeoDataFrame(
        {"u": ["A"], "v": ["B"], "key": ["0"], "one_way": [True]},
        geometry=[MultiLineString([[(X0, Y0), (X0 + 1, Y0)], [(X0 + 5, Y0), (X0 + 6, Y0)]])],
        crs="EPSG:2056",
    )
    profile = TravelTimeProfileConfig.model_validate(
        {
            "profile_id": "p",
            "source": {"kind": "constant_speed", "speed_mps": 1.0},
        }
    )
    with pytest.raises(NetworkPreparationError, match="disconnected MultiLineStrings"):
        prepare_network(
            disconnected,
            extent,
            working_crs="EPSG:2056",
            profiles=(profile,),
            source_content_hash="1" * 64,
        )

    contiguous = disconnected.copy()
    contiguous.geometry = [
        MultiLineString([[(X0, Y0), (X0 + 1, Y0)], [(X0 + 1, Y0), (X0 + 6, Y0)]])
    ]
    normalized = prepare_network(
        contiguous,
        extent,
        working_crs="EPSG:2056",
        profiles=(profile,),
        source_content_hash="1" * 64,
    )
    assert normalized.edges.geometry.iloc[0].geom_type == "LineString"
    assert tuple(normalized.edges.geometry.iloc[0].coords) == (
        (X0, Y0),
        (X0 + 1, Y0),
        (X0 + 6, Y0),
    )

    crossing = gpd.GeoDataFrame(
        {"u": ["outside_left"], "v": ["outside_right"], "key": ["0"], "one_way": [True]},
        geometry=[LineString([(X0 - 5, Y0), (X0 + 15, Y0)])],
        crs="EPSG:2056",
    )
    untrimmed = prepare_network(
        crossing,
        box(X0, Y0 - 1, X0 + 10, Y0 + 1),
        working_crs="EPSG:2056",
        profiles=(profile,),
        source_content_hash="5" * 64,
    )
    assert untrimmed.edges.length_m.iloc[0] == 20.0
    assert tuple(untrimmed.edges.geometry.iloc[0].coords[0]) == (X0 - 5, Y0)
    assert tuple(untrimmed.edges.geometry.iloc[0].coords[-1]) == (X0 + 15, Y0)

    inconsistent = gpd.GeoDataFrame(
        {
            "u": ["A", "B"],
            "v": ["B", "C"],
            "key": ["0", "0"],
            "one_way": [True, True],
        },
        geometry=[
            LineString([(X0, Y0), (X0 + 10, Y0)]),
            LineString([(X0 + 11, Y0), (X0 + 20, Y0)]),
        ],
        crs="EPSG:2056",
    )
    with pytest.raises(NetworkPreparationError, match="inconsistent endpoints"):
        prepare_network(
            inconsistent,
            extent,
            working_crs="EPSG:2056",
            profiles=(profile,),
            source_content_hash="2" * 64,
        )


def test_speed_sources_and_deterministic_route_ties() -> None:
    roads = analytic_roads().iloc[:3].copy()
    profiles = (
        TravelTimeProfileConfig(
            profile_id="direct",
            source=EdgeTravelTimeSourceConfig(kind="edge_travel_time", field="edge_seconds"),
        ),
        TravelTimeProfileConfig(
            profile_id="speed",
            source=EdgeSpeedSourceConfig(
                kind="edge_speed", field="speed_mps", fallback_speed_mps=1.0
            ),
        ),
        TravelTimeProfileConfig(
            profile_id="class",
            source=ClassSpeedSourceConfig(
                kind="road_class_speed",
                class_speed_mps={"local": 2.0, "arterial": 4.0},
                fallback_speed_mps=1.0,
            ),
        ),
    )
    prepared = prepare_network(
        roads,
        box(X0 - 1, Y0 - 1, X0 + 21, Y0 + 2),
        working_crs="EPSG:2056",
        profiles=profiles,
        source_content_hash="3" * 64,
    )
    assert prepared.source_counts["direct"] == {"edge_travel_time:edge_seconds": 4}
    assert prepared.source_counts["speed"] == {
        "edge_speed:speed_mps": 3,
        "explicit_fallback_speed": 1,
    }
    assert prepared.source_counts["class"] == {
        "road_class:arterial": 1,
        "road_class:local": 3,
    }

    tie_roads = gpd.GeoDataFrame(
        {
            "u": ["A", "B", "A", "C"],
            "v": ["B", "D", "C", "D"],
            "key": ["0", "0", "0", "0"],
            "one_way": [True] * 4,
        },
        geometry=[
            LineString([(X0, Y0), (X0 + 10, Y0)]),
            LineString([(X0 + 10, Y0), (X0 + 10, Y0 + 10)]),
            LineString([(X0, Y0), (X0, Y0 + 10)]),
            LineString([(X0, Y0 + 10), (X0 + 10, Y0 + 10)]),
        ],
        crs="EPSG:2056",
    )
    constant = TravelTimeProfileConfig.model_validate(
        {"profile_id": "p", "source": {"kind": "constant_speed", "speed_mps": 1.0}}
    )
    first = prepare_network(
        tie_roads,
        box(X0 - 1, Y0 - 1, X0 + 11, Y0 + 11),
        working_crs="EPSG:2056",
        profiles=(constant,),
        source_content_hash="4" * 64,
    )
    second = prepare_network(
        tie_roads.sample(frac=1, random_state=7),
        box(X0 - 1, Y0 - 1, X0 + 11, Y0 + 11),
        working_crs="EPSG:2056",
        profiles=(constant,),
        source_content_hash="4" * 64,
    )
    assert first.network_hash == second.network_hash
    assert first.profile_hashes == second.profile_hashes
    from mobile_sensing.environment import PreparedRoutingService

    routes = [
        PreparedRoutingService(
            result.nodes,
            result.edges,
            result.routing_weights,
            network_hash=result.network_hash,
            profile_hashes=result.profile_hashes,
        ).route("p", "A", "D")
        for result in (first, second)
    ]
    assert tuple(edge.edge_id for edge in routes[0].edges) == tuple(
        edge.edge_id for edge in routes[1].edges
    )


def test_generated_and_supplied_grid_population_alignment() -> None:
    boundary = box(X0 + 2, Y0 + 2, X0 + 18, Y0 + 8)
    regular = build_config().grid
    generated = prepare_grid(regular, boundary, working_crs="EPSG:2056")
    assert len(generated.cells) == 2
    repeated = prepare_grid(regular, boundary, working_crs="EPSG:2056")
    assert tuple(generated.cells.cell_id) == tuple(repeated.cells.cell_id)
    assert generated.grid_axis_hash == repeated.grid_axis_hash
    assert generated.cells.cell_id.str.match(r"cell_[0-9a-f]{12}_-?\d+_-?\d+").all()
    bounds = generated.cells.geometry.bounds
    assert (bounds.maxx - bounds.minx == 10.0).all()
    assert (bounds.maxy - bounds.miny == 10.0).all()

    supplied = gpd.GeoDataFrame(
        {"easting": [int(X0), int(X0 + 10)], "northing": [int(Y0), int(Y0)]},
        geometry=[box(X0, Y0, X0 + 10, Y0 + 10), box(X0 + 10, Y0, X0 + 20, Y0 + 10)],
        crs="EPSG:2056",
    )
    uploaded = prepare_grid(
        UploadedGridConfig(kind="uploaded", dataset_id="grid"),
        box(X0, Y0, X0 + 20, Y0 + 10),
        working_crs="EPSG:2056",
        supplied=supplied,
    )
    assert tuple(uploaded.cells.cell_id) == (
        "2530000_1150000",
        "2530010_1150000",
    )
    population = pd.DataFrame({"year": [2024], "easting": [X0], "northing": [Y0], "residents": [7]})
    with pytest.raises(GridPreparationError, match="missing for 1"):
        prepare_population(
            population,
            uploaded.cells,
            year=2024,
            missing_policy="error",
            source_cell_size_m=10.0,
        )
    aligned = prepare_population(
        population,
        uploaded.cells,
        year=2024,
        missing_policy="zero",
        source_cell_size_m=10.0,
    )
    assert aligned.matched_source_rows == 1
    assert aligned.features.residents.tolist() == [7.0, 0.0]
    with pytest.raises(GridPreparationError, match="year 2023 is absent"):
        prepare_population(
            population,
            uploaded.cells,
            year=2023,
            missing_policy="zero",
            source_cell_size_m=10.0,
        )

    overlap = supplied.copy()
    overlap.loc[overlap.index[1], "geometry"] = box(X0 + 5, Y0, X0 + 15, Y0 + 10)
    with pytest.raises(GridPreparationError, match="interiors overlap"):
        prepare_grid(
            UploadedGridConfig(kind="uploaded", dataset_id="grid"),
            box(X0, Y0, X0 + 20, Y0 + 10),
            working_crs="EPSG:2056",
            supplied=overlap,
        )
    with pytest.raises(GridPreparationError, match="projected"):
        prepare_grid(regular, boundary, working_crs="EPSG:4326")
