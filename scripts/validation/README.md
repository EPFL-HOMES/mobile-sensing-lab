# Manual validation tools

Run from the repository root in the development environment with `python -m scripts.validation.<name>`. These maintainer tools are not App installation steps and are not collected by pytest.

| Module | Purpose and prerequisites |
|---|---|
| `install_offline` | Install a built wheel into isolated environments using a verified dependency-wheel cache |
| `run_release_workflow` | Run the local Lausanne workflow with networking disabled; requires original local data |
| `verify_browser_results` | Verify a prepared workspace's browser-facing/API results and cancellation records |
| `measure_result_queries` | Measure and cross-check queries against an existing acceptance workspace |
| `run_notebook_workflows` | Execute default/custom notebooks using installed example assets |
| `verify_example_bundle` | Audit an example bundle and retained results |

Use `--help` for required paths and arguments. Write generated outputs outside the source tree or under ignored `results/`; never overwrite immutable inputs.

Shared acceptance-workspace construction and simulation comparison stay under `tests/support/` because regression tests import them. Their module entry points are `python -m tests.support.build_acceptance_workspace --help` and `python -m tests.support.compare_simulation_runs --help`. Contract generation remains `python -m tests.support.generate_contract_fixtures`.

The full release gate is [prepare_release.sh](../prepare_release.sh). See [CONTRIBUTING.md](../../CONTRIBUTING.md) for normal checks.
