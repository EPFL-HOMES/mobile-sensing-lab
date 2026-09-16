"""Serializable local catalog configuration for headless CLI use."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from mobile_sensing.contracts import ContractModel
from mobile_sensing.environment import (
    LocalDatasetCatalog,
    LocalDatasetResource,
    LocalRegionSelection,
)


class LocalResourceConfig(ContractModel):
    dataset_id: str
    role: Literal["boundary", "network", "grid", "population"]
    path: str
    source_crs: str
    layer: str | None = None
    attribution: tuple[str, ...] = ("Local project data",)
    population_cell_size_m: float | None = None

    @field_validator("dataset_id", "path", "source_crs")
    @classmethod
    def nonempty(cls, value: str) -> str:
        if value == "":
            raise ValueError("local resource fields must be nonempty")
        return value


class RegionSelectionConfig(ContractModel):
    dataset_id: str
    municipality_names: tuple[str, ...] | None = None


class LocalCatalogConfig(ContractModel):
    resources: tuple[LocalResourceConfig, ...]
    network_dataset_id: str
    feature_dataset_ids: dict[str, str] = Field(default_factory=dict)
    region_selections: dict[str, RegionSelectionConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_resources(self) -> Self:
        ids = [item.dataset_id for item in self.resources]
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("local catalog requires unique resources")
        return self

    def build(self, *, relative_to: str | Path | None = None) -> LocalDatasetCatalog:
        base = Path(relative_to).resolve() if relative_to is not None else None

        def path(value: str) -> Path:
            candidate = Path(value)
            if base is not None and not candidate.is_absolute():
                return (base / candidate).resolve()
            return candidate.resolve()

        return LocalDatasetCatalog(
            (
                LocalDatasetResource(
                    dataset_id=item.dataset_id,
                    role=item.role,
                    path=path(item.path),
                    source_crs=item.source_crs,
                    layer=item.layer,
                    attribution=item.attribution,
                    population_cell_size_m=item.population_cell_size_m,
                )
                for item in self.resources
            ),
            network_dataset_id=self.network_dataset_id,
            feature_dataset_ids=self.feature_dataset_ids,
            region_selections={
                key: LocalRegionSelection(
                    dataset_id=value.dataset_id,
                    municipality_names=value.municipality_names,
                )
                for key, value in self.region_selections.items()
            },
        )
