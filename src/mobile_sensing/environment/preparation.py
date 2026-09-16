"""Offline environment orchestration and immutable artifact publication."""

from __future__ import annotations

import hashlib
import importlib.metadata
import os
import platform
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pyarrow.parquet as pq
from pyproj import CRS

from mobile_sensing.contracts import (
    ArtifactDependency,
    ArtifactManifest,
    EnvironmentArtifactRef,
    EnvironmentBuildConfig,
    PartitionManifest,
    RawGeographicBundle,
    RuntimeProvenance,
    ScientificIdentity,
    TableManifest,
    canonical_json_bytes,
    scientific_hash,
    scientific_projection,
    stable_id,
)
from mobile_sensing.environment.grid import (
    GRID_ALGORITHM_VERSION,
    POPULATION_ALIGNMENT_VERSION,
    PopulationPreparationResult,
    prepare_grid,
    prepare_population,
)
from mobile_sensing.environment.models import (
    EnvironmentPreparationMetadata,
    LocalDatasetResource,
    PreparedEnvironment,
)
from mobile_sensing.environment.network import (
    NETWORK_ALGORITHM_VERSION,
    ROUTING_ALGORITHM_VERSION,
    PreparedRoutingService,
    prepare_network,
)
from mobile_sensing.environment.repair import NETWORK_REPAIR_ALGORITHM_VERSION
from mobile_sensing.environment.provider import LocalDatasetCatalog, file_sha256
from mobile_sensing.environment.snapping import NodeSnapper


PREPARATION_ALGORITHM_VERSION = "local-environment-preparation@2"
EXPECTED_TABLES = (
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


def _read_geographic(resource: LocalDatasetResource) -> gpd.GeoDataFrame:
    frame = (
        gpd.read_parquet(resource.path)
        if resource.path.suffix == ".parquet"
        else gpd.read_file(resource.path, layer=resource.layer)
    )
    if frame.crs is None:
        raise ValueError(f"dataset {resource.dataset_id} does not declare a CRS")
    if frame.crs != CRS.from_user_input(resource.source_crs):
        raise ValueError(
            f"dataset {resource.dataset_id} CRS {frame.crs} does not match registered "
            f"CRS {resource.source_crs}"
        )
    return frame


def _select_boundary(
    resource: LocalDatasetResource,
    *,
    municipality_names: tuple[str, ...] | None,
    working_crs: str,
) -> tuple[gpd.GeoDataFrame, int, int]:
    source = _read_geographic(resource)
    source_rows = len(source)
    if source.empty:
        raise ValueError("boundary dataset is empty")
    if municipality_names is not None:
        if "name" not in source:
            raise ValueError("municipality selection requires a name field")
        available = set(source.name.astype(str))
        missing = sorted(set(municipality_names) - available)
        if missing:
            raise ValueError(f"unknown municipality names: {missing}")
        source = source.loc[source.name.astype(str).isin(municipality_names)].copy()
    selected_rows = len(source)
    geometry = source.to_crs(working_crs).geometry.make_valid().union_all()
    if geometry.is_empty or geometry.area <= 0:
        raise ValueError("selected boundary has no positive metric area")
    return (
        gpd.GeoDataFrame(
            {"boundary_id": ["selected"], "source_row_count": [selected_rows]},
            geometry=[geometry],
            crs=working_crs,
        ),
        source_rows,
        selected_rows,
    )


def _resource_by_role(bundle: RawGeographicBundle, role: str):
    resources = (bundle.boundary, bundle.network, *bundle.features)
    return next((item for item in resources if item.role == role), None)


def _empty_population() -> PopulationPreparationResult:
    features = pd.DataFrame(
        {
            "cell_id": pd.Series(dtype="string"),
            "residents": pd.Series(dtype="float64"),
            "population_observed": pd.Series(dtype="bool"),
            "uniform_proxy": pd.Series(dtype="bool"),
        }
    )
    return PopulationPreparationResult(
        features=features,
        source_row_count=0,
        matched_source_rows=0,
        unmatched_source_rows=0,
        input_mass=0.0,
        matched_mass=0.0,
        alignment_hash=scientific_hash(
            {"algorithm": POPULATION_ALIGNMENT_VERSION, "population": None}
        ),
        assumptions=(),
    )


def _file_manifest(name: str, relative_path: str, frame, directory: Path) -> TableManifest:
    path = directory / relative_path
    frame.to_parquet(path, index=False)
    checksum = file_sha256(path)
    schema_hash = hashlib.sha256(pq.read_schema(path).serialize().to_pybytes()).hexdigest()
    row_count = len(frame)
    return TableManifest(
        name=name,
        relative_path=relative_path,
        schema_hash=schema_hash,
        row_count=row_count,
        partition_axes=(),
        partitions=(
            PartitionManifest(
                partition_values={},
                row_count=row_count,
                file_sha256=checksum,
            ),
        ),
    )


class LocalEnvironmentBuilder:
    """Prepare and atomically publish a complete environment from registered files."""

    def __init__(self, catalog: LocalDatasetCatalog, artifact_root: Path) -> None:
        self.catalog = catalog
        self.artifact_root = Path(artifact_root).resolve()

    def prepare(self, raw_bundle, config, *, cancellation, progress) -> EnvironmentArtifactRef:
        raw_bundle = RawGeographicBundle.model_validate(raw_bundle)
        config = EnvironmentBuildConfig.model_validate(config)
        if config.provider != "environment.local_files@1" or raw_bundle.provider != config.provider:
            raise ValueError("local builder requires environment.local_files@1 inputs")
        if config.boundary.dataset_id != raw_bundle.boundary.dataset_id:
            raise ValueError("build boundary does not match the acquired raw bundle")
        if config.network_source.dataset_id != raw_bundle.network.dataset_id:
            raise ValueError("build network does not match the acquired raw bundle")
        if cancellation.is_cancelled():
            raise RuntimeError("environment preparation cancelled")
        progress.update(phase="load_boundaries", completed=0, total=1)

        boundary_resource = self.catalog.resource(raw_bundle.boundary.dataset_id, role="boundary")
        if file_sha256(boundary_resource.path) != raw_bundle.boundary.content_hash:
            raise ValueError("boundary content changed after local acquisition")
        selected_names = getattr(config.boundary, "municipality_names", None)
        boundary, boundary_source_rows, boundary_selected_rows = _select_boundary(
            boundary_resource,
            municipality_names=selected_names,
            working_crs=config.working_crs,
        )
        if config.routing_extent_ref is None:
            routing_boundary = boundary
            routing_source_rows = boundary_source_rows
            routing_selected_rows = boundary_selected_rows
        else:
            try:
                selection = self.catalog.region_selections[config.routing_extent_ref]
            except KeyError as exc:
                raise KeyError(f"unknown routing extent: {config.routing_extent_ref}") from exc
            routing_resource = self.catalog.resource(selection.dataset_id, role="boundary")
            routing_boundary, routing_source_rows, routing_selected_rows = _select_boundary(
                routing_resource,
                municipality_names=selection.municipality_names,
                working_crs=config.working_crs,
            )
        progress.update(phase="load_boundaries", completed=1, total=1)

        network_resource = self.catalog.resource(raw_bundle.network.dataset_id, role="network")
        if (
            config.network_source.layer is not None
            and config.network_source.layer != network_resource.layer
        ):
            raise ValueError("configured network layer does not match the acquired local resource")
        if file_sha256(network_resource.path) != raw_bundle.network.content_hash:
            raise ValueError("network content changed after local acquisition")
        roads = _read_geographic(network_resource)
        progress.update(phase="prepare_network", completed=0, total=len(roads))
        network = prepare_network(
            roads,
            routing_boundary.geometry.iloc[0],
            working_crs=config.working_crs,
            profiles=config.travel_time_profiles,
            source_content_hash=raw_bundle.network.content_hash,
            endpoint_tolerance_m=config.network_source.endpoint_tolerance_m,
            repair_policy=config.network_source.topology_policy,
        )
        progress.update(
            phase="prepare_network", completed=network.selected_row_count, total=len(roads)
        )

        supplied_grid = None
        if config.grid.kind == "uploaded":
            grid_raw = _resource_by_role(raw_bundle, "grid")
            if grid_raw is None or grid_raw.dataset_id != config.grid.dataset_id:
                raise ValueError("uploaded grid is absent from the raw geographic bundle")
            grid_resource = self.catalog.resource(config.grid.dataset_id, role="grid")
            if file_sha256(grid_resource.path) != grid_raw.content_hash:
                raise ValueError("grid content changed after local acquisition")
            supplied_grid = _read_geographic(grid_resource)
        progress.update(phase="prepare_grid", completed=0, total=1)
        grid = prepare_grid(
            config.grid,
            boundary.geometry.iloc[0],
            working_crs=config.working_crs,
            supplied=supplied_grid,
        )
        progress.update(phase="prepare_grid", completed=1, total=1)

        population = _empty_population()
        if config.population_features is not None:
            population_raw = _resource_by_role(raw_bundle, "population")
            if (
                population_raw is None
                or population_raw.dataset_id != config.population_features.dataset_id
            ):
                raise ValueError("population feature is absent from the raw geographic bundle")
            resource = self.catalog.resource(
                config.population_features.dataset_id, role="population"
            )
            if file_sha256(resource.path) != population_raw.content_hash:
                raise ValueError("population content changed after local acquisition")
            population = prepare_population(
                pd.read_csv(resource.path),
                grid.cells,
                year=config.population_features.year,
                missing_policy=config.population_features.missing_policy,
                source_cell_size_m=resource.population_cell_size_m,
            )

        mobility_hash = scientific_hash(
            {
                "network_hash": network.network_hash,
                "profile_hashes": dict(network.profile_hashes),
                "snapping": config.snapping,
            }
        )
        sensing_hash = scientific_hash(
            {
                "working_crs": config.working_crs,
                "boundary_wkb": boundary.geometry.iloc[0].wkb_hex,
                "grid_axis_hash": grid.grid_axis_hash,
                "population_alignment_hash": population.alignment_hash,
            }
        )
        dependencies = tuple(
            ArtifactDependency(
                role=item.role,
                artifact_id=item.dataset_id,
                content_hash=item.content_hash,
            )
            for item in sorted(
                (raw_bundle.boundary, raw_bundle.network, *raw_bundle.features),
                key=lambda item: item.role,
            )
        )
        resolved_config = {
            "build": scientific_projection(config),
            "raw_query_hash": raw_bundle.query_hash,
            "network_hash": network.network_hash,
            "profile_hashes": dict(network.profile_hashes),
            "network_repair_hash": network.repair.repair_hash,
            "network_repair_policy": network.repair.policy,
            "network_readiness_grade": network.repair.readiness_grade,
            "grid_axis_hash": grid.grid_axis_hash,
            "mobility_hash": mobility_hash,
            "sensing_hash": sensing_hash,
        }
        identity = ScientificIdentity(
            schema_version="2.0",
            artifact_kind="environment",
            resolved_config=resolved_config,
            resolved_config_hash=scientific_hash(resolved_config),
            dependency_hashes={item.role: item.content_hash for item in dependencies},
            algorithm_versions={
                "environment_preparation": PREPARATION_ALGORITHM_VERSION,
                "grid": GRID_ALGORITHM_VERSION,
                "network": NETWORK_ALGORITHM_VERSION,
                "network_repair": NETWORK_REPAIR_ALGORITHM_VERSION,
                "population_alignment": POPULATION_ALIGNMENT_VERSION,
                "routing": ROUTING_ALGORITHM_VERSION,
            },
            grid_axis_hash=grid.grid_axis_hash,
        )
        content_fingerprint = scientific_hash(identity)
        reference = EnvironmentArtifactRef(
            artifact_id=stable_id("environment", content_fingerprint),
            artifact_kind="environment",
            content_hash=content_fingerprint,
        )
        final_directory = self.artifact_root / "environments" / reference.artifact_id
        if final_directory.exists():
            return PreparedEnvironmentReader(self.artifact_root).read(reference).reference

        parent = final_directory.parent
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".environment-staging-", dir=parent
        ) as staging_name:
            staging = Path(staging_name)
            frames = {
                "boundary": ("boundary.parquet", boundary),
                "edge_lineage": ("edge_lineage.parquet", network.edge_lineage),
                "grid_cells": ("grid_cells.parquet", grid.cells),
                "node_lineage": ("node_lineage.parquet", network.repair.node_lineage),
                "nodes": ("nodes.parquet", network.nodes),
                "population_features": (
                    "population_features.parquet",
                    population.features,
                ),
                "quarantined_edges": (
                    "quarantined_edges.parquet",
                    network.repair.quarantined_edges,
                ),
                "repair_actions": ("repair_actions.parquet", network.repair.repair_actions),
                "repair_issues": ("repair_issues.parquet", network.repair.repair_issues),
                "repair_summary": ("repair_summary.parquet", network.repair.repair_summary),
                "repaired_roads": ("repaired_roads.parquet", network.repair.repaired_roads),
                "road_edges": ("road_edges.parquet", network.edges),
                "routing_weights": ("routing_weights.parquet", network.routing_weights),
            }
            progress.update(phase="publish_environment", completed=0, total=len(EXPECTED_TABLES))
            table_values = []
            for index, name in enumerate(EXPECTED_TABLES, start=1):
                table_values.append(_file_manifest(name, *frames[name], staging))
                progress.update(
                    phase="publish_environment", completed=index, total=len(EXPECTED_TABLES)
                )
            tables = tuple(table_values)
            try:
                package_version = importlib.metadata.version("mobile-sensing")
            except importlib.metadata.PackageNotFoundError:
                from mobile_sensing import __version__

                package_version = __version__
            runtime = RuntimeProvenance(
                python_version=platform.python_version(),
                package_version=package_version,
                worker_count=1,
                platform=platform.platform(),
            )
            manifest = ArtifactManifest(
                schema_version="2.0",
                artifact_id=reference.artifact_id,
                artifact_kind="environment",
                content_fingerprint=content_fingerprint,
                scientific_identity=identity,
                dependencies=dependencies,
                expected_table_names=EXPECTED_TABLES,
                tables=tables,
                created_at_utc=datetime.now(timezone.utc),
                runtime=runtime,
            )
            metadata = EnvironmentPreparationMetadata(
                schema_version="2.0",
                environment=reference,
                working_crs=config.working_crs,
                network_hash=network.network_hash,
                mobility_hash=mobility_hash,
                sensing_hash=sensing_hash,
                grid_axis_hash=grid.grid_axis_hash,
                profile_hashes=dict(network.profile_hashes),
                profile_source_counts={
                    key: dict(value) for key, value in network.source_counts.items()
                },
                network_repair_hash=network.repair.repair_hash,
                network_repair_policy=network.repair.policy,
                network_readiness_grade=network.repair.readiness_grade,
                repaired_source_rows=int(
                    (network.repair.repair_actions.action != "quarantined").sum()
                ),
                quarantined_source_rows=len(network.repair.quarantined_edges),
                split_source_node_count=int(
                    network.repair.node_lineage.loc[
                        network.repair.node_lineage.node_kind == "split_conflict",
                        "source_node_id",
                    ].nunique()
                ),
                snapping_max_distance_m=config.snapping.max_distance_m,
                sensing_boundary_source_rows=boundary_source_rows,
                sensing_boundary_selected_rows=boundary_selected_rows,
                routing_boundary_source_rows=routing_source_rows,
                routing_boundary_selected_rows=routing_selected_rows,
                road_source_rows=network.source_row_count,
                road_selected_rows=network.selected_row_count,
                road_node_count=len(network.nodes),
                directed_edge_count=len(network.edges),
                grid_source_rows=grid.source_row_count,
                grid_selected_rows=grid.selected_row_count,
                population_source_rows=population.source_row_count,
                population_matched_rows=population.matched_source_rows,
                population_unmatched_rows=population.unmatched_source_rows,
                population_input_mass=population.input_mass,
                population_matched_mass=population.matched_mass,
                assumptions=population.assumptions,
            )
            (staging / "manifest.json").write_bytes(
                canonical_json_bytes(manifest.model_dump(mode="json"))
            )
            (staging / "environment.json").write_bytes(
                canonical_json_bytes(metadata.model_dump(mode="json"))
            )
            (staging / "_SUCCESS").write_bytes(b"")
            try:
                os.replace(staging, final_directory)
            except FileExistsError:
                pass
        return PreparedEnvironmentReader(self.artifact_root).read(reference).reference


class PreparedEnvironmentReader:
    def __init__(self, artifact_root: Path) -> None:
        self.artifact_root = Path(artifact_root).resolve()

    def read(self, reference: EnvironmentArtifactRef) -> PreparedEnvironment:
        directory = self.artifact_root / "environments" / reference.artifact_id
        if not (directory / "_SUCCESS").is_file():
            raise FileNotFoundError(f"incomplete prepared environment: {reference.artifact_id}")
        manifest = ArtifactManifest.model_validate_json((directory / "manifest.json").read_bytes())
        if (
            manifest.artifact_id != reference.artifact_id
            or manifest.content_fingerprint != reference.content_hash
        ):
            raise ValueError("prepared environment reference does not match its manifest")
        for table in manifest.tables:
            path = directory / table.relative_path
            if not path.is_file() or file_sha256(path) != table.partitions[0].file_sha256:
                raise ValueError(f"prepared environment table checksum failed: {table.name}")
            schema_hash = hashlib.sha256(pq.read_schema(path).serialize().to_pybytes()).hexdigest()
            if schema_hash != table.schema_hash:
                raise ValueError(f"prepared environment table schema failed: {table.name}")
            if pq.read_metadata(path).num_rows != table.row_count:
                raise ValueError(f"prepared environment table row count failed: {table.name}")
        metadata = EnvironmentPreparationMetadata.model_validate_json(
            (directory / "environment.json").read_bytes()
        )
        if metadata.environment != reference:
            raise ValueError("prepared environment metadata reference mismatch")
        resolved = manifest.scientific_identity.resolved_config
        factored = {
            "network_hash": metadata.network_hash,
            "profile_hashes": metadata.profile_hashes,
            "network_repair_hash": metadata.network_repair_hash,
            "network_repair_policy": metadata.network_repair_policy,
            "network_readiness_grade": metadata.network_readiness_grade,
            "grid_axis_hash": metadata.grid_axis_hash,
            "mobility_hash": metadata.mobility_hash,
            "sensing_hash": metadata.sensing_hash,
        }
        if any(resolved[name] != value for name, value in factored.items()):
            raise ValueError("prepared environment metadata does not match scientific identity")
        boundary = gpd.read_parquet(directory / "boundary.parquet")
        edge_lineage = pd.read_parquet(directory / "edge_lineage.parquet")
        grid_cells = gpd.read_parquet(directory / "grid_cells.parquet")
        node_lineage = pd.read_parquet(directory / "node_lineage.parquet")
        nodes = gpd.read_parquet(directory / "nodes.parquet")
        road_edges = gpd.read_parquet(directory / "road_edges.parquet")
        routing_weights = pd.read_parquet(directory / "routing_weights.parquet")
        population_features = pd.read_parquet(directory / "population_features.parquet")
        quarantined_edges = gpd.read_parquet(directory / "quarantined_edges.parquet")
        repair_actions = pd.read_parquet(directory / "repair_actions.parquet")
        repair_issues = pd.read_parquet(directory / "repair_issues.parquet")
        repair_summary = pd.read_parquet(directory / "repair_summary.parquet")
        repaired_roads = gpd.read_parquet(directory / "repaired_roads.parquet")
        table_counts = {
            "boundary": len(boundary),
            "edge_lineage": len(edge_lineage),
            "grid_cells": len(grid_cells),
            "node_lineage": len(node_lineage),
            "nodes": len(nodes),
            "population_features": len(population_features),
            "quarantined_edges": len(quarantined_edges),
            "repair_actions": len(repair_actions),
            "repair_issues": len(repair_issues),
            "repair_summary": len(repair_summary),
            "repaired_roads": len(repaired_roads),
            "road_edges": len(road_edges),
            "routing_weights": len(routing_weights),
        }
        if any(table_counts[table.name] != table.row_count for table in manifest.tables):
            raise ValueError("prepared environment loaded row counts do not match manifest")
        routing = PreparedRoutingService(
            nodes,
            road_edges,
            routing_weights,
            network_hash=metadata.network_hash,
            profile_hashes=metadata.profile_hashes,
        )
        snapping = NodeSnapper(nodes, max_distance_m=metadata.snapping_max_distance_m)
        return PreparedEnvironment(
            reference=reference,
            directory=directory,
            metadata=metadata,
            boundary=boundary,
            grid_cells=grid_cells,
            nodes=nodes,
            road_edges=road_edges,
            routing_weights=routing_weights,
            population_features=population_features,
            repair_actions=repair_actions,
            edge_lineage=edge_lineage,
            node_lineage=node_lineage,
            quarantined_edges=quarantined_edges,
            repair_issues=repair_issues,
            repair_summary=repair_summary,
            repaired_roads=repaired_roads,
            routing=routing,
            snapping=snapping,
        )
