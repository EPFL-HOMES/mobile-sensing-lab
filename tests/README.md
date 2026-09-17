# Tests and validation tools

The current Python suite lives in `v2/`. The `mXX` and `mrXX` prefixes record historical implementation stages; they do not mean a test is obsolete. There are 56 `test_*.py` modules, plus shared fixtures and nine validation/helper scripts. Frontend tests are colocated with their components in `frontend/src/`.

## Find a test by behavior

| Behavior | Current test modules |
|---|---|
| Schemas, scientific invariants and dependency boundaries | `test_contract_*`, `test_m00_baseline.py` |
| Networks, inputs, GTFS and local Lausanne regression | `test_m02_*`, `test_m03_*`, `test_m04_*` |
| Event kernel, execution, dispatch and idle policies | `test_m05*`, `test_mr04_dispatch.py` |
| Exposure, headless application and portfolios | `test_m06_*`, `test_m07_*`, `test_m08*` |
| Jobs, HTTP, queries, exports and reload behavior | `test_m09_*`, `test_m10_*`, `test_m11_*` |
| Packaging, startup and result comparison | `test_m12_*` |
| Current project editor, workspace, temporal/spatial features and regressions | `test_mr*` |
| Notebook adapters and workflows | `test_notebook_workflow.py`, `test_mr15_tutorials.py` |

Run the Python suite from the repository root in the development environment:

```bash
python -m pytest -q tests/v2
```

The clean public checkout skips six tests that need local Lausanne source data. Keep those tests: they run when the maintainer's immutable dataset is present. A skip is not a pass. Native Windows currently runs the scoped headless check documented in [the installation guide](../docs/INSTALLATION.md#windows-native-python), not the full suite.

## What belongs in Git

- Keep behavioral tests, meaningful regressions, shared fixture builders, and small checked-in scientific/contract baselines.
- Keep expected JSON/schema files under `fixtures/`; they are test inputs and expected results, not disposable run outputs.
- Keep reproducible validation tools when they still verify a release, archive, notebook, or performance claim. Ordinary App users do not need to run them.
- Exclude caches, generated reports, screenshots, test workspaces, and large local datasets. The repository ignore rules cover these artifacts.

## Naming maintenance

New tests should use behavioral names such as `test_event_kernel.py`, `test_headless_application.py`, or `test_workspace_ownership.py`, without new milestone numbers. Existing names remain for now so imports, fixture paths, CI selectors, and documentation links stay valid.

A future naming-only cleanup should rename modules by behavior, update all references in the same change, and compare collected test cases before and after. For example, `test_m05a_event_kernel.py` can become `test_event_kernel.py`; `test_mr34_auto_service_areas.py` can become `test_auto_service_areas.py`. Do not change assertions or delete coverage in that cleanup.

The nine non-test helpers mix fixture generation, comparisons, acceptance workspaces, packaging checks, browser checks, notebook execution, and bundle auditing. Group them under a clearly named support/tools directory in a separate migration. `generate_contract_fixtures.py`, `m12_compare_runs.py`, and `build_m11_acceptance_workspace.py` are directly imported by tests; they cannot simply be removed.

Only retire a test or helper after identifying its replacement or establishing that its behavior is intentionally unsupported. A historical filename, lack of automatic collection, or overlap in the feature name is insufficient evidence for deletion.
