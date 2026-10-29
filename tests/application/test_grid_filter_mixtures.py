import numpy as np
import pandas as pd
import pytest

from mobile_sensing.application.project_models import GridFilter
from mobile_sensing.application.resource_tables import publish_tables, read_table
from mobile_sensing.application.spatial_support import prepare_support
from mobile_sensing.environment import PreparedEnvironmentReader
from mobile_sensing.exposure.geometry import road_intersecting_grid_cells
from tests.application.test_runs_analysis import config_fixture


def test_filtered_mixture_ignores_zero_coefficient_layer_without_retained_mass(tmp_path):
    root, config = config_fixture(tmp_path)
    prepared = config.prepared_environment
    environment = PreparedEnvironmentReader(root).read(prepared.artifact)
    road_cells = road_intersecting_grid_cells(environment.road_edges, environment.grid_cells)
    selected = tuple(sorted(road_cells.cell_id)[:2])
    uniform = read_table(root, prepared.features, "grid_features")
    population = uniform.assign(
        feature="population", value=uniform.cell_id.isin(selected).astype(float)
    )
    shops = uniform.assign(feature="shops", value=(~uniform.cell_id.isin(selected)).astype(float))
    features = publish_tables(
        root,
        config={"fixture": "grid-filter-zero-coefficient"},
        dependencies=(),
        algorithm="test-grid-features@1",
        frames={"grid_features": pd.concat([uniform, population, shops])},
        keys={"grid_features": ("cell_id", "feature")},
    )
    support = prepare_support(
        root,
        environment,
        features,
        grid_filter=GridFilter(features=("population",)),
    )
    components = (("population", 1.0), ("shops", 0.0))
    assert support.raw_mixture(components) == {cell: 1.0 / len(selected) for cell in selected}
    locations, probabilities = support.weighted_mix(components)
    assert set(locations) == {support.cell(cell) for cell in selected}
    np.testing.assert_allclose(probabilities, 1.0 / len(selected))
    with pytest.raises(ValueError, match="no positive mass"):
        support.raw_mixture((("population", 1.0), ("shops", 1.0)))
