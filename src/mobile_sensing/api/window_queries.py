"""Bounded whole-window maps: sum within observations before statistics."""

import json
import math
from collections import defaultdict
from itertools import groupby
from pathlib import Path
import pyarrow.dataset as ds

from mobile_sensing.artifacts import partition_file
from mobile_sensing.contracts import ArtifactRef
from mobile_sensing.exposure import ExposureArtifactReader
from mobile_sensing.portfolio.storage import PortfolioArtifactReader
from mobile_sensing.portfolio.reconstruction import ReconstructedMatrices


def window_matrix(root, request, limits):
    from mobile_sensing.api.queries import ArtifactCatalog, QueryLimitExceeded

    catalog = ArtifactCatalog(root)
    located = catalog.locate(request.resource_id)
    portfolio = request.kind.startswith("portfolio")
    if portfolio:
        sample_reference = located.reference
        if located.manifest.scientific_identity.algorithm_versions.get("storage") in {
            "portfolio-analysis-parquet@1",
            "portfolio-analysis-parquet@2",
            "portfolio-analysis-parquet@3",
        }:
            dependency = next(
                value
                for value in located.manifest.dependencies
                if value.role == "portfolio_samples"
            )
            sample_reference = ArtifactRef(
                artifact_id=dependency.artifact_id,
                artifact_kind="portfolio",
                content_hash=dependency.content_hash,
            )
        reader = PortfolioArtifactReader(root, sample_reference)
        dependency = next(
            value for value in reader.artifact.manifest.dependencies if value.role == "exposure"
        )
        exposure = ArtifactRef(
            artifact_id=dependency.artifact_id,
            artifact_kind="exposure",
            content_hash=dependency.content_hash,
        )
        all_samples = [
            row
            for row in reader.read(
                "portfolio_samples", filters=[("portfolio_id", "=", request.portfolio_id)]
            ).to_pylist()
        ]
        samples = [
            row
            for row in all_samples
            if request.kind != "portfolio_sample" or row["round_id"] == request.round_id
        ]
        if not samples:
            raise ValueError("Select an existing portfolio and sampling round")
        if len(samples) > 10000:
            raise QueryLimitExceeded("Window query exceeds the 10,000 observation limit")
        matrices = ReconstructedMatrices(
            root, reader, matrix_ids={row["matrix_id"] for row in samples}
        )
        observations = (str(row["round_id"]) for row in samples)
    else:
        exposure = located.reference
    axes = ExposureArtifactReader(Path(root)).axes(exposure)
    valid_cells, valid_bins = set(axes["cell_ids"]), set(axes["time_bin_ids"])
    cells, bins = set(request.cell_ids or axes["cell_ids"]), set(
        request.time_bin_ids or axes["time_bin_ids"]
    )
    if not cells <= valid_cells or not bins <= valid_bins:
        raise ValueError(
            "Requested cells or reporting bins are outside the retained exposure domain"
        )
    if len(cells) > limits.max_matrix_rows:
        raise QueryLimitExceeded("Too many cells for a window projection")
    if not portfolio:
        observations = tuple(request.replication_ids or axes["replication_ids"])
        if not observations or not set(observations) <= set(axes["replication_ids"]):
            raise ValueError("Invalid replication selection")
        vehicles = set(request.vehicle_keys or axes["vehicle_keys"])
        if not vehicles <= set(axes["vehicle_keys"]):
            raise ValueError("Vehicle selection is outside the frozen catalog")
        pairs = {(key.fleet_id, key.vehicle_id) for key in vehicles}
        artifact = axes["artifact"]
        table = next(value for value in artifact.manifest.tables if value.name == "exposure")
        paths = [
            str(partition_file(artifact.directory / table.relative_path, index))
            for index, partition in enumerate(table.partitions)
            if partition.row_count and partition.partition_values["replication_id"] in observations
        ]

        def source_rows():
            if not paths:
                return
            dataset = ds.dataset(paths, format="parquet")
            predicate = ds.field("cell_id").isin(sorted(cells)) & ds.field("time_bin_id").isin(
                sorted(bins)
            )
            for batch in dataset.scanner(filter=predicate, batch_size=20000).to_batches():
                for row in batch.to_pylist():
                    if (row["fleet_id"], row["vehicle_id"]) in pairs:
                        yield row["replication_id"], row["cell_id"], row["time_bin_id"], row[
                            "duration_s"
                        ]

        rows = source_rows()
    observations = tuple(observations)
    if len(set(observations)) != len(observations):
        raise ValueError("Observation selection contains duplicate identities")
    if request.statistic == "realization" and len(observations) != 1:
        raise ValueError("A realization view requires exactly one replication or sampling round")
    # Only one observation's sums and O(cells + bins) moment states are held.
    # Missing observation/axis pairs enter the denominator as certified zeros.
    moments = {"cells": {}, "bins": {}, "total": {}}

    def accumulate(group, key, value):
        n, mean, m2 = group.get(key, (0, 0.0, 0.0))
        n += 1
        delta = value - mean
        mean += delta / n
        group[key] = (n, mean, m2 + delta * (value - mean))

    def source_groups():
        for observation, group in groupby(rows, key=lambda row: row[0]):
            cell_sums, bin_sums = defaultdict(list), defaultdict(list)
            for _, cell, time_bin, duration in group:
                if cell in cells and time_bin in bins:
                    cell_sums[cell].append(duration)
                    bin_sums[time_bin].append(duration)
            yield observation, {key: math.fsum(v) for key, v in cell_sums.items()}, {
                key: math.fsum(v) for key, v in bin_sums.items()
            }

    groups = matrices.window_observations(samples, cells, bins) if portfolio else source_groups()
    seen = set()
    positive_cells_sum = 0
    for observation, cell_sums, bin_sums in groups:
        if observation in seen or observation not in observations:
            raise ValueError(
                "Exposure rows do not preserve contiguous declared observation identities"
            )
        seen.add(observation)
        for cell, duration in cell_sums.items():
            accumulate(moments["cells"], cell, duration)
        positive_cells_sum += sum(value > 0 for value in cell_sums.values())
        for time_bin, duration in bin_sums.items():
            accumulate(moments["bins"], time_bin, duration)
        accumulate(moments["total"], "total", math.fsum(cell_sums.values()))
    count = len(observations)

    def statistic(group, key):
        n, nonzero_mean, m2 = group.get(key, (0, 0.0, 0.0))
        mean = nonzero_mean * n / count
        if request.statistic in {"mean", "realization"}:
            return mean
        if count < 2:
            return None
        variance = max(0.0, m2 + nonzero_mean**2 * n * (count - n) / count) / (count - 1)
        return math.sqrt(variance) if request.statistic == "std" else variance

    values = []
    for cell in sorted(moments["cells"]):
        value = statistic(moments["cells"], cell)
        if value is not None and value > 0:
            values.append({"cell_id": cell, "time_bin_id": "selected_window", "value": value})
    result = {
        "resource_id": request.resource_id,
        "kind": request.kind,
        "statistic": request.statistic,
        "replication_ids": list(request.replication_ids),
        "vehicle_keys": [key.model_dump(mode="json") for key in request.vehicle_keys],
        "cell_ids": sorted(cells),
        "time_bin_ids": ["selected_window"],
        "source_time_bin_ids": [value for value in axes["time_bin_ids"] if value in bins],
        "expected_shape": [len(cells), 1],
        "values": values,
        "unit": "s^2" if request.statistic == "variance" else "s",
        "zero_fill": "absent_sparse_rows_are_zero",
        "complete": True,
        "replications_R": len(axes["replication_ids"]),
        "sampling_rounds_J": len(all_samples) if portfolio else None,
        "selected_observation_count": count,
        "portfolio_id": request.portfolio_id,
        "round_id": request.round_id,
        "positive_cell_count": len(values),
        "coverage_denominator_cell_count": len(cells),
        "mean_coverage_fraction": positive_cells_sum / count / len(cells) if cells else None,
        "coverage_semantics": (
            "mean_within_observation_any_time_spatial_coverage_road_intersecting_cells"
            if axes["artifact"].manifest.scientific_identity.algorithm_versions.get("grid_domain")
            == "positive-length-road-grid@1"
            else "mean_within_observation_any_time_spatial_coverage_prepared_grid"
        ),
        "overall_value": statistic(moments["total"], "total"),
        "time_summary": [
            {"time_bin_id": value, "value": statistic(moments["bins"], value)}
            for value in axes["time_bin_ids"]
            if value in bins
        ],
        "summary_semantics": "statistic_of_within_observation_cell_sum",
        "temporal_aggregation": "sum",
    }
    if len(json.dumps(result).encode()) > limits.max_matrix_bytes:
        raise QueryLimitExceeded("Window projection exceeds the response byte limit")
    return result
