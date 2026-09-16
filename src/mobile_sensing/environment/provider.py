"""Offline local-file implementation of the geographic provider seam."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping

from mobile_sensing.contracts import (
    EnvironmentProviderRequest,
    RawGeographicBundle,
    RawGeographicResource,
    scientific_hash,
    stable_id,
)
from mobile_sensing.environment.models import LocalDatasetResource, LocalRegionSelection


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class LocalDatasetCatalog:
    resources: Mapping[str, LocalDatasetResource]
    network_dataset_id: str
    feature_dataset_ids: Mapping[str, str]
    region_selections: Mapping[str, LocalRegionSelection]

    def __init__(
        self,
        resources: Iterable[LocalDatasetResource],
        *,
        network_dataset_id: str,
        feature_dataset_ids: Mapping[str, str] | None = None,
        region_selections: Mapping[str, LocalRegionSelection] | None = None,
    ) -> None:
        resource_tuple = tuple(resources)
        indexed = {item.dataset_id: item for item in resource_tuple}
        if len(indexed) != len(resource_tuple):
            raise ValueError("local dataset IDs must be unique")
        if network_dataset_id not in indexed or indexed[network_dataset_id].role != "network":
            raise ValueError("network_dataset_id must identify a network resource")
        features = dict(feature_dataset_ids or {})
        for role, dataset_id in features.items():
            if role not in {"grid", "population"}:
                raise ValueError(f"unsupported local feature role: {role}")
            if dataset_id not in indexed or indexed[dataset_id].role != role:
                raise ValueError(f"feature role {role} does not match resource {dataset_id}")
        selections = dict(region_selections or {})
        for selection in selections.values():
            if selection.dataset_id not in indexed:
                raise ValueError("region selection references an unknown dataset")
            if indexed[selection.dataset_id].role != "boundary":
                raise ValueError("region selection must reference a boundary resource")
        object.__setattr__(self, "resources", MappingProxyType(indexed))
        object.__setattr__(self, "network_dataset_id", network_dataset_id)
        object.__setattr__(self, "feature_dataset_ids", MappingProxyType(features))
        object.__setattr__(self, "region_selections", MappingProxyType(selections))

    def resource(self, dataset_id: str, *, role: str | None = None) -> LocalDatasetResource:
        try:
            resource = self.resources[dataset_id]
        except KeyError as exc:
            raise KeyError(f"unknown local dataset: {dataset_id}") from exc
        if role is not None and resource.role != role:
            raise ValueError(f"dataset {dataset_id} has role {resource.role}, expected {role}")
        if not resource.path.is_file():
            raise FileNotFoundError(f"local dataset does not exist: {dataset_id}")
        return resource


class LocalFileEnvironmentProvider:
    """Resolve registered local files without any network access."""

    capability_key = "environment.local_files@1"
    implementation_version = "local-files@1"

    def __init__(self, catalog: LocalDatasetCatalog) -> None:
        self.catalog = catalog

    def acquire(self, request, *, cancellation, progress) -> RawGeographicBundle:
        request = EnvironmentProviderRequest.model_validate(request)
        if request.provider != self.capability_key:
            raise ValueError(f"unsupported provider: {request.provider}")
        if cancellation.is_cancelled():
            raise RuntimeError("environment acquisition cancelled")

        boundary = self.catalog.resource(request.boundary.dataset_id, role="boundary")
        network = self.catalog.resource(self.catalog.network_dataset_id, role="network")
        requested: list[LocalDatasetResource] = []
        for role in request.requested_features:
            try:
                dataset_id = self.catalog.feature_dataset_ids[role]
            except KeyError as exc:
                raise KeyError(f"local provider has no configured feature: {role}") from exc
            requested.append(self.catalog.resource(dataset_id, role=role))

        resources = [boundary, network, *sorted(requested, key=lambda item: item.role)]
        progress.update(phase="hash_local_resources", completed=0, total=len(resources))
        hashes: dict[str, str] = {}
        for index, resource in enumerate(resources, start=1):
            if cancellation.is_cancelled():
                raise RuntimeError("environment acquisition cancelled")
            hashes[resource.dataset_id] = file_sha256(resource.path)
            progress.update(phase="hash_local_resources", completed=index, total=len(resources))

        query_hash = scientific_hash(request)
        bundle_identity = {
            "provider": self.capability_key,
            "provider_version": self.implementation_version,
            "query_hash": query_hash,
            "resources": [
                [resource.role, resource.dataset_id, hashes[resource.dataset_id]]
                for resource in resources
            ],
        }

        def raw(resource: LocalDatasetResource) -> RawGeographicResource:
            return RawGeographicResource(
                role=resource.role,
                dataset_id=resource.dataset_id,
                content_hash=hashes[resource.dataset_id],
                source_crs=resource.source_crs,
            )

        attribution = tuple(
            sorted({item for resource in resources for item in resource.attribution})
        )
        return RawGeographicBundle(
            schema_version=request.schema_version,
            bundle_id=stable_id("raw_bundle", bundle_identity),
            provider=self.capability_key,
            provider_version=self.implementation_version,
            query_hash=query_hash,
            boundary=raw(boundary),
            network=raw(network),
            features=tuple(raw(item) for item in sorted(requested, key=lambda item: item.role)),
            retrieved_at_utc=datetime.now(timezone.utc),
            attribution=attribution,
            source_coverage_limits=(
                "Coverage is limited to the explicitly registered local files.",
            ),
        )
