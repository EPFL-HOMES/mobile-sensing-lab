# Contributing

Contributions should preserve the scientific contracts and reproducibility guarantees described in `AGENTS.md` and `docs/`.

## Development environment

Use Python 3.12 and Node.js 22.12–22.x:

```bash
poetry install --all-extras
npm --prefix frontend ci
npm --prefix frontend run build
```

Production Python is under `src/mobile_sensing/`. Browser code is under `frontend/src/`. Current tests are under `tests/v2/`. Raw research inputs, reference implementations, user workspaces, and generated results are intentionally outside the public source tree.

## Contracts

`docs/INTERFACES.md` owns serialized names. Python schemas generate `frontend/src/api/generated.ts`; do not edit the generated TypeScript file by hand.

After changing a public model, regenerate and verify the OpenAPI and TypeScript contracts using the generator in `mobile_sensing.api.generate_contracts`, then update the checked fixture only after reviewing the semantic change. Canonical scientific fixtures and checksums are under `tests/v2/fixtures/contracts/`.

## Required checks

```bash
python -m pytest -q tests/v2
npm --prefix frontend test
npm --prefix frontend run typecheck
npm --prefix frontend run build
ruff check src/mobile_sensing tests/v2
black --check src/mobile_sensing tests/v2
poetry check --lock
poetry build
```

Some geographic tests require the original immutable Lausanne inputs and skip when those inputs are unavailable. Record every skip and do not describe an unexecuted check as passing.

## Documentation

Update `README.md` and `docs/QUICKSTART.md` for user-visible workflow changes. Update the corresponding scientific specification for semantic changes and `docs/INTERFACES.md` for serialized fields. Keep `docs/IMPLEMENTATION_STATUS.md` concise: current release state, executed validation, known limitations, and compatibility notes.

## Pull requests

Describe the concrete trigger and resulting behavior, list contract or migration effects, and include the checks actually executed. Keep raw datasets, generated workspaces, credentials, large example archives, and validation scratch directories out of commits.

## Preparing a release

Run `scripts/prepare_release.sh` from a checkout containing the four matching example manifest/archive files. The script executes the release checks, rebuilds the browser bundle and Python distributions, and writes upload-ready assets plus `SHA256SUMS` under `release/<version>/`.

The Git repository contains source, tests, lock files, notebooks, public documentation, mapping templates, and the small example manifests. GitHub Release assets contain the wheel, source distribution, both example JSON/ZIP pairs, and checksum file. Do not commit `data/`, `ref/`, `results/`, `project/`, virtual environments, frontend dependencies, built browser files, distribution files, example ZIP archives, or the local release-staging directory.
