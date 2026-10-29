# Public development roadmap

The current release implements the complete local workflow from data registration through simulation, exposure analysis, sampled sensor portfolios, visualization, and portable export. Future work must preserve the frozen framework and contracts described in the technical specifications.

## Release maintenance

Completed: bound heatmap colour scales by the backend-computed positive-value 95th percentile, with manual upper limits and a data-maximum option. Keep scale metadata and controls in the presentation boundary, preserve raw scientific values and camera position, and apply the same scale to grid fills, source features and value-coloured road maps.

Completed: expose OD distributions as registered demand data and an explicit Fleet Configuration spatial source. Show the selected file, hide inactive marginal controls, preserve existing references and results, and reject missing/incompatible inputs without a Uniform fallback. Register cell-ID-only OD tables without a CRS; retain coordinate/geometry checks and historical record readability.

Completed: provide addable grid-filter rows with per-feature any/must logic, including Road network in either group. Retain the intersection of all required conditions and the union of alternatives when present. Preserve legacy any/all-plus-road semantics and historical artifacts. Apply the same Python filter to previews, generated support, exposure and source-run portfolios; preserve the full routing network and verify contracts, cache compatibility and persistence.

Completed: replace full event-heap scans with exact pruned lookahead and reuse only verified complete exposure artifacts. Record separate-process Lausanne timing and deterministic result comparisons; retain a read-only replication benchmark tool.

Completed: remove repeated waiting-pool sorting and complete-plan copying, reuse bounded reverse pickup trees and exact route caches, and avoid repeated infeasible cruising checks. Preserve scientific hashes and trajectories while coalescing idle diagnostics. Verify full-day replication speed separately from input resolution, publication and exposure allocation; retain complete Python/frontend regression gates.

Completed: place the budget-series chart below the utility frontier in a common-width column opposite Selected portfolio; retain hover-only composition and empirically derived percentile whiskers. Regenerate the Lausanne report's spatial-feature, full-budget frontier/sensitivity, and five-budget duration PNGs from retained artifacts without rerunning mobility.

Completed: add backend-owned fleet-subset frontier projections and utility/coverage budget-series presentation without mutating retained portfolio artifacts.

Completed: regenerated and audited the Lausanne example with five selected bus lines, `R=50`, four workers, `J=200`, five-sensor count/budget steps through 50 and P05-only risk analysis. Exposure allocation now reads one complete replication at a time and preserves the original canonical sparse-exposure hash, bounding the 50-replication build's peak working set.

Completed: keep large research and report artifacts outside Git; publish the current two city examples through checksum-verified optional release assets, with the Lausanne ZIP split into sub-2-GiB parts and reassembled by the source installer.

Completed: suppress zero-budget categories consistently in portfolio result presentation while retaining complete analysis data and exports.

Completed: connect frontier markers only within their displayed budget category, sharing color and legend visibility; leave singleton categories unconnected.

Maintain the versioned 99%-duration convention across the utility curve, both evaluators, caches, and city-example regeneration; keep historical results readable without relabeling their utility semantics.

Completed: expose per-round mean any-visit grid coverage in Selected portfolio using the existing backend statistic and zero-inclusive exposure domain.

Example upgrade acceptance must include an existing workspace with independently created equivalent resources, not only empty-workspace imports.

Completed bounded change: regenerated both immutable city-example bundles after validating fleet, spatial-feature and portfolio definitions. Both cities retain P05 and standard-deviation analyses with five-minute saturation. Strict saturation-map filtering, at-least-saturation reporting and minute heatmap legends are covered by frontend regressions.

1. Keep Python 3.12 and Node 22 dependency locks reproducible.
2. Maintain byte-checked Python/OpenAPI/TypeScript contracts.
3. Validate wheel installation without a Node runtime.
4. Publish matching example manifests, archives, and checksums with each release.
5. Keep the public documentation consistent with actual application controls and supported platforms.
6. Maintain behavior-based names for templates, tests, fixtures and validation tools; update imports and documentation together when paths change.

## Reliability and performance

- Further reduce initial large exposure allocation and publication costs; the new exposure cache accelerates repeated identical builds. Measure the complete 50-replication/four-worker workflow separately before advertising an end-to-end speedup.

- Reduce cold large-region acquisition time and provide a documented local/offline road-network path.
- Bound cache growth and expose cache maintenance without changing scientific identities.
- Reduce first large portfolio-map query latency and large browser bundle sizes.
- Extend Linux browser and WSL2 end-to-end coverage beyond the automated checks.
- Preserve the native Windows synchronous Python smoke test; broaden scientific and multiprocessing coverage before claiming complete native Windows Python support.
- Replace Unix-only workspace/coordinator locking and decouple tutorial adapters before offering the native Windows App and bundled notebooks.

## Scientific extensions

Any extension requires an explicit contract, deterministic random-stream ownership, analytic regression cases, resource bounds, and migration behavior. Candidate areas include additional calibrated data sources, alternative dispatch policies through the existing executor boundary, and out-of-sample portfolio evaluation. Fleet-specific kernels and sensor-dependent operating supply remain outside the model.

Pending research idea retained from the retired revision note: a gravity-model demand generator. No implementation or scientific specification is defined yet.

## Acceptance for a new capability

A capability is complete only when:

- serialized fields and mathematical meaning are documented;
- unsupported cases fail explicitly;
- deterministic and numerical corner cases are covered;
- generated contracts are synchronized;
- bounded storage and computation are demonstrated;
- user-facing documentation identifies assumptions and limitations;
- the complete applicable Python and frontend suites pass.
