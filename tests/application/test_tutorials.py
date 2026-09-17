"""Tutorial provenance gates and reuse through the real scientific pipeline."""

import pytest


from mobile_sensing.application import tutorial_workflow as tutorial
from mobile_sensing.application.project_models import PortfolioEditor, PortfolioFleetEditor
from mobile_sensing.application.run_models import RunOptions
from mobile_sensing.application.services import HeadlessApplication
from tests.application.test_runs_analysis import config_fixture


def test_tutorial_refuses_stale_results_and_reuses_mobility(tmp_path, monkeypatch):
    root, config = config_fixture(tmp_path)
    options = RunOptions()
    original = tutorial.simulation_result(root, config, None, recompute=True, options=options)
    assert (
        tutorial.simulation_result(root, config, original, recompute=False, options=options)
        is original
    )
    changed = config.model_copy(
        update={
            "simulation": config.simulation.model_copy(update={"temporal_resolution_minutes": 60.0})
        }
    )
    with pytest.raises(ValueError, match="RUN_SIMULATION"):
        tutorial.simulation_result(root, changed, original, recompute=False, options=options)

    def forbidden(*args, **kwargs):
        raise AssertionError("Reporting or portfolio changes reran mobility")

    monkeypatch.setattr(HeadlessApplication, "run_simulation", forbidden)
    hourly = tutorial.simulation_result(root, changed, original, recompute=True, options=options)
    assert hourly.simulation == original.simulation and hourly.exposure != original.exposure
    summary, grid, time, _ = tutorial.fleet_results(root, hourly, "f")
    assert summary["replications_R"] == 2
    assert time.start_s.is_monotonic_increasing
    assert grid.mean_sensing_minutes.sum() == pytest.approx(time.mean_sensing_hours.sum() * 60)
    with pytest.raises(ValueError, match="absent"):
        tutorial.fleet_results(root, hourly, "unknown")
    editor = PortfolioEditor(
        source_run_id=hourly.run_id,
        sampling_runs=3,
        budgets=(0.0, 1.0, 2.0),
        fleets=(PortfolioFleetEditor(fleet_id="f", counts=(0, 1, 2)),),
    )
    analysis = tutorial.analysis_result(root, editor, None, recompute=True, options=options)
    assert (
        tutorial.analysis_result(root, editor, analysis, recompute=False, options=options)
        is analysis
    )
    with pytest.raises(ValueError, match="RUN_PORTFOLIO"):
        tutorial.analysis_result(
            root,
            editor.model_copy(update={"sampling_runs": 5}),
            analysis,
            recompute=False,
            options=options,
        )
    result, statistics = tutorial.portfolio_results(root, analysis, budget=1.0)
    assert result["replications_R"] == 2 and result["sampling_rounds_J"] == 3
    assert statistics[statistics.feasible].total_cost.max() <= 1
    with pytest.raises(ValueError, match="saved budget"):
        tutorial.portfolio_results(root, analysis, budget=1.5)


def test_tutorial_queries_do_not_import_optional_http_runtime():
    import subprocess
    import sys

    subprocess.run(
        [
            sys.executable,
            "-c",
            "import mobile_sensing.application.tutorial_workflow; "
            "import sys; assert 'fastapi' not in sys.modules; "
            "assert 'uvicorn' not in sys.modules",
        ],
        check=True,
    )
