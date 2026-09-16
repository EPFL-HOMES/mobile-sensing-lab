"""Whole-grid spatial support shared by fleet generators and initial locations."""

from dataclasses import dataclass
import numpy as np
from mobile_sensing.application.resource_tables import read_table
from mobile_sensing.contracts import LocationRef, ResolutionStatus, stable_id


@dataclass
class SpatialSupport:
    environment: object
    locations: dict[str, LocationRef]
    cell_locations: dict[str, str]
    feature_values: dict[str, dict[str, float]]
    audit: list[dict]

    def weighted(self, feature, allowed_locations=None):
        return self.weighted_mix(((feature, 1.0),), allowed_locations)

    def weighted_mix(self, components, allowed_locations=None):
        components = tuple(
            (item.feature, item.weight) if hasattr(item, "feature") else item for item in components
        )
        if (
            not components
            or any(weight < 0 for _, weight in components)
            or sum(weight for _, weight in components) <= 0
        ):
            raise ValueError("Spatial-feature mixture requires positive coefficient mass")
        for feature, _ in components:
            if feature not in self.feature_values:
                raise ValueError(
                    f"Spatial feature {feature!r} is unavailable; choose a prepared feature or explicit uniform"
                )
        all_cells = sorted(
            set().union(*(self.feature_values[feature] for feature, _ in components))
        )
        mixture = np.zeros(len(all_cells), dtype=float)
        coefficient_total = sum(weight for _, weight in components)
        for feature, coefficient in components:
            if coefficient == 0:
                continue
            source = np.array(
                [self.feature_values[feature].get(cell, 0.0) for cell in all_cells], dtype=float
            )
            source_total = source.sum()
            if not np.isfinite(source_total) or source_total <= 0:
                raise ValueError(f"Feature {feature!r} has no positive mass on the prepared grid")
            mixture += coefficient / coefficient_total * source / source_total
        eligible = np.array(
            [
                cell in self.cell_locations
                and (allowed_locations is None or self.cell_locations[cell] in allowed_locations)
                and mixture[index] > 0
                for index, cell in enumerate(all_cells)
            ]
        )
        cells = [cell for cell, keep in zip(all_cells, eligible, strict=True) if keep]
        mixture = mixture[eligible]
        if not cells:
            raise ValueError("Spatial-feature mixture has no positive eligible mass")
        ids = [self.cell_locations[cell] for cell in cells]
        return ids, mixture / mixture.sum()

    def raw_mixture(self, components):
        """Return the normalized whole-grid mixture keyed by cell for audit calculations."""
        components = tuple(
            (item.feature, item.weight) if hasattr(item, "feature") else item for item in components
        )
        for feature, _ in components:
            if feature not in self.feature_values:
                raise ValueError(f"Spatial feature {feature!r} is unavailable")
        cells = sorted(set().union(*(self.feature_values[feature] for feature, _ in components)))
        coefficient_total = sum(weight for _, weight in components)
        if coefficient_total <= 0 or any(weight < 0 for _, weight in components):
            raise ValueError("Spatial-feature mixture requires positive coefficient mass")
        result = np.zeros(len(cells), dtype=float)
        for feature, coefficient in components:
            source = np.array([self.feature_values[feature].get(cell, 0.0) for cell in cells])
            if source.sum() <= 0:
                raise ValueError(f"Spatial feature {feature!r} has no positive mass")
            result += coefficient / coefficient_total * source / source.sum()
        return dict(zip(cells, result, strict=True))

    def cell(self, cell_id):
        try:
            return self.cell_locations[str(cell_id)]
        except KeyError as exc:
            raise ValueError(
                f"Grid cell {cell_id!r} is absent or cannot resolve to a road node"
            ) from exc


def prepare_support(root, environment, feature_ref, cancellation=None):
    cells = environment.grid_cells.sort_values("cell_id")
    features = read_table(root, feature_ref, "grid_features")
    weights = {
        name: dict(zip(group.cell_id, group.value)) for name, group in features.groupby("feature")
    }
    locations, mapping, audit = {}, {}, []
    points = cells.sensing_geometry.representative_point()
    for i, (cell, point) in enumerate(zip(cells.cell_id, points)):
        if cancellation and i % 256 == 0:
            cancellation.raise_if_cancelled()
        identifier = stable_id(
            "grid_location", {"grid": environment.metadata.grid_axis_hash, "cell": cell}
        )
        location = environment.snapping.resolve(
            location_id=identifier,
            x=float(point.x),
            y=float(point.y),
            source_crs=environment.metadata.working_crs,
        )
        accepted = location.resolution_status == ResolutionStatus.RESOLVED
        audit.append(
            {
                "cell_id": cell,
                "location_id": identifier,
                "accepted": accepted,
                "reason": location.resolution_status.value,
                "snap_distance_m": location.snap_distance_m,
                "node_id": location.node_id,
            }
        )
        if accepted:
            locations[identifier] = location
            mapping[cell] = identifier
    return SpatialSupport(environment, locations, mapping, weights, audit)
