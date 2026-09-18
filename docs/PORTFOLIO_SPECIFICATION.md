# Portfolio Random Enumeration and Analysis Specification

## MR33 extension: utility temporal interval

The reporting time axis retained by exposure remains independent from the utility evaluation axis. Let \(B\) be the ordered reporting bins and let \(Q\) be an ordered partition in which every utility interval is a union of complete reporting bins. For every sampled portfolio matrix,

\[
S^{(j)}_{g,q}=\sum_{t\in q}S^{(j)}_{g,t},\qquad
U_j=\sum_{g,q}w_{g,q}\phi(S^{(j)}_{g,q};\tau).
\]

Aggregation occurs inside each sample, before nonlinear utility. Reporting-bin sensing maps and time summaries are retained unchanged. An interval boundary that cuts a reporting bin is rejected because the sparse reporting-bin matrix cannot identify the within-bin split. `temporal_interval_s=null` preserves historical per-reporting-bin evaluation; schema 3.3 authoring resolves a positive interval explicitly. The full-day examples use one 1,440-minute interval over 24 hourly reporting bins, so utility has spatial but no within-day temporal differentiation.

## MR26 bounded computation and on-demand maps

New web/tutorial analyses compute all P×J scalar utilities and retain the complete random design before publishing scalar statistics and frontiers. Cell/bin statistics are optional, explicitly identified by `sensing_statistics_mode`; an uncomputed table does not mean zero coverage. Requested means, variances and coverage probabilities are reconstructed from the same empirical selections. Whole-window variances use within-sample temporal sums, retaining cross-bin covariance.

The indexed evaluation path caches at most two compressed replication axes and one round's fleet-prefix matrices, bounded by a 64 MiB numerical workspace and the configured remaining memory allowance. It never creates an R×vehicle×grid×time array. Counts use the existing sampled permutations; pointwise utility is applied after combining selected vehicle exposure inside each draw. Float64 vector reductions may differ from reference `math.fsum` at roundoff scale; paired samples, objective comparison keys, and frontiers are independently checked and the algorithm version is recorded. Exceeding the bounded numerical workspace uses the existing sparse fsum implementation of the same estimator.

## MR17 extension: lower-tail utility objective

For retained sample utilities sorted as u_(0) <= ... <= u_(J-1), set h=0.05(J-1), k=floor(h), and P05=(1-h+k)u_(k)+(h-k)u_(min(k+1,J-1)). The requested risk view uses P05, not the conditional mean below the quantile and not a guaranteed minimum. Under `risk_metric=p05`, portfolio a dominates b iff its quantized mean and P05 are both at least those of b and at least one is strictly greater. Retain all exact objective ties. Sort descending P05 and descending mean, grouping equal P05, then scan maximal means: O(P_B log P_B). `std` preserves the historical objective. Comparison resolution and metric are recorded in immutable analysis identity. J<2 disables frontiers for both objectives; quantiles and the observed mean remain available. P05 is conditional on the retained joint operational pool and random vehicle selections; J=100 gives limited lower-tail precision. Nonlinear utility remains inside each sample and is never evaluated on the mean map.

Status: normative implementation baseline, 2026-09-07. The user selected resolution-controlled enumeration of fleet sensor counts, random vehicle allocation with a configurable sampling-round limit, sensing-duration matrices and utility for each sample, and mean–standard-deviation Pareto frontiers under different budgets. No optimization solver or selection based on known vehicle quality is required.

## 1. Decision and uncertainty model

The user-facing portfolio is a fleet count vector

\[
n=(n_1,\ldots,n_K),\qquad 0\le n_k\le N_k,
\]

where \(N_k=|V_k|\) is the fixed operational population of fleet \(k\). A physical vehicle is a particular simulated agent, not a route or a count. Installation locations are assumed unknown: all vehicles in \(V_k\) are eligible for uniform sampling. For each random allocation,

\[
X_k\sim\operatorname{Uniform}\{A\subseteq V_k:|A|=n_k\}.
\]

Sample without replacement within a fleet/round; rounds are independent and may repeat the same subset. The operational fleet remains fixed. Changing sensor counts or sampling rounds never changes mobility. Do not preferentially select vehicles with large exposure, known route quality, long shifts, or activity in a particular replication. Inactive vehicles remain in the catalog and contribute zero when sampled.

The framework's binary physical-vehicle indicator is preserved within each sampled allocation: \(x_{k,v}=1\) iff \(v\in X_k\). The product adds a random-allocation analysis around that interface; it does not alter the framework or the simulation. A count portfolio is therefore a distribution over possible installed subsets, not a known fixed installed subset.

## 2. Distinguish replications from sampling rounds

- \(R\): number of complete joint operational simulation replications already retained in an exposure artifact.
- \(J\): number of random-allocation evaluation rounds per count portfolio, controlled by `sampling_rounds`. This is the configured hard round limit; v1 executes exactly this many rounds and has no data-dependent early stopping.
- \(P\): number of feasible count portfolios on the enumeration grid.

Do not multiply \(R\) and \(J\) and label all resulting observations independent. Use the following explicit default sampling experiment instead. For each round \(j\):

1. Draw one joint replication ID \(I_j\) uniformly with replacement from the complete \(R\) replication IDs.
2. Independently generate one uniform random permutation \(\pi_{k,j}\) of the full stable vehicle catalog of each fleet.
3. For count vector \(n\), select the first \(n_k\) vehicles in \(\pi_{k,j}\), giving \(X_{k,j}(n)\).
4. Calculate and retain the sample matrix and utility:

\[
S^{(j)}_{g,t}(n)=\sum_k\sum_{v\in X_{k,j}(n)}E^{(I_j)}_{k,v,g,t},
\qquad U_j(n)=\Phi(S^{(j)}(n)).
\]

Each sample uses one entire joint replication across all fleets. Never select a separate replication independently for each vehicle/fleet: that destroys the joint operational scenario. Replication selection and vehicle permutations use independent named RNG streams. Different rounds have independent draws conditional on the retained dataset.

Use common random numbers across count portfolios: round \(j\) uses the same \(I_j\) and per-fleet permutations for every count vector. Prefix selection is nested when counts increase and uniform over subsets of each fixed size. This improves paired comparisons without changing any portfolio's marginal sampling distribution. It does not assume known vehicle quality. Across rounds, vehicle identities vary under the unknown-installation assumption.

For the supported nonnegative monotone utilities, if n' is componentwise at least n, shared prefix draws imply `S_j(n') >= S_j(n)` elementwise and `U_j(n') >= U_j(n)` in every round. Use this as a deterministic implementation invariant. Standard deviation has no corresponding monotonicity guarantee; do not force it to increase or decrease with vehicle count.

Default `sampling_rounds=100`, editable before execution; this is an illustrative computational default, not an accuracy guarantee. `sampling_seed` is explicit and independent of the mobility seed. Seed keys include round ID, stream purpose, fleet ID, and catalog hash, but exclude budget, count resolution, cost, utility, UI order, job ID, and worker count. Increasing J preserves the previous sample prefix and appends rounds in a new immutable analysis. Changing enumeration resolution reuses the same rounds for existing count vectors.

## 3. Estimand and interpretation

Conditional on retained exposure data \(\mathcal E\), the target is the joint empirical scenario/random-installation distribution:

\[
\mu_{\mathcal E}(n)=\frac1R\sum_{r=1}^{R}\mathbb E_X[\Phi(S^{(r)}(X(n)))],
\qquad
\sigma_{\mathcal E}^2(n)=\operatorname{Var}_{I,X}[\Phi(S^{(I)}(X(n)))].
\]

This combines operational scenario variability and unknown installation location. Its decomposition is

\[
\operatorname{Var}_{I,X}(U)=
\operatorname{Var}_{I}(\mathbb E_X[U\mid I])+
\mathbb E_I[\operatorname{Var}_X(U\mid I)].
\]

The first release reports the combined standard deviation; it does not claim to estimate the two terms separately from a crossed design. A fixed-installed-subset operational risk analysis is a future/secondary analysis mode with a different estimand and must not be mislabeled as this result.

With R=1, random vehicle allocation can still produce nonzero variability. Label that outcome as allocation variability conditional on one operational realization; more sampling rounds cannot create independent operational evidence. With multiple replications the mixture also includes their empirical operational variation. Sampling a replication with replacement is Monte Carlo evaluation of the retained empirical distribution, not generation of a new simulation replication.

Do not optimize, rank, or retain only the best allocation samples when computing a count portfolio's statistics. All J completed rounds contribute, including repeated subsets, inactive selections, and zero-utility samples. Deduplication may reuse a matrix computation, but must preserve each repeated draw's statistical multiplicity.

## 4. Input completeness and alignment

Consume complete sparse \(E^{(r)}_{k,v,g,t}\) from one compatible exposure artifact. Validate catalog, explicit grid/bin axes, sensing definition, horizon, scenario provenance, and complete replication IDs. Missing sparse entries mean zero only for certified complete partitions. Missing/failed replications are errors, not zero outcomes. Reject duplicate exposure keys, negative/non-finite durations, incompatible catalogs, and axis mixtures.

All fleet outcomes in replication r must have the same joint scenario identity. Never reconstruct a joint experiment by joining independently generated fleet rows on row position. No covariance matrix or independence assumption is needed in the evaluator: use the joint retained rows directly.

## 5. Utility functions and numerical rules

Default weighted exponential saturation:

\[
\Phi(S)=\sum_{g,t}w_{g,t}\bigl(1-e^{-S_{g,t}/\tau_{g,t}}\bigr),
\quad w_{g,t}\ge0,\quad\sum_{g,t}w_{g,t}=1,\quad\tau_{g,t}>0.
\]

For new `sample-utility@4` artifacts, configured `saturation_s` is the 99% duration D, with tau = D / ln(100) in the framework formula above. Local utility is `-expm1(-ln(100) * S / D)`, equals 0.99 at D, and approaches 1 asymptotically. Exposure and D are seconds; utility is dimensionless in [0,1]. Historical sample-utility versions 1–3 interpret `saturation_s` as tau and retain their original results. The algorithm-version change invalidates new-evaluation caches without rewriting historical artifacts. Capped-linear and binary utilities are unchanged. Validate finite positive saturation and nonnegative exposure. Large arguments should saturate safely; never clip materially invalid data into a valid range.

Weight sources: uniform, population × temporal weights, or uploaded cell-bin weights. Default temporal raw weights are bin durations. Normalize once on the declared evaluation domain. All-zero raw weights fail validation. Missing weights require an explicit policy; cells never reached by roads remain in the domain unless the user supplies an evaluation mask. Do not renormalize only onto cells reached by the current portfolio.

An optional linear diagnostic utility is \(\sum_{g,t}w_{g,t}S_{g,t}/\tau_{g,t}\), dimensionless and unbounded. It checks additivity; it is not the default sensing objective.

Compute \(U_j\) from each sample matrix, then statistics. In general,

\[
\frac1J\sum_j\Phi(S^{(j)})\ne\Phi\left(\frac1J\sum_j S^{(j)}\right).
\]

Never substitute pooled duration observations, the utility of a mean matrix, marginal fleet utilities, legacy moment/delta approximations, or sums of marginal variances. Changing grid/bins or utility weights defines a new analysis; reuse mobility/exposure where compatible but reevaluate nonlinear utility.

## 6. Sample statistics and matrix summaries

For each count portfolio:

\[
\widehat\mu(n)=\frac1J\sum_{j=1}^J U_j(n),\qquad
\widehat\sigma^2(n)=\frac1{J-1}\sum_{j=1}^J(U_j(n)-\widehat\mu(n))^2,
\qquad\widehat\sigma(n)=\sqrt{\widehat\sigma^2(n)}.
\]

Under the independent round design, sample variance is unbiased for the combined variance conditional on \(\mathcal E\); sample standard deviation is not generally unbiased. Report J and R separately. Mean Monte Carlo SE \(\widehat\sigma/\sqrt J\) describes finite sampling error conditional on retained replications. It does not quantify uncertainty about the underlying operational distribution from only R simulations. Optional confidence intervals must carry this limitation. v1 can simply report mean, std, variance, SE, min/max and p05/p50/p95 without confidence bands.

Quantiles use recorded linear interpolation. J=1 yields a valid observed mean/matrix but null variance/std/SE; disable mean–std frontiers until J≥2. Zero variance for identical samples is valid but not proof of real-world certainty. For R=1 and J≥2 a frontier is allowed with the one-operational-realization label.

For sensing maps also compute elementwise sample matrix means and sample variances over J, treating absent sparse cells as zero. These support duration mean/std maps; utility std is calculated from scalar sample utilities, not from summed cell variances. Use float64 stable accumulation; account for cancellation in sum-of-squares variance, use stable chunked/Welford methods, and check nonnegativity within numerical tolerance.

## 7. Count and budget resolution

Each fleet defines either an explicit sorted unique `count_levels` list or `min_count`, `max_count`, `step`, `include_max` (default true). These representations are mutually exclusive. For step mode:

\[
L_k=\{m_k+j\Delta_k:j\ge0,\ m_k+j\Delta_k\le M_k\}
\cup\{M_k\text{ if include_max}\}.
\]

Require integer counts, positive step, and \(0\le m_k\le M_k\le N_k\). Show the resolved levels, including a shorter last step if the upper endpoint was appended. A count-grid example is bus {0,2,4,6,8,10}, logistics {0,5,10,15,20}, taxi {0,10,20,30}, yielding 120 count vectors before budgets. There is no enumeration of all \(\binom{N_k}{n_k}\) installed subsets; sampling approximates each count vector's random-allocation distribution.

Budget settings likewise accept an explicit list or a min/max/step range with an explicit upper-endpoint rule. Count spacing, budget spacing, and sampling rounds are distinct controls. Use per-fleet nonnegative installation cost \(c_k\) in a declared currency/abstract unit and exact minor-unit scale:

\[
C(n)=\sum_k c_k n_k,\qquad C(n)\le B.
\]

V1 costs are constant within each fleet, consistent with unknown vehicle identity. Per-vehicle costs, exclusions, locked-in installations, and nonuniform sampling require a later explicit model extension; do not insert them as default controls. All simulated physical vehicles are eligible for sampling. A budget is an upper bound, not exact spending. Zero cost and unspent budget are valid.

Enumerate the count grid once and prune portfolios costing more than the maximum budget. Evaluate each remaining count portfolio for J rounds once; reuse statistics at every budget where it is feasible. Changing a budget within the evaluated range requires only feasibility/frontier recomputation. Expanding the range may require evaluating newly feasible count vectors, using the same exposure and round design, never rerunning mobility.

## 8. Resource controls and complexity

Preview \(P_0=\prod_k|L_k|\), the exact budget-feasible count P when affordable, planned scalar samples P×J, matrix storage estimates, exposure density, and estimated memory/work. Bound integer calculations before materializing product grids. Initial guards: at most 10,000 count portfolios and 1,000,000 portfolio-round records; actual matrix nnz/storage estimates and disk budget may impose tighter limits. These are safeguards, not performance promises. Display the selected J as a hard cap; no hidden adaptive stopping or silent reduction of rounds/resolution.

Each sample matrix can be sparse. Aggregate the sampled vehicles' exposure in the chosen joint replication, then compute utility. A dense G×T working matrix is allowed only within a memory budget; otherwise use sparse/chunked domain accumulation because supported utility is zero at zero exposure. No dense P×J×G×T or R×N×G×T default allocation. Stream matrix/utility records in bounded batches to Parquet.

Every sample matrix must be retained, as requested. Store unique matrix artifacts with sample-to-matrix references when identical `(exposure_id,replication_id,selected_ID_set)` recurs; preserve one round record per draw. Sparse empty matrices have explicit manifests. Do not replace sampled matrices with means/stds alone. Store per-round selected VehicleKeys or shared fleet-round orderings plus count-prefix references sufficient to recover every selection unambiguously.

A baseline sample evaluation is proportional to the selected vehicles' touched exposure plus utility accumulation. Prefix caches across count levels and common random numbers can reduce repeated work but must remain bounded. Do not promise linear runtime in count resolution. If predicted matrix storage exceeds the limit, block with options to increase count steps, reduce J, narrow the domain, or raise the resource budget explicitly. Do not silently drop sample matrices.

## 9. Budget-specific empirical Pareto frontier

For evaluated count portfolios \(\mathcal N\), let \(\mathcal N_B=\{n\in\mathcal N:C(n)\le B\}\). Portfolio m dominates n when

\[
\widehat\mu(m)\ge\widehat\mu(n),\qquad
\widehat\sigma(m)\le\widehat\sigma(n),
\]

with at least one strict inequality and both budget-feasible. Each plotted point is one count portfolio with sampling statistics, not one fortuitous allocation sample. An allocation sample can be inspected inside the point's details.

Record an objective comparison resolution (default 10^-12 utility units on each axis). Quantize mean/std to integer comparison keys using documented round-half-to-even, then perform exact dominance on keys. Preserve unrounded values for display/export. This gives deterministic transitive ties; avoid inconsistent pairwise epsilon dominance. Equal-objective count portfolios are ties; keep every ID in an inspectable group. Cost is feasibility for this two-dimensional view, not an unrequested third objective.

The empty portfolio has mean/std/cost zero when permitted and may be nondominated. Do not silently delete it. A larger budget can render old points dominated; do not force frontiers to be nested. Frontier membership is empirical on the enumerated count grid and finite random sample, not globally complete or statistically certified population dominance. Connecting points is a visual aid, not a claim that intermediate fractional portfolios are deployable.

For each budget, sort by ascending std key/descending mean key, group equal std, and scan maximal means at lower risk. Retain all exact objective ties. Complexity is O(P_B log P_B), not an O(P_B²) pairwise dominance loop. Sort by cost once to support repeated budget filtering. More advanced incremental frontiers are optional after profiling.

## 10. Artifacts and identity

- `portfolio_id`: count vector plus catalog hash; independent of budget and draw order.
- `round_id`: zero-based stable sample index tied to sampling design/seed and complete replication-set hash.
- `sample_id`: portfolio ID plus round ID and sampling-design identity.
- `matrix_id`: selected VehicleKeys, selected joint replication, exposure hash; permits storage reuse while preserving draw multiplicity.
- Analysis identity: exposure, utility/weights, count levels, costs/budgets, J, sampling seed/design, comparison resolution, and algorithm versions.

Persist round replication IDs, fleet-round permutations/prefix references, actual selected IDs or losslessly recoverable selections, sparse sample matrices, sample utility, count-portfolio scalar statistics, elementwise sensing statistics, budget feasibility/frontier membership, and full manifest. Scientific hashes exclude timestamp, worker count, and UI display filters.

Cancelled/failed sampling jobs cannot publish completed frontiers from the successful prefix. A later explicit partial-analysis mode would need a different status and estimand; it is deferred. J and R are recorded on every relevant result surface.

## 11. User interface and interpretation

Actions: `Preview Enumeration`, then `Evaluate Portfolios`. No `Optimize` button or risk-aversion slider is required. Configuration shows operational population read-only, count min/max/step or levels, cost per sensor vehicle, budget levels, sampling rounds J, sampling seed, R available, and utility settings. State the sampling rule: uniform from all physical vehicles; no replacement within a draw, independent draws across rounds.

Main plot: combined utility standard deviation on x, mean utility on y; select one budget or compare budgets as overlays/small multiples with common scales. Optional muted feasible-count points distinguish the full enumerated cloud from its frontier. Clicking a point shows count vector, installation cost, unspent budget, J/R, utility distribution, sensing mean/std maps, and a sample-round selector. The selected sample reveals its joint replication, exact vehicle selection, matrix and utility. Sample utility is not substituted for the point's mean.

The sample-based frontier is a research comparison conditional on the simulated model and empirical replication pool. Increasing J improves integration over this pool/allocation distribution; increasing R improves representation of operational scenarios. The UI must not imply that a large J compensates for too few independent mobility replications.

## 12. Acceptance tests

1. One fleet, two vehicles, one cell/bin, R=1, selecting one vehicle with exposures 0 and 2 and saturation 1: analytic mean over allocations is `(1-exp(-2))/2`, distinct from `1-exp(-1)`. Validate the evaluator exactly on a prescribed draw sequence, and convergence in a separately bounded statistical test with declared seed/tolerance.
2. Two fleets with anticorrelated exposures across R=2 yield zero combined operational variation when all vehicles are selected. A joint replication draw preserves this; independently drawing fleet replications would fail the test.
3. Every round samples exactly n_k distinct vehicles from each complete catalog. Inactive vehicles remain eligible. Repeated subsets across rounds retain multiplicity.
4. For a tiny catalog, prescribed permutations enumerate all equally likely subsets and match analytic moments. No ranking by exposure or best-sample filtering occurs.
5. Same round uses common replication/permutations across counts; increasing counts adds a prefix and increasing J preserves earlier samples. Changing budget/resolution/UI order/worker count does not change existing round outcomes.
6. Sample matrix conservation, sparse zero entries, retained matrix references, per-sample utility, sample mean/std, and elementwise duration summaries agree with an independent tiny reference.
7. R=1/J≥2 yields allocation variability with the correct label; J=1 yields null std and no mean–std frontier. Missing replication partitions are rejected.
8. Count/budget endpoints, exact costs, no-feasible-budget cases, zero portfolios, quantized ties and hand-computed frontiers match expectations.
9. Resource preview bounds P×J and matrix storage; exceeding limits blocks before heavy evaluation without silently altering the requested design.
10. No portfolio action executes mobility. Exported point statistics and its sampled matrices/vehicle selections reproduce one another.
