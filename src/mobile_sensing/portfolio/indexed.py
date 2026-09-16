"""Bounded numerical fleet prefixes on a compressed, observed cell/bin axis."""

from collections import OrderedDict, defaultdict

import numpy as np


class IndexedFleetPrefixes:
    def __init__(self, source, counts, weight, *, max_bytes, utility_axis=lambda axis: axis):
        self.source = defaultdict(dict)
        for (replication, fleet, vehicle), values in source.items():
            self.source[replication][(fleet, vehicle)] = values
        self.levels = {
            fleet: sorted({row.count_by_fleet[fleet] for row in counts})
            for fleet in counts[0].count_by_fleet
        }
        self.weight = weight
        self.utility_axis = utility_axis
        self.max_bytes = min(max_bytes, 64 * 1024**2)
        self.indexes = OrderedDict()
        self.prefixes = None

    def prepare(self, replication, permutations):
        self.prefixes = None
        self.weights = None
        source = self.source[replication]
        index = self.indexes.pop(replication, None)
        if index is None:
            axes = tuple(sorted({axis for values in source.values() for axis in values}))
            # Include scratch vectors and weight/index storage in the numerical bound.
            utility_axes = tuple(sorted({self.utility_axis(axis) for axis in axes}))
            required = (
                len(axes) * 8 * (8 + sum(map(len, self.levels.values())))
                + len(utility_axes) * 16
                + sum(len(values) * 16 for values in source.values())
            )
            if required > self.max_bytes:
                return False
            while (
                self.indexes
                and required + sum(self._index_bytes(value) for value in self.indexes.values())
                > self.max_bytes
            ):
                self.indexes.popitem(last=False)
            positions = {axis: i for i, axis in enumerate(axes)}
            vehicles = {
                key: (
                    np.fromiter(
                        (positions[axis] for axis in values), dtype=np.int64, count=len(values)
                    ),
                    np.fromiter(values.values(), dtype=np.float64, count=len(values)),
                )
                for key, values in source.items()
            }
            utility_positions = {axis: i for i, axis in enumerate(utility_axes)}
            groups = np.fromiter(
                (utility_positions[self.utility_axis(axis)] for axis in axes),
                dtype=np.int64,
                count=len(axes),
            )
            weights = np.fromiter(
                (self.weight(axis) for axis in utility_axes),
                dtype=np.float64,
                count=len(utility_axes),
            )
            index = vehicles, weights, groups
        self.indexes[replication] = index
        scratch = len(index[2]) * 8 * (7 + sum(map(len, self.levels.values())))
        while len(self.indexes) > 2 or (
            len(self.indexes) > 1
            and scratch + sum(self._index_bytes(value) for value in self.indexes.values())
            > self.max_bytes
        ):
            self.indexes.popitem(last=False)
        vehicles, weights, groups = index
        prefixes = {}
        for fleet, levels in self.levels.items():
            current = np.zeros(len(groups), dtype=np.float64)
            rank = 0
            for level in levels:
                while rank < level:
                    key = permutations[fleet][rank]
                    values = vehicles.get((key.fleet_id, key.vehicle_id))
                    if values is not None:
                        positions, durations = values
                        current[positions] += durations
                    rank += 1
                prefixes[fleet, level] = current.copy()
        self.prefixes, self.weights, self.groups = prefixes, weights, groups
        return True

    @staticmethod
    def _index_bytes(index):
        vehicles, weights, groups = index
        return (
            weights.nbytes
            + groups.nbytes
            + sum(indices.nbytes + durations.nbytes for indices, durations in vehicles.values())
        )

    def evaluate(self, counts, kind, saturation_s):
        matrix = np.zeros(len(self.groups), dtype=np.float64)
        for fleet in sorted(counts):
            matrix += self.prefixes[fleet, counts[fleet]]
        total = float(np.sum(matrix, dtype=np.float64))
        if not np.isfinite(total):
            raise ValueError("Aggregated sample exposure exceeds finite float64 range")
        nonzero = int(np.count_nonzero(matrix))
        utility_matrix = np.bincount(self.groups, weights=matrix, minlength=len(self.weights))
        if kind == "exponential_saturation":
            values = -np.expm1(-utility_matrix / saturation_s)
        elif kind == "linear_capped":
            values = np.minimum(utility_matrix / saturation_s, 1.0)
        elif kind == "binary":
            values = utility_matrix > 0
        elif kind == "linear_diagnostic":
            values = utility_matrix / saturation_s
        else:
            raise ValueError(f"Unsupported utility {kind!r}")
        utility = float(np.sum(self.weights * values, dtype=np.float64))
        if not np.isfinite(utility):
            raise ValueError("Sample utility exceeds finite float64 range")
        return total, utility, nonzero
