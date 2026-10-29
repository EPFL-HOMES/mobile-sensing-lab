"""Explicit joint-OD selections preserve historical reads and lightweight checks."""

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Point

from mobile_sensing.application.configuration_check import check_configuration
from mobile_sensing.application.project_models import (
    DemandEditor,
    FleetEditor,
    ProjectConfig,
    SpatialFeatureWeight,
)
from mobile_sensing.application.project_resolution import resolve_project
from mobile_sensing.application.run_models import RunOptions, RunView
from mobile_sensing.application.run_pipeline import run_project
from mobile_sensing.contracts import ArtifactRef
from mobile_sensing.datasets.inputs import InputRegistration, load_input, snapshot_input
from mobile_sensing.environment import PreparedEnvironmentReader
from tests.application.test_runs_analysis import config_fixture
from tests.environment.test_environment_editor import Cancellation
from tests.support.environment_fixtures import RecordedProgress


def _snapshot(tmp_path, root, *, role="demand", extension="csv", extra=None, omit=None):
    values = {"origin_cell_id": ["a"], "destination_cell_id": ["b"], "weight": [1.0]}
    values.update(extra or {})
    if omit:
        values.pop(omit)
    frame = pd.DataFrame(values)
    source = tmp_path / f"joint_od.{extension}"
    if extension == "csv":
        frame.to_csv(source, index=False)
    elif extension == "parquet":
        frame.to_parquet(source, index=False)
    else:
        gpd.GeoDataFrame(frame, geometry=[Point(8, 47)], crs="EPSG:4326").to_file(source)
    return snapshot_input(root, InputRegistration(path=str(source), name="Joint OD", role=role))


def _with_demand(config, demand):
    fleet = config.fleets[0].model_copy(update={"demand": demand})
    return config.model_copy(update={"fleets": (fleet,)})


def _check(root, config):
    return check_configuration(
        root, config, cancellation=Cancellation(), progress=RecordedProgress()
    )


@pytest.mark.parametrize("role", ["demand", "feature", "weight", "population"])
@pytest.mark.parametrize("extension", ["csv", "parquet"])
def test_cell_id_only_od_registration_needs_no_crs(tmp_path, role, extension):
    item = _snapshot(
        tmp_path, tmp_path / "artifacts", role=role, extension=extension, extra={"note": ["x"]}
    )
    assert item["source_crs"] is None
    assert item["crs_required"] is False


@pytest.mark.parametrize(
    "extra",
    [
        {"longitude": [8.0], "latitude": [47.0]},
        {"origin_x": [100.0], "origin_y": [200.0]},
        {"pickup_lon": [8.0], "pickup_lat": [47.0]},
        {"geometry": ["POINT (8 47)"]},
    ],
)
def test_od_columns_do_not_exempt_coordinate_or_geometry_tables_from_crs(tmp_path, extra):
    item = _snapshot(tmp_path, tmp_path / "artifacts", extra=extra)
    assert item["crs_required"] is True


def test_od_columns_do_not_exempt_crsless_geoparquet_geometry(tmp_path):
    source = tmp_path / "joint_od.parquet"
    gpd.GeoDataFrame(
        {"origin_cell_id": ["a"], "destination_cell_id": ["b"], "weight": [1.0]},
        geometry=[Point(100, 200)],
    ).to_parquet(source)
    item = snapshot_input(
        tmp_path / "artifacts",
        InputRegistration(path=str(source), name="OD with geometry", role="feature"),
    )
    assert item["source_crs"] is None
    assert item["crs_required"] is True


@pytest.mark.parametrize("role", ["demand", "feature", "weight", "population"])
def test_joint_od_check_uses_metadata_and_skips_both_inactive_mixtures(tmp_path, monkeypatch, role):
    root, config = config_fixture(tmp_path)
    item = _snapshot(tmp_path, root, role=role)
    demand = DemandEditor(
        task_type="od",
        od_distribution_input=item["input_id"],
        spatial_weights=(SpatialFeatureWeight(feature="unused_origin"),),
        destination_spatial_weights=(SpatialFeatureWeight(feature="unused_destination"),),
    )
    config = _with_demand(config, demand)

    def forbidden(*args, **kwargs):
        raise AssertionError("Configuration checks must not read OD rows or prepare routes")

    monkeypatch.setattr(pd, "read_csv", forbidden)
    monkeypatch.setattr(pd, "read_parquet", forbidden)
    monkeypatch.setattr(PreparedEnvironmentReader, "read", forbidden)
    result = _check(root, config)
    assert result.validation_level == "configuration"
    assert result.task_counts == {} and result.vehicle_counts == {"f": 2}
    assert not (root / "preparation_cache").exists()

    with pytest.raises(ValueError, match="F: A selected spatial feature is absent"):
        _check(
            root, _with_demand(config, demand.model_copy(update={"od_distribution_input": None}))
        )


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"role": "supply"}, "incompatible role"),
        ({"omit": "weight"}, "origin_cell_id, destination_cell_id and weight"),
        ({"extension": "geojson"}, "requires CSV or Parquet"),
        ({"extra": {"longitude": [8.0], "latitude": [47.0]}}, "Confirm the CRS"),
    ],
)
def test_joint_od_metadata_rejects_incompatible_inputs(tmp_path, options, message):
    root, config = config_fixture(tmp_path)
    item = _snapshot(tmp_path, root, **options)
    config = _with_demand(
        config, DemandEditor(task_type="od", od_distribution_input=item["input_id"])
    )
    with pytest.raises(ValueError, match=f"F: .*{message}"):
        _check(root, config)


@pytest.mark.parametrize("missing", ["metadata", "source"])
def test_joint_od_missing_inputs_fail_with_fleet_context(tmp_path, missing):
    root, config = config_fixture(tmp_path)
    item = _snapshot(tmp_path, root)
    _, source = load_input(root, item["input_id"])
    (source.parent / "input.json" if missing == "metadata" else source).unlink()
    config = _with_demand(
        config, DemandEditor(task_type="od", od_distribution_input=item["input_id"])
    )
    message = "Selected input .* is missing" if missing == "metadata" else "Invalid input inventory"
    with pytest.raises(ValueError, match=f"F: {message}"):
        _check(root, config)


@pytest.mark.parametrize(
    ("demand", "message"),
    [
        (DemandEditor(task_type="location", od_distribution_input="input_old"), "generated OD"),
        (
            DemandEditor(source="import", task_type="od", od_distribution_input="input_old"),
            "generated OD",
        ),
        (
            DemandEditor(source="import", task_type="ordered", od_distribution_input="input_old"),
            "generated OD",
        ),
        (DemandEditor(task_type="od", od_distribution_input=""), "Select an OD distribution"),
    ],
)
def test_historical_stale_od_reads_but_active_check_resolution_and_run_reject(
    tmp_path, monkeypatch, demand, message
):
    config = ProjectConfig(fleets=(FleetEditor(fleet_id="f", name="F", demand=demand),))
    # Persisted configurations and saved run views must remain deserializable.
    config = ProjectConfig.model_validate_json(config.model_dump_json())
    reference = ArtifactRef(artifact_id="legacy", artifact_kind="dataset", content_hash="0" * 64)
    view = RunView(
        run_id="legacy",
        artifact=reference,
        name="Legacy run",
        source_revision_id=None,
        config=config,
        simulation=reference,
        exposure=reference,
        resolution=reference,
        replications=1,
        vehicle_counts={"f": 10},
        task_counts={"f": (100,)},
        assumptions=(),
        mobility_reused=False,
        elapsed_seconds={},
    )
    assert RunView.model_validate_json(view.model_dump_json()).config == config

    def forbidden(*args, **kwargs):
        raise AssertionError("Invalid OD choices must fail before calendar, cache or preparation")

    monkeypatch.setattr("mobile_sensing.application.calendar_authoring.resolve_calendar", forbidden)
    kwargs = dict(cancellation=Cancellation(), progress=RecordedProgress())
    with pytest.raises(ValueError, match=f"F: .*{message}"):
        check_configuration(tmp_path, config, **kwargs)
    with pytest.raises(ValueError, match=f"F: .*{message}"):
        resolve_project(tmp_path, config, **kwargs)
    with pytest.raises(ValueError, match=f"F: .*{message}"):
        run_project(
            tmp_path,
            config,
            name="Invalid current run",
            source_revision_id=None,
            options=RunOptions(),
            **kwargs,
        )
    assert not list(tmp_path.iterdir())
