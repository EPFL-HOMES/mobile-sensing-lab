# Repository guidance for coding agents

This file defines the constraints that any coding assistant or external contributor must follow when changing the repository. User documentation is in `README.md` and `docs/QUICKSTART.md`.

## Read before changing code

- Read `docs/ARCHITECTURE.md` and the relevant scientific specification.
- Treat `docs/INTERFACES.md` as the authority for serialized names and external contracts.
- Never modify `docs/MOBILE_SENSING_FRAMEWORK.tex`; it is the frozen scientific framework.
- Keep `data/`, `ref/`, user projects, and retained scientific results immutable. Write derived outputs to an explicit artifact or temporary directory.
- Keep repository documents, code identifiers, comments, tests, and execution records in English.

## Scientific invariants

- Use one task-driven, vehicle-agent, discrete-event kernel. Fleet labels select configurations, not separate simulators.
- Physical supply is independent of sensor portfolios. Keep the vehicle catalog fixed across joint operational replications.
- Preserve sparse physical-vehicle × cell × reporting-bin exposure for each complete replication.
- Evaluate nonlinear utility inside each sampled realization before computing empirical statistics.
- Keep operational replications `R` distinct from portfolio sampling rounds `J`. Each portfolio round samples one joint replication and concrete vehicles uniformly from the complete catalog.
- Treat a frontier point as a count portfolio, not a best observed allocation.
- Keep service-area geometry separate from vehicle-to-area assignment; never infer an assignment from the depot alone.
- Preserve directed road-edge identity, ordered half-open time intervals, exposure conservation, semantic random streams, and stable tie-breaking.
- Label synthetic demand, inferred duties, assumed speeds, heuristic search, and in-sample analysis explicitly.

## Engineering boundaries

- Production Python belongs under `src/mobile_sensing/`; browser source belongs under `frontend/`.
- Keep scientific calculations in Python. Do not add scientific estimators to the frontend.
- Unsupported configurations must fail explicitly.
- Avoid dense replication × vehicle × grid × time arrays, unbounded result payloads, and unconditional fleet-wide pair matrices.
- Run long jobs outside the API process and preserve cancellation, resource limits, lease ownership, and atomic artifact publication.
- Generate TypeScript contracts from Python schemas. Update schemas, generated files, and contract tests together.
- Add dependencies only when the current capability requires them and document the reason.
- Do not weaken meaningful tests. Classify compatibility changes and preserve scientific regression coverage.

## Change workflow

1. Inspect the existing implementation and current validation record before editing.
2. Define one bounded change and identify its governing interface and scientific specification.
3. Implement the smallest complete change, including migrations and contract updates when required.
4. Run focused checks, then the complete applicable Python and frontend suites.
5. Update public documentation, `docs/IMPLEMENTATION_STATUS.md`, and `docs/IMPLEMENTATION_PLAN.md` when behavior or open work changes.
6. Report executed checks, measured results, assumptions, and remaining limitations separately.

Do not import or execute source from `ref/` in production. Do not add Streamlit, Dash, or Panel. Do not create fleet-specific simulation kernels.
