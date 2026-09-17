# Public development roadmap

The current release implements the complete local workflow from data registration through simulation, exposure analysis, sampled sensor portfolios, visualization, and portable export. Future work must preserve the frozen framework and contracts described in the technical specifications.

## Release maintenance

1. Keep Python 3.12 and Node 22 dependency locks reproducible.
2. Maintain byte-checked Python/OpenAPI/TypeScript contracts.
3. Validate wheel installation without a Node runtime.
4. Publish matching example manifests, archives, and checksums with each release.
5. Keep the public documentation consistent with actual application controls and supported platforms.
6. Maintain behavior-based names for templates, tests, fixtures and validation tools; update imports and documentation together when paths change.

## Reliability and performance

- Reduce cold large-region acquisition time and provide a documented local/offline road-network path.
- Bound cache growth and expose cache maintenance without changing scientific identities.
- Reduce first large portfolio-map query latency and large browser bundle sizes.
- Extend Linux browser and WSL2 end-to-end coverage beyond the automated checks.
- Preserve the native Windows synchronous Python smoke test; broaden scientific and multiprocessing coverage before claiming complete native Windows Python support.
- Replace Unix-only workspace/coordinator locking and decouple tutorial adapters before offering the native Windows App and bundled notebooks.

## Scientific extensions

Any extension requires an explicit contract, deterministic random-stream ownership, analytic regression cases, resource bounds, and migration behavior. Candidate areas include additional calibrated data sources, alternative dispatch policies through the existing executor boundary, and out-of-sample portfolio evaluation. Fleet-specific kernels and sensor-dependent operating supply remain outside the model.

## Acceptance for a new capability

A capability is complete only when:

- serialized fields and mathematical meaning are documented;
- unsupported cases fail explicitly;
- deterministic and numerical corner cases are covered;
- generated contracts are synchronized;
- bounded storage and computation are demonstrated;
- user-facing documentation identifies assumptions and limitations;
- the complete applicable Python and frontend suites pass.
