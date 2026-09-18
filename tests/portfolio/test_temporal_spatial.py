from types import SimpleNamespace

import numpy as np
import pytest

from mobile_sensing.application.project_models import (
    DemandEditor,
    PortfolioEditor,
    ProjectConfig,
    SimulationEditor,
    SpatialFeatureWeight,
)
from mobile_sensing.application.spatial_support import SpatialSupport
from mobile_sensing.application.migration import migrate_project
from mobile_sensing.portfolio.evaluation import _utility_matrix
from mobile_sensing.portfolio.utility import pointwise_utility
from mobile_sensing.portfolio import evaluate_samples
from tests.portfolio.test_portfolio_sampling import (
    _portfolio_config,
    _two_vehicle_exposure,
    _uniform_weights,
)


def test_spatial_mixture_normalizes_components_before_coefficients():
    support = SpatialSupport(
        environment=None,
        locations={},
        cell_locations={"a": "A", "b": "B"},
        feature_values={
            "population": {"a": 90.0, "b": 10.0},
            "shops": {"a": 0.0, "b": 4.0},
        },
        audit=[],
    )
    ids, probabilities = support.weighted_mix(
        (
            SpatialFeatureWeight(feature="population", weight=3),
            SpatialFeatureWeight(feature="shops", weight=1),
        )
    )
    assert ids == ["A", "B"]
    assert probabilities == pytest.approx(np.array([0.675, 0.325]))
    assert probabilities.sum() == pytest.approx(1.0)


def test_utility_interval_sums_reporting_bins_before_nonlinear_utility():
    plan = SimpleNamespace(utility_bin_by_time_bin={"hour-1": "day", "hour-2": "day"})
    matrix = {("cell", "hour-1"): 2.0, ("cell", "hour-2"): 3.0}
    aggregated = _utility_matrix(matrix, plan)
    assert aggregated == {("cell", "day"): 5.0}
    daily = pointwise_utility(aggregated["cell", "day"], "exponential_saturation", 5.0)
    hourly = 0.5 * sum(
        pointwise_utility(value, "exponential_saturation", 5.0) for value in (2.0, 3.0)
    )
    assert daily == pytest.approx(0.99)
    assert daily != pytest.approx(hourly)


def test_portfolio_utility_interval_is_independent_of_reporting_bins(tmp_path):
    exposure, _ = _two_vehicle_exposure(tmp_path, (0.0, 1.0, 2.0))
    hourly = _portfolio_config(exposure, {"fleet": (2,)}, sampling_rounds=1)
    daily = hourly.model_copy(
        update={
            "utility": hourly.utility.model_copy(update={"temporal_interval_s": 2.0}),
            "sample_matrix_storage": "reconstruct",
        }
    )
    hourly_value = evaluate_samples(
        artifact_root=tmp_path,
        exposure=exposure,
        config=hourly,
        weights=_uniform_weights(),
    ).sample_rows[0]["utility"]
    daily_samples = evaluate_samples(
        artifact_root=tmp_path,
        exposure=exposure,
        config=daily,
        weights=_uniform_weights(),
    )
    daily_value = daily_samples.sample_rows[0]["utility"]
    assert hourly_value == pytest.approx(0.99)
    assert daily_value == pytest.approx(0.9999)
    assert daily_value > hourly_value
    assert daily_samples.matrix_metadata[0]["nonzero_rows"] == 2


def test_v32_migration_preserves_old_utility_bin_semantics():
    old = ProjectConfig(
        schema_version="3.2",
        simulation=SimulationEditor(temporal_resolution_minutes=60),
        portfolio=PortfolioEditor.model_construct(),
    )
    payload = old.model_dump(
        mode="json", exclude={"portfolio": {"utility_temporal_resolution_minutes"}}
    )
    migrated = migrate_project(payload, "revision-old")
    assert migrated.config.schema_version == "3.4"
    assert migrated.config.portfolio.utility_temporal_resolution_minutes == 60


def test_spatial_mixture_requires_positive_coefficients():
    with pytest.raises(ValueError, match="positive total mass"):
        DemandEditor(spatial_weights=(SpatialFeatureWeight(feature="population", weight=0),))
