from __future__ import annotations

import importlib.metadata
import json
from types import SimpleNamespace

import numpy as np
import pytest

from mobile_sensing import __version__
from mobile_sensing.application import notebook_workflow as workflow
from mobile_sensing.environment import LocalEnvironmentBuilder
from tests.support.environment_fixtures import (
    NoCancellation,
    RecordedProgress,
    build_config,
    write_analytic_sources,
)
from tests.environment.test_environment import _acquire
from tests.portfolio.test_portfolio_sampling import _two_vehicle_exposure
from tests.portfolio.test_portfolio_analysis import _anticorrelated_exposure


def test_environment_publication_without_distribution_metadata(tmp_path, monkeypatch):
    original = importlib.metadata.version

    def missing_project(name):
        if name == "mobile-sensing":
            raise importlib.metadata.PackageNotFoundError(name)
        return original(name)

    monkeypatch.setattr(importlib.metadata, "version", missing_project)
    catalog = write_analytic_sources(tmp_path / "sources")
    reference = LocalEnvironmentBuilder(catalog, tmp_path / "artifacts").prepare(
        _acquire(catalog),
        build_config(),
        cancellation=NoCancellation(),
        progress=RecordedProgress(),
    )
    manifest = json.loads(
        (tmp_path / "artifacts/environments" / reference.artifact_id / "manifest.json").read_text()
    )
    assert manifest["runtime"]["package_version"] == __version__
    assert manifest["complete"]


def test_static_selection_zeros_filters_and_rng_isolation(tmp_path):
    exposure, _ = _two_vehicle_exposure(tmp_path)
    for kwargs in ({"vehicle_ids": []}, {"vehicle_count": 0}, {"vehicle_ids": ("inactive",)}):
        view = workflow.sensing_view(tmp_path, exposure, **kwargs)
        assert view.cells.duration_s.sum() == 0
    moving = workflow.sensing_view(tmp_path, exposure, vehicle_ids=("moving",))
    assert moving.cells.duration_s.sum() == pytest.approx(2.0)
    np.random.seed(345)
    before = np.random.get_state()
    first = workflow.sensing_view(tmp_path, exposure, vehicle_count=1, selection_seed=4)
    workflow.sensing_view(tmp_path, exposure, vehicle_count=1, selection_seed=99)
    second = workflow.sensing_view(tmp_path, exposure, vehicle_count=1, selection_seed=4)
    after = np.random.get_state()
    assert first.vehicle_ids == second.vehicle_ids
    assert np.array_equal(before[1], after[1]) and before[2:] == after[2:]
    for kwargs in (
        {"vehicle_ids": ("missing",)},
        {"bin_indices": (999,)},
        {"statistic": "std"},
        {"replication_ids": ()},
        {"vehicle_ids": [], "vehicle_count": 1},
    ):
        with pytest.raises(ValueError):
            workflow.sensing_view(tmp_path, exposure, **kwargs)


def test_static_sd_sums_vehicles_before_reducing_replications(tmp_path):
    exposure = _anticorrelated_exposure(tmp_path)
    mean = workflow.sensing_view(tmp_path, exposure)
    sd = workflow.sensing_view(tmp_path, exposure, statistic="std")
    assert mean.cells.duration_s.sum() == pytest.approx(10.0)
    assert sd.cells.duration_s.sum() == 0.0
    assert sd.time_series.duration_s.sum() == 0.0


def test_reporting_bins_include_short_final_interval():
    assert workflow.reporting_edges(100.0, 200.0, 30.0) == (100.0, 130.0, 160.0, 190.0, 200.0)
    for end, width in ((100.0, 30.0), (200.0, 0.0), (float("inf"), 1.0)):
        with pytest.raises(ValueError):
            workflow.reporting_edges(100.0, end, width)


def test_operations_exclude_future_timetable_but_retain_censored_and_warmup():
    def task(key, target):
        return SimpleNamespace(task_id=key, steps=[SimpleNamespace(scheduled_time_s=target)])

    def outcome(key, state):
        return SimpleNamespace(
            task_id=key,
            kind="service",
            status=SimpleNamespace(value=state),
            assignment_wait_s=2.0 if state == "completed" else None,
        )

    validated = SimpleNamespace(
        replications=[
            SimpleNamespace(
                replication_id="r",
                tasks=[task("warmup", 0.0), task("future", 100.0), task("generated", None)],
            )
        ]
    )
    simulation = SimpleNamespace(
        results=[
            SimpleNamespace(
                replication_id="r",
                end_s=100.0,
                processed_heap_events=8,
                vehicle_outcomes=[],
                task_outcomes=[
                    outcome("warmup", "completed"),
                    outcome("future", "unserved"),
                    outcome("generated", "unserved"),
                ],
            )
        ]
    )
    summary, _ = workflow.operations_tables(simulation, validated)
    assert summary.service_tasks.iloc[0] == 2
    assert summary.future_scheduled_tasks_excluded.iloc[0] == 1
    assert summary.completion_fraction.iloc[0] == 0.5
