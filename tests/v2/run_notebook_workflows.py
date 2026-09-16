"""Execute the joint tutorial, capturing evidence outside its temporary workspace.

The notebook is never rewritten. This verifies actual IPython cell execution and
inline figures; it does not claim a graphical notebook-editor inspection.
"""

import argparse
import base64
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path


def small_joint_configuration(namespace):
    from mobile_sensing.application.project_models import ProjectConfig

    settings = namespace["configuration"].model_dump(mode="json")
    settings["simulation"]["replications"] = 1
    for fleet in settings["fleets"]:
        if fleet["fleet_id"] == "bus":
            fleet["demand"]["route_ids"] = ["92-3-S-j26-1"]
        else:
            fleet["demand"]["task_volume"] = 8.0
            fleet["supply"]["fleet_size"] = 2 if fleet["fleet_id"] == "postal" else 4
            for index, group in enumerate(fleet["supply"]["shift_groups"]):
                group["count"] = 1 if index == 0 else 3
            if fleet["fleet_id"] == "postal":
                fleet["supply"]["capacity"] = 4.0
    namespace["configuration"] = ProjectConfig.model_validate_json(json.dumps(settings))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", choices=("default", "custom"), default="default")
    parser.add_argument(
        "--evidence-root", type=Path, default=Path("results/demonstration-release/notebooks")
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    os.chdir(root)
    os.environ.setdefault("MPLBACKEND", "Agg")
    from IPython.core.interactiveshell import InteractiveShell
    from IPython.utils.capture import capture_output

    target = args.evidence_root / (
        args.variant + "-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    )
    target.mkdir(parents=True, exist_ok=False)
    path = root / "notebooks/lausanne_simulation_tutorial.ipynb"
    before = path.read_bytes()
    notebook = json.loads(before)
    assert all(
        not cell.get("outputs") and cell.get("execution_count") is None
        for cell in notebook["cells"]
    )
    shell = InteractiveShell.instance()
    shell.reset(new_session=True)
    images = 0
    started = time.perf_counter()
    executed = []
    try:
        with (target / "execution.log").open("w") as log:
            for cell in notebook["cells"]:
                if cell["cell_type"] != "code":
                    continue
                print("Executing", cell["id"], flush=True)
                with capture_output() as captured:
                    result = shell.run_cell("".join(cell["source"]), store_history=True)
                log.write(captured.stdout + captured.stderr)
                for rich in captured.outputs:
                    if "image/png" in rich.data:
                        value = rich.data["image/png"]
                        data = value if isinstance(value, bytes) else base64.b64decode(value)
                        (target / f"{cell['id']}.png").write_bytes(data)
                        images += 1
                if result.error_before_exec or result.error_in_exec:
                    raise RuntimeError(
                        f"Cell {cell['id']} failed; see {target / 'execution.log'}"
                    ) from (result.error_before_exec or result.error_in_exec)
                executed.append(cell["id"])
                if args.variant == "default" and cell["id"] == "imports":
                    source = shell.user_ns["source_run"]
                    assert (
                        next(
                            f for f in source.config.fleets if f.fleet_id == "postal"
                        ).demand.task_volume
                        == 2000
                    )
                    assert shell.user_ns["source_analysis"].config.risk_metric == "p05"
                if args.variant == "custom" and cell["id"] == "ride-settings":
                    small_joint_configuration(shell.user_ns)
                if args.variant == "custom" and cell["id"] == "portfolio-settings":
                    shell.user_ns["portfolio_editor"] = shell.user_ns[
                        "portfolio_editor"
                    ].model_copy(update={"sampling_runs": 5})
        namespace = shell.user_ns
        run, analysis = namespace["run"], namespace["analysis"]
        assert set(run.vehicle_counts) == {"bus", "postal", "ride_hailing"}
        assert analysis.source_run_id == run.run_id
        assert analysis.config.risk_metric == "p05" and analysis.config.saturation_minutes == 10
        assert analysis.config.spatial_weight == "uniform"
        assert not namespace["workspace"].exists()
        assert images == 4
        for fleet, (_, grid, daily, _) in namespace["fleet_views"].items():
            import numpy as np

            assert len(grid) == 17553 and len(daily) == 24
            np.testing.assert_allclose(
                grid.mean_per_vehicle_minutes * run.vehicle_counts[fleet],
                grid.mean_sensing_minutes,
                rtol=1e-14,
                atol=1e-12,
            )
            np.testing.assert_allclose(
                grid.mean_sensing_minutes.sum(), daily.mean_sensing_hours.sum() * 60, rtol=1e-12
            )
        if args.variant == "default":
            assert run.run_id == namespace["source_run"].run_id
            assert analysis.analysis_id == namespace["source_analysis"].analysis_id
            assert run.replications == 10 and analysis.sampling_runs == 100
            from mobile_sensing.application.example_build import feasible_portfolio_count

            assert analysis.count_portfolios == feasible_portfolio_count(analysis.config)
        assert before == path.read_bytes()
        record = dict(
            passed=True,
            variant=args.variant,
            cells=executed,
            figures=images,
            replications=run.replications,
            sampling_runs=analysis.sampling_runs,
            run_id=run.run_id,
            analysis_id=analysis.analysis_id,
            selected_portfolio_count=len(namespace["selected_points"]),
            temporary_workspace_removed=True,
            notebook_outputs_empty=True,
            notebook_sha256=hashlib.sha256(before).hexdigest(),
            elapsed_seconds=time.perf_counter() - started,
        )
        (target / "execution.json").write_text(json.dumps(record, indent=2) + "\n")
        print(json.dumps(record), flush=True)
    finally:
        if "temporary" in shell.user_ns:
            shell.user_ns["temporary"].cleanup()


if __name__ == "__main__":
    main()
