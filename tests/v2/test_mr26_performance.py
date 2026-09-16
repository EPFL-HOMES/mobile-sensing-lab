"""Scientific equivalence and invalidation boundaries for bounded performance paths."""

import json
import math

import numpy as np
import pytest

from mobile_sensing.application import HeadlessApplication
from mobile_sensing.application.project_models import PortfolioEditor, PortfolioFleetEditor
from mobile_sensing.application.run_models import RunOptions
from mobile_sensing.application.run_pipeline import run_project, run_analysis
from mobile_sensing.api.models import MatrixQueryRequest
from mobile_sensing.api.queries import query_matrix
from mobile_sensing.contracts import VehicleKey
from mobile_sensing.jobs import JobStoreLimits
from mobile_sensing.portfolio import PortfolioArtifactReader, PortfolioAnalysisArtifactReader
from mobile_sensing.portfolio.evaluation import evaluate_samples
from mobile_sensing.portfolio.indexed import IndexedFleetPrefixes
from mobile_sensing.portfolio.utility import pointwise_utility
from tests.v2.test_m08a_portfolio_sampling import (
    _two_vehicle_exposure,
    _portfolio_config,
    _uniform_weights,
)
from tests.v2.test_m08b_portfolio_analysis import _anticorrelated_exposure
from tests.v2.test_mr05_runs_analysis import config_fixture
from tests.v2.test_mr02_environment_editor import Cancellation
from tests.v2.environment_fixtures import RecordedProgress


@pytest.mark.parametrize(
    "kind", ["exponential_saturation", "linear_capped", "binary", "linear_diagnostic"]
)
def test_indexed_prefixes_match_independent_fsum_for_overlaps_and_empty_catalog_members(kind):
    from types import SimpleNamespace

    rng = np.random.default_rng(431)
    source = {}
    permutations = {}
    for fleet in ("a", "b", "c"):
        keys = [VehicleKey(fleet_id=fleet, vehicle_id=f"v{i}") for i in range(8)]
        permutations[fleet] = tuple(keys[i] for i in rng.permutation(8))
        for key in keys[:-1]:
            source["r", fleet, key.vehicle_id] = {
                (f"c{i}", f"t{i % 3}"): float(rng.lognormal(3, 2))
                for i in range(25)
                if rng.random() > 0.3
            }
    counts = [
        SimpleNamespace(count_by_fleet={"a": a, "b": b, "c": c})
        for a in (0, 3, 8)
        for b in (0, 3, 8)
        for c in (0, 3, 8)
    ]

    def weight(axis):
        return (int(axis[0][1:]) + 1) / 325

    engine = IndexedFleetPrefixes(source, counts, weight, max_bytes=1024**2)
    assert engine.prepare("r", permutations)
    for record in counts:
        axes = {axis for values in source.values() for axis in values}
        matrix = {
            axis: math.fsum(
                source.get(("r", fleet, key.vehicle_id), {}).get(axis, 0)
                for fleet in sorted(permutations)
                for key in permutations[fleet][: record.count_by_fleet[fleet]]
            )
            for axis in axes
        }
        expected = math.fsum(
            weight(axis) * pointwise_utility(value, kind, 300) for axis, value in matrix.items()
        )
        total, utility, nonzero = engine.evaluate(record.count_by_fleet, kind, 300)
        assert total == pytest.approx(math.fsum(matrix.values()), rel=1e-14)
        assert utility == pytest.approx(expected, rel=1e-14, abs=1e-15)
        assert nonzero == sum(v > 0 for v in matrix.values())
    constrained = IndexedFleetPrefixes(source, counts, weight, max_bytes=1)
    assert not constrained.prepare("r", permutations)


def test_on_demand_keeps_sample_utilities_and_reconstructs_all_statistics(tmp_path):
    exposure = _anticorrelated_exposure(tmp_path)
    config = _portfolio_config(
        exposure,
        {"fleet_a": (0, 1), "fleet_b": (0, 1)},
        sampling_rounds=12,
        sampling_seed=61,
        budgets=(0, 10, 20),
    ).model_copy(update={"sample_matrix_storage": "reconstruct"})
    app = HeadlessApplication(tmp_path)
    samples = app.evaluate_portfolio_samples(exposure, config, _uniform_weights())
    eager = app.summarize_portfolios(samples.reference, config)
    lazy = app.summarize_portfolios(
        samples.reference, config.model_copy(update={"sensing_statistics_mode": "on_demand"})
    )
    left, right = [
        PortfolioAnalysisArtifactReader(tmp_path, value.reference) for value in (eager, lazy)
    ]
    assert right.read("portfolio_sensing_statistics").num_rows == 0
    metadata = right.read("portfolio_analysis_metadata").to_pylist()[0]
    assert (
        metadata["sensing_statistics_mode"] == "on_demand"
        and not metadata["absent_sparse_rows_are_zero"]
    )
    for table in ("portfolio_statistics", "budget_frontiers"):
        assert left.read(table).to_pylist() == right.read(table).to_pylist()
    portfolio = next(
        row["portfolio_id"]
        for row in right.read("portfolio_counts").to_pylist()
        if sum(json.loads(row["count_by_fleet_json"]).values()) == 2
    )
    for statistic in ("mean", "variance", "std"):
        for aggregate in ("bins", "sum"):
            results = []
            for analysis in (eager, lazy):
                request = MatrixQueryRequest(
                    resource_id=analysis.reference.artifact_id,
                    kind="portfolio_summary",
                    statistic=statistic,
                    portfolio_id=portfolio,
                    temporal_aggregation=aggregate,
                )
                results.append(query_matrix(tmp_path, request, JobStoreLimits()))
            assert results[0]["values"] == results[1]["values"]
            assert results[0]["overall_value"] == results[1]["overall_value"]
            assert results[0]["time_summary"] == results[1]["time_summary"]
    # Correct covariance: this fixture's total across both bins is constant.
    assert results[1]["overall_value"] == pytest.approx(0, abs=1e-12)


def test_cached_budget_repricing_skips_sampling_but_utility_changes_recompute(
    tmp_path, monkeypatch
):
    import mobile_sensing.application.run_pipeline as pipeline

    root, config = config_fixture(tmp_path)
    kwargs = dict(
        options=RunOptions(),
        cancellation=Cancellation(),
        progress=RecordedProgress(),
        source_revision_id=None,
    )
    run = run_project(root, config, name="Operating input", **kwargs)
    editor = PortfolioEditor(
        source_run_id=run.run_id,
        sampling_runs=5,
        budgets=(0, 1, 2),
        fleets=(PortfolioFleetEditor(fleet_id="f", counts=(0, 1, 2), unit_cost=1),),
    )
    first = run_analysis(root, editor, name="Initial", **kwargs)
    original = pipeline.publish_portfolio_samples
    calls = []

    def traced(**arguments):
        calls.append(True)
        return original(**arguments)

    monkeypatch.setattr(pipeline, "publish_portfolio_samples", traced)
    second = run_analysis(
        root, editor.model_copy(update={"budgets": (0, 0.5, 1)}), name="Reduced budget", **kwargs
    )
    assert first.samples == second.samples and not calls
    assert first.count_portfolios == 3 and second.count_portfolios == 2
    # A portable example/copy must retain the same bounded reusable cache.
    from mobile_sensing.jobs import JobStore
    from mobile_sensing.jobs.workspace import clone_project

    store = JobStore(root)
    project = store.create_project("Portable analysis", "")
    linked = config.model_copy(
        update={"linked_run_ids": (run.run_id,), "linked_analysis_ids": (first.analysis_id,)}
    )
    store.create_revision(project.project_id, None, linked.model_dump(mode="json"))
    destination = tmp_path / "Copied analysis"
    clone_project(store, project.project_id, destination, new_identity=True)
    copied = run_analysis(destination / ".system", editor, name="Portable reuse", **kwargs)
    assert copied.samples == first.samples and copied.frontier == first.frontier and not calls
    repeated = run_analysis(root, editor, name="Reopened analysis", **kwargs)
    assert repeated.frontier == first.frontier and not calls
    changed = run_analysis(
        root,
        editor.model_copy(update={"saturation_minutes": 10.0}),
        name="Changed utility",
        **kwargs,
    )
    assert changed.samples != first.samples and len(calls) == 1
    a, b = [PortfolioArtifactReader(root, value.samples) for value in (first, changed)]
    assert a.read("sample_selection").to_pylist() == b.read("sample_selection").to_pylist()


def test_numerical_budget_fallback_retains_exact_random_design(tmp_path, monkeypatch):
    exposure, _ = _two_vehicle_exposure(tmp_path)
    config = _portfolio_config(exposure, {"fleet": (0, 1, 2)}, sampling_rounds=8).model_copy(
        update={"sample_matrix_storage": "reconstruct"}
    )
    arguments = dict(
        artifact_root=tmp_path, exposure=exposure, config=config, weights=_uniform_weights()
    )
    indexed = evaluate_samples(**arguments)
    monkeypatch.setattr(IndexedFleetPrefixes, "prepare", lambda *a: False)
    reference = evaluate_samples(**arguments)
    assert indexed.sample_rows == reference.sample_rows
    assert indexed.selection_rows == reference.selection_rows
    assert not indexed.matrix_rows and not reference.matrix_rows


@pytest.mark.parametrize("seed", range(5))
def test_native_bulk_routing_matches_independent_string_heap_with_parallel_ties(seed):
    import heapq
    import geopandas as gpd
    import pandas as pd
    from shapely.geometry import Point, LineString
    from mobile_sensing.environment.network import PreparedRoutingService

    rng = np.random.default_rng(seed)
    names = [f"n{i}" for i in range(43)]
    coordinates = {name: (2500000 + i, 1100000) for i, name in enumerate(names)}
    nodes = gpd.GeoDataFrame(
        {
            "node_id": names,
            "x_m": [coordinates[n][0] for n in names],
            "y_m": [coordinates[n][1] for n in names],
        },
        geometry=[Point(coordinates[n]) for n in names],
        crs=2056,
    )
    rows = []
    weights = []
    for i, name in enumerate(names):
        for j in sorted({(i + 1) % len(names), int(rng.integers(len(names)))}):
            if i == j:
                continue
            for parallel in range(2):
                edge = f"edge{i}_{j}_{parallel}"
                rows.append(
                    {
                        "edge_id": edge,
                        "u_node_id": name,
                        "v_node_id": names[j],
                        "length_m": 1.0,
                        "geometry": LineString([coordinates[name], coordinates[names[j]]]),
                    }
                )
                weight = (
                    float(rng.choice([0.1, 0.2, 0.3, 1.0, 1.1]))
                    if parallel == 0
                    else weights[-1]["duration_s"]
                )
                weights.append({"profile_id": "p", "edge_id": edge, "duration_s": weight})
    router = PreparedRoutingService(
        nodes,
        gpd.GeoDataFrame(rows, crs=2056),
        pd.DataFrame(weights),
        network_hash="a" * 64,
        profile_hashes={"p": "b" * 64},
    )
    for source in names[::11]:
        best = {source: (0.0, ())}
        heap = [(0.0, (), source)]
        while heap:
            duration, path, node = heapq.heappop(heap)
            if best[node] != (duration, path):
                continue
            for target, edge, weight in router._adjacency["p"][node]:
                candidate = (duration + weight, path + (edge,))
                if target not in best or candidate < best[target]:
                    best[target] = candidate
                    heapq.heappush(heap, (candidate[0], candidate[1], target))
        native = router._native_lexicographic_paths(
            "p", source, {name: router._node_index[name] for name in names}
        )
        assert native == best
        assert router._search_from("p", source, names + ["missing"]) == best
        for target, total in router.travel_times_from("p", source, names).items():
            assert total == router.route("p", source, target).total_duration_s
