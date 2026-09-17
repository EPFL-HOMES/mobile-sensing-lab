# Tests and fixtures

Python tests are grouped by behavior. All 56 test modules and 290 collected cases are retained from the previous layout. Frontend tests remain beside their components in `frontend/src/`.

| Directory | Coverage |
|---|---|
| `contracts/` | Schemas, identifiers and scientific invariants |
| `environment/` | Networks, geometry, acquisition, features and service areas |
| `datasets/` | Normalization, generation, GTFS and local-data regressions |
| `simulation/` | Event ordering, task execution, dispatch and operational policies |
| `exposure/` | Sparse storage and fleet sensing statistics |
| `portfolio/` | Sampling, utility, frontiers and numerical optimizations |
| `application/` | Headless workflows, authoring, examples and notebook adapters |
| `api/` | HTTP contracts, reload, queries and exports |
| `jobs/` | Durable jobs, ownership, project folders and access |
| `packaging/` | Repository invariants, launcher, distributions and result comparison |
| `support/` | Shared fixture builders, contract generation and comparison helpers |
| `fixtures/` | Small inputs, schemas and expected scientific results |

## Run tests

From the repository root, with the development environment active:

```bash
python -m pytest -q tests
python -m pytest -q tests/simulation
```

The second command runs one group. CI and release preparation run the full suite. Native Windows runs the narrower [headless simulation check](../docs/INSTALLATION.md#windows-native-python). Six local Lausanne regressions skip when undistributed source data are absent; they run against the maintainer's immutable dataset when available. A skip is not a pass.

## Fixtures and tools

`fixtures/contracts/` contains byte-checked schemas and examples. Other fixture directories follow functional names. `fixtures/release_baselines/` stores expected outputs and migration evidence. These are validation inputs, not disposable run outputs. Historical schema versions and scientific IDs inside them are intentionally preserved.

Shared helpers live in `support/`. Run contract regeneration with `python -m tests.support.generate_contract_fixtures`, then review the changes. Manually invoked packaging, browser, notebook, performance and example audits live under [scripts/validation](../scripts/validation/README.md).

## Naming and maintenance

Use `test_<behavior>.py` for tests, descriptive nouns for fixtures, and `<action>_<object>.py` for tools. Do not add milestone numbers to filenames. User-editable input templates belong in [docs/templates](../docs/templates/README.md); test-only inputs belong here.

For a rename, update imports, fixture paths, CI selectors, scripts, documentation and notebooks together. Compare collected test IDs after mapping old paths to new paths, and run the applicable suite. Keep scientific assertions unchanged during naming cleanup.

Keep small fixtures and meaningful regressions in Git. Exclude caches, screenshots, generated reports, workspaces and large local datasets. Retire a test only after identifying replacement coverage or explicitly withdrawing the behavior it protects.
