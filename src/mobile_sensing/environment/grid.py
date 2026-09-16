"""Deterministic supplied/generated sensing grids and population alignment."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import box

from mobile_sensing.contracts import scientific_hash
from mobile_sensing.contracts.configuration import RegularGridConfig, UploadedGridConfig


GRID_ALGORITHM_VERSION = "metric-grid@1"
POPULATION_ALIGNMENT_VERSION = "population-centroid-or-exact@1"


class GridPreparationError(ValueError):
    """Raised when a grid or feature alignment is scientifically invalid."""


@dataclass(frozen=True, slots=True)
class GridPreparationResult:
    cells: gpd.GeoDataFrame
    grid_axis_hash: str
    source_row_count: int
    selected_row_count: int


@dataclass(frozen=True, slots=True)
class PopulationPreparationResult:
    features: pd.DataFrame
    source_row_count: int
    matched_source_rows: int
    unmatched_source_rows: int
    input_mass: float
    matched_mass: float
    alignment_hash: str
    assumptions: tuple[str, ...]


def _check_metric_crs(crs: Any) -> None:
    parsed = gpd.GeoSeries([], crs=crs).crs
    if parsed is None or not parsed.is_projected:
        raise GridPreparationError("working CRS must be projected")
    units = [axis.unit_name.casefold() for axis in parsed.axis_info]
    factors = [axis.unit_conversion_factor for axis in parsed.axis_info]
    if not units or any(unit not in {"metre", "meter"} for unit in units):
        raise GridPreparationError("working CRS axes must use metres")
    if any(not math.isclose(factor, 1.0, rel_tol=0.0, abs_tol=1e-12) for factor in factors):
        raise GridPreparationError("working CRS axes must have unit conversion factor 1")


def _validate_nonoverlap(cells: gpd.GeoDataFrame, tolerance_m2: float = 1e-8) -> None:
    pairs = cells.sindex.query(cells.geometry, predicate="intersects")
    for left, right in zip(pairs[0], pairs[1]):
        if left >= right:
            continue
        if cells.geometry.iloc[left].intersection(cells.geometry.iloc[right]).area > tolerance_m2:
            raise GridPreparationError(
                f"grid cell interiors overlap: {cells.cell_id.iloc[left]}, "
                f"{cells.cell_id.iloc[right]}"
            )


def prepare_grid(
    definition: RegularGridConfig | UploadedGridConfig,
    boundary,
    *,
    working_crs: str,
    supplied: gpd.GeoDataFrame | None = None,
) -> GridPreparationResult:
    _check_metric_crs(working_crs)
    if definition.kind == "regular":
        source_row_count = 0
        size = float(definition.cell_size_m)
        min_x, min_y, max_x, max_y = boundary.bounds
        i_min = math.floor((min_x - definition.origin_easting_m) / size)
        i_max = math.ceil((max_x - definition.origin_easting_m) / size)
        j_min = math.floor((min_y - definition.origin_northing_m) / size)
        j_max = math.ceil((max_y - definition.origin_northing_m) / size)
        definition_hash = scientific_hash(
            {
                "algorithm": GRID_ALGORITHM_VERSION,
                "working_crs": working_crs,
                "cell_size_m": size,
                "origin_easting_m": float(definition.origin_easting_m),
                "origin_northing_m": float(definition.origin_northing_m),
            }
        )
        records: list[dict[str, Any]] = []
        for i in range(i_min, i_max):
            x = float(definition.origin_easting_m) + i * size
            for j in range(j_min, j_max):
                y = float(definition.origin_northing_m) + j * size
                geometry = box(x, y, x + size, y + size)
                intersection = geometry.intersection(boundary)
                if intersection.is_empty or intersection.area <= 1e-8:
                    continue
                records.append(
                    {
                        "cell_id": f"cell_{definition_hash[:12]}_{i}_{j}",
                        "lattice_i": i,
                        "lattice_j": j,
                        "lattice_easting_m": x,
                        "lattice_northing_m": y,
                        "cell_size_m": size,
                        "included_area_fraction": float(intersection.area / geometry.area),
                        "sensing_geometry": intersection,
                        "geometry": geometry,
                    }
                )
        cells = gpd.GeoDataFrame(records, geometry="geometry", crs=working_crs)
        if cells.empty:
            raise GridPreparationError("generated grid retained no positive-area cells")
        cells["sensing_geometry"] = gpd.GeoSeries(
            cells["sensing_geometry"], index=cells.index, crs=working_crs
        )
        for geometry in cells.geometry:
            min_cell_x, min_cell_y, max_cell_x, max_cell_y = geometry.bounds
            if not (
                math.isclose(max_cell_x - min_cell_x, size, abs_tol=1e-9)
                and math.isclose(max_cell_y - min_cell_y, size, abs_tol=1e-9)
            ):
                raise GridPreparationError("generated grid cells do not have declared dimensions")
    else:
        if supplied is None:
            raise GridPreparationError("uploaded grid definition requires supplied grid data")
        if supplied.empty or supplied.crs is None:
            raise GridPreparationError("supplied grid must be nonempty and declare a CRS")
        source_row_count = len(supplied)
        cells = supplied.to_crs(working_crs).copy()
        if not cells.geometry.geom_type.isin({"Polygon", "MultiPolygon"}).all():
            raise GridPreparationError("supplied grid must contain only polygonal cells")
        if not cells.geometry.is_valid.all() or (cells.geometry.area <= 1e-8).any():
            raise GridPreparationError(
                "supplied grid cells must be valid with positive metric area"
            )
        if "cell_id" in cells:
            if not cells.cell_id.map(lambda value: isinstance(value, str)).all():
                raise GridPreparationError("supplied cell_id values must be strings")
        elif {"easting", "northing"} <= set(cells.columns):
            for field in ("easting", "northing"):
                cells[field] = pd.to_numeric(cells[field], errors="raise")
                if (
                    not np.isfinite(cells[field]).all()
                    or not np.equal(cells[field], np.floor(cells[field])).all()
                ):
                    raise GridPreparationError(
                        "easting/northing used for generated cell IDs must be finite integers"
                    )
            cells["cell_id"] = [
                f"{int(easting)}_{int(northing)}"
                for easting, northing in zip(cells.easting, cells.northing)
            ]
        else:
            raise GridPreparationError(
                "supplied grid requires cell_id or unique easting/northing fields"
            )
        if cells.cell_id.eq("").any() or cells.cell_id.duplicated().any():
            raise GridPreparationError("grid cell IDs must be nonempty and unique")
        intersection = cells.geometry.intersection(boundary)
        keep = (~intersection.is_empty) & (intersection.area > 1e-8)
        cells = cells.loc[keep].copy()
        intersection = intersection.loc[keep]
        if cells.empty:
            raise GridPreparationError("supplied grid retained no positive-area cells")
        cells["included_area_fraction"] = intersection.area / cells.geometry.area
        cells["sensing_geometry"] = gpd.GeoSeries(intersection, index=cells.index, crs=working_crs)
        if {"easting", "northing"} <= set(cells.columns):
            cells["lattice_easting_m"] = pd.to_numeric(cells.easting, errors="raise")
            cells["lattice_northing_m"] = pd.to_numeric(cells.northing, errors="raise")
        else:
            cells["lattice_easting_m"] = np.nan
            cells["lattice_northing_m"] = np.nan
        cells["lattice_i"] = pd.Series([None] * len(cells), index=cells.index, dtype="Int64")
        cells["lattice_j"] = pd.Series([None] * len(cells), index=cells.index, dtype="Int64")
        cells["cell_size_m"] = np.nan
        cells = cells[
            [
                "cell_id",
                "lattice_i",
                "lattice_j",
                "lattice_easting_m",
                "lattice_northing_m",
                "cell_size_m",
                "included_area_fraction",
                "sensing_geometry",
                "geometry",
            ]
        ]
    cells = cells.sort_values("cell_id", kind="stable").reset_index(drop=True)
    _validate_nonoverlap(cells)
    grid_axis_hash = scientific_hash(
        {
            "algorithm": GRID_ALGORITHM_VERSION,
            "working_crs": working_crs,
            "cells": [
                [
                    row.cell_id,
                    row.geometry.wkb_hex,
                    row.sensing_geometry.wkb_hex,
                    float(row.included_area_fraction),
                ]
                for row in cells.itertuples()
            ],
        }
    )
    return GridPreparationResult(
        cells=cells,
        grid_axis_hash=grid_axis_hash,
        source_row_count=source_row_count,
        selected_row_count=len(cells),
    )


def prepare_population(
    raw: pd.DataFrame,
    cells: gpd.GeoDataFrame,
    *,
    year: int,
    missing_policy: str,
    source_cell_size_m: float | None,
) -> PopulationPreparationResult:
    required = {"year", "easting", "northing", "residents"}
    if not required <= set(raw.columns):
        raise GridPreparationError(f"population data lack fields: {sorted(required-set(raw))}")
    source_row_count = len(raw)
    normalized_year = pd.to_numeric(raw.year, errors="raise")
    if (
        not np.isfinite(normalized_year).all()
        or not np.equal(normalized_year, np.floor(normalized_year)).all()
    ):
        raise GridPreparationError("population year must contain finite integers")
    selected = raw.loc[normalized_year == year, ["year", "easting", "northing", "residents"]].copy()
    if selected.empty:
        raise GridPreparationError(f"population year {year} is absent")
    for field in ("easting", "northing", "residents"):
        selected[field] = pd.to_numeric(selected[field], errors="raise")
        if not np.isfinite(selected[field]).all():
            raise GridPreparationError(f"population {field} must be finite")
    if (selected.residents < 0).any():
        raise GridPreparationError("population residents must be nonnegative")
    if selected.duplicated(["easting", "northing"]).any():
        raise GridPreparationError("population coordinates must be unique within a year")
    input_mass = float(selected.residents.sum())

    assignments: pd.DataFrame
    dimensions_match = False
    if source_cell_size_m is not None:
        bounds = cells.geometry.bounds
        dimensions_match = bool(
            np.isclose(bounds.maxx - bounds.minx, source_cell_size_m, atol=1e-8).all()
            and np.isclose(bounds.maxy - bounds.miny, source_cell_size_m, atol=1e-8).all()
        )
    exact_possible = bool(
        cells.lattice_easting_m.notna().all()
        and cells.lattice_northing_m.notna().all()
        and dimensions_match
    )
    if exact_possible:
        assignments = selected.merge(
            cells[["cell_id", "lattice_easting_m", "lattice_northing_m"]],
            left_on=["easting", "northing"],
            right_on=["lattice_easting_m", "lattice_northing_m"],
            how="inner",
            validate="one_to_one",
        )[["cell_id", "residents"]]
        matched_source_rows = len(assignments)
        assumptions = ("population_alignment=exact_lattice_coordinate",)
    else:
        if source_cell_size_m is None:
            raise GridPreparationError(
                "spatial population alignment requires declared source_cell_size_m"
            )
        selected = selected.reset_index(drop=True)
        selected["_source_row_id"] = selected.index
        points = gpd.GeoDataFrame(
            selected,
            geometry=gpd.points_from_xy(
                selected.easting + source_cell_size_m / 2,
                selected.northing + source_cell_size_m / 2,
            ),
            crs=cells.crs,
        )
        joined = gpd.sjoin(
            points,
            cells[["cell_id", "geometry"]],
            how="inner",
            predicate="intersects",
        )
        joined = joined.sort_values(["_source_row_id", "cell_id"], kind="stable").drop_duplicates(
            "_source_row_id", keep="first"
        )
        assignments = joined.groupby("cell_id", as_index=False, sort=True).residents.sum()
        matched_source_rows = joined._source_row_id.nunique()
        assumptions = ("population_alignment=source_cell_centroid_within_target_cell",)
    matched_mass = float(assignments.residents.sum())
    aligned = cells[["cell_id"]].merge(assignments, on="cell_id", how="left", validate="one_to_one")
    aligned["population_observed"] = aligned.residents.notna()
    missing_count = int(aligned.residents.isna().sum())
    if missing_count and missing_policy == "error":
        raise GridPreparationError(f"population is missing for {missing_count} prepared grid cells")
    if missing_policy == "zero":
        aligned["residents"] = aligned.residents.fillna(0.0)
        aligned["uniform_proxy"] = False
    elif missing_policy == "uniform_proxy":
        aligned["uniform_proxy"] = aligned.residents.isna()
        aligned["residents"] = aligned.residents.fillna(1.0)
        assumptions += ("missing_population_uniform_proxy=1_per_cell",)
    elif missing_policy != "error":
        raise GridPreparationError(f"unsupported population missing policy: {missing_policy}")
    else:
        aligned["uniform_proxy"] = False
    aligned["residents"] = aligned.residents.astype(float)
    aligned = aligned.sort_values("cell_id", kind="stable").reset_index(drop=True)
    alignment_hash = scientific_hash(
        {
            "algorithm": POPULATION_ALIGNMENT_VERSION,
            "year": year,
            "missing_policy": missing_policy,
            "values": aligned.to_dict(orient="records"),
            "matched_source_rows": matched_source_rows,
            "input_mass": input_mass,
            "matched_mass": matched_mass,
            "assumptions": assumptions,
        }
    )
    return PopulationPreparationResult(
        features=aligned,
        source_row_count=source_row_count,
        matched_source_rows=matched_source_rows,
        unmatched_source_rows=len(selected) - matched_source_rows,
        input_mass=input_mass,
        matched_mass=matched_mass,
        alignment_hash=alignment_hash,
        assumptions=assumptions,
    )
