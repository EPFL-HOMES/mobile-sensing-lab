"""Reconstruct one selected matrix from immutable physical-vehicle exposure."""

import math
from collections import defaultdict
from collections.abc import Mapping
import numpy as np

from mobile_sensing.contracts import ArtifactRef, VehicleKey, scientific_hash, stable_id
from mobile_sensing.exposure import ExposureArtifactReader


class ReconstructedMatrices(Mapping):
    """Only one aggregate matrix is retained; the source is sparse by vehicle."""

    def __init__(self, root, reader, *, cancellation=None, matrix_ids=None):
        self.cancellation = cancellation
        dependency = next(
            item for item in reader.artifact.manifest.dependencies if item.role == "exposure"
        )
        self.exposure = ArtifactRef(
            artifact_id=dependency.artifact_id,
            artifact_kind="exposure",
            content_hash=dependency.content_hash,
        )
        filters = [("matrix_id", "in", sorted(matrix_ids))] if matrix_ids is not None else None
        self.registry = {
            row["matrix_id"]: row
            for row in reader.read("sample_matrices", filters=filters).to_pylist()
        }
        if matrix_ids is not None:
            if set(self.registry) != set(matrix_ids):
                raise ValueError("Requested matrix is outside the complete sample registry")
        samples = reader.read("portfolio_samples", filters=filters).to_pylist()
        selection_filters = (
            [("sample_id", "in", [row["sample_id"] for row in samples])]
            if matrix_ids is not None
            else None
        )
        selections = defaultdict(list)
        for row in reader.read("sample_selection", filters=selection_filters).to_pylist():
            selections[row["sample_id"]].append(
                VehicleKey(fleet_id=row["fleet_id"], vehicle_id=row["vehicle_id"])
            )
        self.selection = {}
        for sample in samples:
            if sample["matrix_id"] not in self.registry:
                continue
            selected = tuple(
                sorted(
                    selections[sample["sample_id"]], key=lambda key: (key.fleet_id, key.vehicle_id)
                )
            )
            matrix_id = sample["matrix_id"]
            record = self.registry[matrix_id]
            expected = stable_id(
                "matrix",
                {
                    "exposure_hash": self.exposure.content_hash,
                    "replication_id": record["selected_joint_replication_id"],
                    "selected_vehicles": selected,
                },
            )
            if (
                expected != matrix_id
                or scientific_hash(selected) != record["selected_vehicle_set_hash"]
            ):
                raise ValueError("Reconstruction selection differs from immutable matrix identity")
            if matrix_id in self.selection and self.selection[matrix_id] != selected:
                raise ValueError("Conflicting selections for one matrix")
            self.selection[matrix_id] = selected
        source = ExposureArtifactReader(root)
        replications = tuple(
            sorted({record["selected_joint_replication_id"] for record in self.registry.values()})
        )
        vehicles = tuple(
            sorted(
                {key for selected in self.selection.values() for key in selected},
                key=lambda key: (key.fleet_id, key.vehicle_id),
            )
        )
        chunks = ()
        if replications and vehicles:
            _, chunks = source.read_sparse_batches(
                self.exposure, replication_ids=replications, vehicle_keys=vehicles
            )
        self.vehicle_values = defaultdict(dict)
        keys = {(key.fleet_id, key.vehicle_id): key for key in vehicles}
        for chunk in chunks:
            if cancellation:
                cancellation.raise_if_cancelled()
            for row in chunk.to_pylist():
                values = self.vehicle_values[
                    (row["replication_id"], keys[row["fleet_id"], row["vehicle_id"]])
                ]
                axis = (row["cell_id"], row["time_bin_id"])
                if axis in values:
                    raise ValueError("Duplicate physical-vehicle exposure axis")
                values[axis] = row["duration_s"]
        self.cached_id, self.cached_matrix = None, None

    def window_observations(self, samples, cells, bins):
        """Sum time inside each vehicle once, then form one selected observation at a time."""
        cell_axis, bin_axis = tuple(sorted(cells)), tuple(sorted(bins))
        cell_index = {value: i for i, value in enumerate(cell_axis)}
        bin_index = {value: i for i, value in enumerate(bin_axis)}
        projected = {}
        for key, values in self.vehicle_values.items():
            grouped_cells, grouped_bins = defaultdict(list), defaultdict(list)
            for (cell, time_bin), duration in values.items():
                if cell in cells and time_bin in bins:
                    grouped_cells[cell_index[cell]].append(duration)
                    grouped_bins[bin_index[time_bin]].append(duration)
            projected[key] = tuple(
                (
                    np.fromiter(group, dtype=np.int64),
                    np.fromiter((math.fsum(v) for v in group.values()), dtype=np.float64),
                )
                for group in (grouped_cells, grouped_bins)
            )
        for sample in samples:
            if self.cancellation:
                self.cancellation.raise_if_cancelled()
            matrix_id = sample["matrix_id"]
            replication = self.registry[matrix_id]["selected_joint_replication_id"]
            cell_values = np.zeros(len(cell_axis), dtype=np.float64)
            bin_values = np.zeros(len(bin_axis), dtype=np.float64)
            for key in self.selection[matrix_id]:
                value = projected.get((replication, key))
                if value is not None:
                    (ci, cv), (bi, bv) = value
                    cell_values[ci] += cv
                    bin_values[bi] += bv
            yield str(sample["round_id"]), {
                cell_axis[i]: float(cell_values[i]) for i in np.flatnonzero(cell_values)
            }, {bin_axis[i]: float(bin_values[i]) for i in np.flatnonzero(bin_values)}

    def __iter__(self):
        return iter(self.registry)

    def __len__(self):
        return len(self.registry)

    def __getitem__(self, matrix_id):
        if self.cancellation:
            self.cancellation.raise_if_cancelled()
        if self.cached_id == matrix_id:
            return self.cached_matrix
        record = self.registry[matrix_id]
        grouped = defaultdict(list)
        for key in self.selection[matrix_id]:
            for axis, duration in self.vehicle_values.get(
                (record["selected_joint_replication_id"], key), {}
            ).items():
                grouped[axis].append(duration)
        matrix = {axis: math.fsum(values) for axis, values in grouped.items()}
        if len(matrix) != record["nonzero_rows"] or not math.isclose(
            math.fsum(matrix.values()), record["total_exposure_s"], rel_tol=1e-12, abs_tol=1e-12
        ):
            raise ValueError("Reconstructed matrix disagrees with its retained exposure summary")
        self.cached_id, self.cached_matrix = matrix_id, matrix
        return matrix


def reconstructed_statistics(root, reader, portfolio_id, *, cells=(), bins=(), max_rows=100000):
    from mobile_sensing.portfolio.analysis import _sparse_axis_statistics

    samples = reader.read(
        "portfolio_samples", filters=[("portfolio_id", "=", portfolio_id)]
    ).to_pylist()
    if not samples:
        raise ValueError("Select an existing count portfolio")
    if len(samples) > 10000:
        raise ValueError("On-demand statistics exceed the 10,000 observation query limit")
    matrices = ReconstructedMatrices(root, reader, matrix_ids={row["matrix_id"] for row in samples})
    cells, bins = frozenset(cells), frozenset(bins)

    class FilteredMatrices:
        def __getitem__(self, identifier):
            return {
                axis: value
                for axis, value in matrices[identifier].items()
                if (not cells or axis[0] in cells) and (not bins or axis[1] in bins)
            }

    metadata = reader.read("portfolio_metadata").to_pylist()[0]
    return _sparse_axis_statistics(
        portfolio_id=portfolio_id,
        samples=samples,
        matrix_values=FilteredMatrices(),
        replications_R=metadata["replications_R"],
        sampling_rounds_J=len(samples),
        max_rows=max_rows,
    )


def reconstructed_matrix_rows(root, reader, matrix_ids, *, cells=(), bins=(), max_rows=100000):
    """Bounded query projection with explicit failure, never an incomplete matrix."""
    matrices = ReconstructedMatrices(root, reader, matrix_ids=matrix_ids)
    cells, bins = frozenset(cells), frozenset(bins)
    rows = []
    for matrix_id in sorted(set(matrix_ids)):
        for (cell, time_bin), duration in sorted(matrices[matrix_id].items()):
            if (cells and cell not in cells) or (bins and time_bin not in bins):
                continue
            if len(rows) >= max_rows:
                raise ValueError(
                    "Reconstructed matrix exceeds the configured query row limit; select fewer cells or bins"
                )
            rows.append(
                {
                    "matrix_id": matrix_id,
                    "cell_id": cell,
                    "time_bin_id": time_bin,
                    "duration_s": duration,
                }
            )
    return rows
