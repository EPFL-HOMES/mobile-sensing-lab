import itertools

import numpy as np
import pytest

from mobile_sensing.application.project_models import PortfolioEditor
from mobile_sensing.portfolio.analysis import build_budget_frontiers, summarize_scalar_samples


def memberships(rows, metric, budgets=(10,)):
    return build_budget_frontiers(
        rows,
        budgets=budgets,
        unit="units",
        minor_unit_scale=1,
        mean_resolution=0.001,
        std_resolution=0.001,
        p05_resolution=0.001,
        replications_R=10,
        sampling_rounds_J=100,
        frontier_enabled=True,
        risk_metric=metric,
    )[1]


def test_p05_frontier_changes_dominance_and_ignores_std_in_objective_ties():
    rows = [
        dict(
            portfolio_id=name,
            utility_mean=mean,
            utility_sample_std=std,
            utility_p05=p05,
            total_cost_minor=1,
        )
        for name, mean, std, p05 in (
            ("A", 0.6, 0.1, 0.3),
            ("B", 0.6, 0.2, 0.4),
            ("C", 0.65, 0.05, 0.2),
            ("D", 0.6, 0.3, 0.4),
        )
    ]
    p05 = {row["portfolio_id"]: row for row in memberships(rows, "p05")}
    assert {key for key, row in p05.items() if row["nondominated"]} == {"B", "C", "D"}
    assert p05["B"]["tie_group_id"] == p05["D"]["tie_group_id"]
    assert {row["portfolio_id"] for row in memberships(rows, "std") if row["nondominated"]} == {"C"}


@pytest.mark.parametrize("metric", ["std", "p05"])
def test_sorted_frontier_matches_pairwise_oracle_with_budgets_and_ties(metric):
    rng = np.random.default_rng(710)
    rows = [
        dict(
            portfolio_id=str(i),
            total_cost_minor=int(cost),
            utility_mean=mean / 10,
            utility_sample_std=std / 10,
            utility_p05=p05 / 10,
        )
        for i, (cost, mean, std, p05) in enumerate(rng.integers(0, 10, size=(80, 4)))
    ]
    for budget in (0, 3, 9):
        feasible = [row for row in rows if row["total_cost_minor"] <= budget]

        def objectives(row):
            return row["utility_mean"], (
                row["utility_p05"] if metric == "p05" else -row["utility_sample_std"]
            )

        dominated = {
            b["portfolio_id"]
            for a, b in itertools.permutations(feasible, 2)
            if all(x >= y for x, y in zip(objectives(a), objectives(b)))
            and any(x > y for x, y in zip(objectives(a), objectives(b)))
        }
        observed = memberships(rows[::-1], metric, (budget,))
        assert {row["portfolio_id"] for row in observed if row["nondominated"]} == {
            row["portfolio_id"] for row in feasible
        } - dominated


def test_quantile_is_interpolated_boundary_and_historical_metric_is_std():
    values = [i / 99 for i in range(100)]
    summary = summarize_scalar_samples(values)
    assert summary.p05 == pytest.approx(0.05)
    assert summary.p05 != pytest.approx(np.mean(values[:5]))
    assert PortfolioEditor.model_validate_json("{}").risk_metric == "std"
    assert PortfolioEditor(risk_metric="p05").risk_metric == "p05"


def test_p05_analysis_reuses_saved_sample_utilities_and_api_reports_saved_metric(tmp_path):
    from tests.portfolio.test_portfolio_sampling import _two_vehicle_exposure, _portfolio_config
    from tests.portfolio.test_portfolio_analysis import _sample_reference
    from mobile_sensing.application import HeadlessApplication
    from mobile_sensing.portfolio import PortfolioAnalysisArtifactReader
    from mobile_sensing.api.queries import query_portfolio_frontier
    from mobile_sensing.jobs.models import JobStoreLimits

    exposure, _ = _two_vehicle_exposure(tmp_path)
    config = _portfolio_config(
        exposure, {"fleet": (0, 1, 2)}, sampling_rounds=8, sampling_seed=19, budgets=(0, 10, 20)
    )
    samples = _sample_reference(tmp_path, exposure, config)
    references = []
    for metric in ("std", "p05"):
        result = HeadlessApplication(tmp_path).summarize_portfolios(
            samples, config.model_copy(update={"risk_metric": metric})
        )
        reader = PortfolioAnalysisArtifactReader(tmp_path, result.reference)
        assert reader.sample_reference == samples
        budget = reader.read("budget_levels").to_pylist()[-1]
        view = query_portfolio_frontier(
            tmp_path,
            result.reference.artifact_id,
            budget_id=budget["budget_id"],
            limits=JobStoreLimits(),
        )
        assert view["risk_metric"] == metric
        assert view["max_mean_utility"] == max(p["utility_mean"] for p in view["points"])
        assert view["max_p05_utility"] == max(p["utility_p05"] for p in view["points"])
        assert view["frontier_portfolio_count"] == sum(p["nondominated"] for p in view["points"])
        assert reader.read("portfolio_analysis_metadata").to_pylist()[0]["risk_metric"] == metric
        references.append(result.reference)
    assert references[0] != references[1]
