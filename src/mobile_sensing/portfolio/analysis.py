"""M08B stable sample summaries and budget-specific empirical frontiers."""

from __future__ import annotations

import itertools
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_EVEN
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from mobile_sensing.contracts import (
    ArtifactRef,
    PortfolioConfig,
    canonical_json_text,
    scientific_projection,
    stable_id,
)
from mobile_sensing.exposure import ExposureArtifactReader
from mobile_sensing.portfolio.storage import PortfolioArtifactReader


PORTFOLIO_STATISTICS_VERSION = "stable-sample-statistics@1"
PORTFOLIO_SENSING_STATISTICS_VERSION = "sparse-zero-welford-statistics@2"
PORTFOLIO_FRONTIER_VERSION = "quantized-budget-frontier@2"
PORTFOLIO_QUANTILE_METHOD = "linear-interpolation@1"
VARIANCE_DDOF = 1
MAX_INT64 = 2**63 - 1


@dataclass(frozen=True, slots=True)
class SampleStatistics:
    count: int
    mean: float
    sample_variance: float | None
    sample_std: float | None
    conditional_mean_se: float | None
    minimum: float
    maximum: float
    p05: float
    p50: float
    p95: float


@dataclass(frozen=True, slots=True)
class PortfolioAnalysisTables:
    sample_reference: ArtifactRef
    exposure_reference: ArtifactRef
    replications_R: int
    sampling_rounds_J: int
    variability_interpretation: str
    grid_cell_count: int
    reporting_bin_count: int
    resolved_budgets: tuple[int, ...]
    metadata_rows: tuple[dict[str, object], ...]
    statistic_rows: tuple[dict[str, object], ...]
    sensing_rows: tuple[dict[str, object], ...]
    budget_rows: tuple[dict[str, object], ...]
    frontier_rows: tuple[dict[str, object], ...]


def _linear_quantile(ordered: Sequence[float], probability: float) -> float:
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    if ordered[lower] == ordered[upper]:
        return ordered[lower]
    fraction = position - lower
    return math.fsum(((1.0 - fraction) * ordered[lower], fraction * ordered[upper]))


def summarize_scalar_samples(values: Iterable[float]) -> SampleStatistics:
    """Return deterministic float64 sample statistics using Welford accumulation."""

    materialized = tuple(float(value) for value in values)
    if not materialized or any(not math.isfinite(value) for value in materialized):
        raise ValueError("sample statistics require nonempty finite values")
    mean = 0.0
    m2 = 0.0
    for index, value in enumerate(materialized, start=1):
        delta = value - mean
        mean += delta / index
        m2 += delta * (value - mean)
    if m2 < 0.0:
        tolerance = 64.0 * math.ulp(max(1.0, abs(mean))) ** 2 * len(materialized)
        if m2 < -tolerance:
            raise ArithmeticError("stable sample variance became materially negative")
        m2 = 0.0
    variance = m2 / (len(materialized) - 1) if len(materialized) >= 2 else None
    std = math.sqrt(variance) if variance is not None else None
    ordered = tuple(sorted(materialized))
    return SampleStatistics(
        count=len(materialized),
        mean=mean,
        sample_variance=variance,
        sample_std=std,
        conditional_mean_se=std / math.sqrt(len(materialized)) if std is not None else None,
        minimum=ordered[0],
        maximum=ordered[-1],
        p05=_linear_quantile(ordered, 0.05),
        p50=_linear_quantile(ordered, 0.50),
        p95=_linear_quantile(ordered, 0.95),
    )


def quantize_objective(value: float, resolution: float) -> int:
    """Quantize one finite objective with decimal round-half-to-even semantics."""

    if not math.isfinite(value) or not math.isfinite(resolution) or resolution <= 0.0:
        raise ValueError("objective values must be finite and resolution must be positive")
    return int(
        (Decimal(str(value)) / Decimal(str(resolution))).to_integral_value(rounding=ROUND_HALF_EVEN)
    )


def _resolved_budgets(config: PortfolioConfig, maximum: int = 10_000) -> tuple[int, ...]:
    value = config.budgets
    if hasattr(value, "levels_minor"):
        result = tuple(value.levels_minor)
    else:
        quotient, remainder = divmod(value.max_minor - value.min_minor, value.step_minor)
        size = quotient + 1 + int(value.include_max and remainder != 0)
        if size > maximum:
            raise ValueError("budget levels exceed the M08B limit")
        levels = list(range(value.min_minor, value.max_minor + 1, value.step_minor))
        if value.include_max and levels[-1] != value.max_minor:
            levels.append(value.max_minor)
        result = tuple(levels)
    if len(result) > maximum:
        raise ValueError("budget levels exceed the M08B limit")
    return result


def build_budget_frontiers(
    statistic_rows: Sequence[Mapping[str, object]],
    *,
    budgets: Sequence[int],
    unit: str,
    minor_unit_scale: int,
    mean_resolution: float,
    std_resolution: float,
    replications_R: int,
    sampling_rounds_J: int,
    frontier_enabled: bool,
    risk_metric: str = "std",
    p05_resolution: float = 1e-12,
) -> tuple[tuple[dict[str, object], ...], tuple[dict[str, object], ...]]:
    """Build exact-key Pareto memberships in O(sum_B P_B log P_B)."""

    if risk_metric not in {"std", "p05"}:
        raise ValueError("Unsupported frontier risk metric")
    sorted_by_cost = sorted(
        statistic_rows, key=lambda row: (int(row["total_cost_minor"]), str(row["portfolio_id"]))
    )
    budget_rows: list[dict[str, object]] = []
    memberships: list[dict[str, object]] = []
    for budget in budgets:
        budget_id = stable_id(
            "budget", {"unit": unit, "minor_unit_scale": minor_unit_scale, "budget_minor": budget}
        )
        feasible = []
        for source_row in sorted_by_cost:
            if int(source_row["total_cost_minor"]) > budget:
                break
            row = dict(source_row)
            row["mean_comparison_key"] = str(
                quantize_objective(float(row["utility_mean"]), mean_resolution)
            )
            std = row.get("utility_sample_std")
            row["std_comparison_key"] = (
                str(quantize_objective(float(std), std_resolution)) if std is not None else None
            )
            row["risk_comparison_key"] = (
                str(quantize_objective(float(row["utility_p05"]), p05_resolution))
                if risk_metric == "p05"
                else row["std_comparison_key"]
            )
            feasible.append(row)
        frontier_ids: set[str] = set()
        if frontier_enabled:
            ordered = sorted(
                feasible,
                key=lambda row: (
                    (1 if risk_metric == "std" else -1) * int(str(row["risk_comparison_key"])),
                    -int(str(row["mean_comparison_key"])),
                    str(row["portfolio_id"]),
                ),
            )
            best_mean_at_lower_risk: int | None = None
            for _, group_iter in itertools.groupby(
                ordered, key=lambda row: int(str(row["risk_comparison_key"]))
            ):
                group = tuple(group_iter)
                group_best = max(int(str(row["mean_comparison_key"])) for row in group)
                if best_mean_at_lower_risk is None or group_best > best_mean_at_lower_risk:
                    frontier_ids.update(
                        str(row["portfolio_id"])
                        for row in group
                        if int(str(row["mean_comparison_key"])) == group_best
                    )
                best_mean_at_lower_risk = (
                    group_best
                    if best_mean_at_lower_risk is None
                    else max(best_mean_at_lower_risk, group_best)
                )
        budget_rows.append(
            {
                "budget_id": budget_id,
                "budget_minor": budget,
                "cost_unit": unit,
                "minor_unit_scale": minor_unit_scale,
                "replications_R": replications_R,
                "sampling_rounds_J": sampling_rounds_J,
                "feasible_portfolio_count": len(feasible),
                "frontier_portfolio_count": len(frontier_ids) if frontier_enabled else None,
                "frontier_enabled": frontier_enabled,
                "disabled_reason": None if frontier_enabled else "sampling_rounds_J_is_less_than_2",
            }
        )
        for row in feasible:
            mean_key = str(row["mean_comparison_key"])
            std_key = str(row["std_comparison_key"]) if frontier_enabled else None
            tie_group_id = (
                stable_id(
                    "objective_tie",
                    {
                        "mean_key": mean_key,
                        "mean_resolution": mean_resolution,
                        "risk_metric": risk_metric,
                        "risk_key": row["risk_comparison_key"],
                        "risk_resolution": (
                            p05_resolution if risk_metric == "p05" else std_resolution
                        ),
                    },
                )
                if frontier_enabled
                else None
            )
            memberships.append(
                {
                    "budget_id": budget_id,
                    "budget_minor": budget,
                    "portfolio_id": str(row["portfolio_id"]),
                    "replications_R": replications_R,
                    "sampling_rounds_J": sampling_rounds_J,
                    "total_cost_minor": int(row["total_cost_minor"]),
                    "unspent_minor": budget - int(row["total_cost_minor"]),
                    "feasible": True,
                    "nondominated": (
                        str(row["portfolio_id"]) in frontier_ids if frontier_enabled else None
                    ),
                    "tie_group_id": tie_group_id,
                    "mean_comparison_key": mean_key,
                    "std_comparison_key": std_key,
                    "risk_comparison_key": row["risk_comparison_key"] if frontier_enabled else None,
                }
            )
    return tuple(budget_rows), tuple(memberships)


def _exposure_reference(reader: PortfolioArtifactReader) -> ArtifactRef:
    dependency = next(
        (item for item in reader.artifact.manifest.dependencies if item.role == "exposure"), None
    )
    if dependency is None:
        raise ValueError("M08A sample artifact lacks its exposure dependency")
    return ArtifactRef(
        artifact_id=dependency.artifact_id,
        artifact_kind="exposure",
        content_hash=dependency.content_hash,
    )


def _source_config(reader: PortfolioArtifactReader) -> PortfolioConfig:
    resolved = reader.artifact.manifest.scientific_identity.resolved_config
    try:
        return PortfolioConfig.model_validate_json(canonical_json_text(resolved["portfolio"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("M08A sample artifact lacks a valid resolved PortfolioConfig") from exc


def _validate_analysis_config(source: PortfolioConfig, current: PortfolioConfig) -> None:
    invariant_fields = (
        "schema_version",
        "exposure_id",
        "utility",
        "count_enumeration",
        "sampling_rounds",
        "sampling_seed",
        "sampling_design",
    )
    source_data = scientific_projection(source)
    current_data = scientific_projection(current)
    changed = [name for name in invariant_fields if source_data[name] != current_data[name]]
    if changed:
        raise ValueError(
            "M08B may reuse samples only when these fields are unchanged: " + ", ".join(changed)
        )


def _portfolio_cost(counts: Mapping[str, object], config: PortfolioConfig) -> int:
    if set(counts) != set(config.costs.by_fleet_minor):
        raise ValueError("portfolio count vector does not align with configured fleet costs")
    cost = sum(int(counts[fleet]) * config.costs.by_fleet_minor[fleet] for fleet in counts)
    if cost > MAX_INT64:
        raise ValueError("portfolio cost exceeds signed 64-bit artifact storage")
    return cost


def _validate_reusable_count_coverage(
    reader: PortfolioArtifactReader,
    config: PortfolioConfig,
    count_rows: Sequence[Mapping[str, object]],
    budgets: Sequence[int],
) -> None:
    identity = reader.artifact.manifest.scientific_identity
    resolved = identity.resolved_config
    levels = resolved.get("resolved_count_levels")
    if not isinstance(levels, dict) or set(levels) != set(config.costs.by_fleet_minor):
        raise ValueError("M08A artifact lacks aligned resolved count levels")
    available = {str(row["portfolio_id"]) for row in count_rows}
    maximum_budget = max(budgets)
    fleet_ids = tuple(sorted(levels))
    for values in itertools.product(*(tuple(levels[fleet]) for fleet in fleet_ids)):
        vector = dict(zip(fleet_ids, values, strict=True))
        if _portfolio_cost(vector, config) > maximum_budget:
            continue
        portfolio_id = stable_id(
            "portfolio", {"catalog_hash": identity.catalog_hash, "count_by_fleet": vector}
        )
        if portfolio_id not in available:
            raise ValueError(
                "updated budgets/costs require a count portfolio absent from the M08A artifact; "
                "expand M08A sampling without rerunning mobility"
            )


def _sparse_axis_statistics(
    *,
    portfolio_id: str,
    samples: Sequence[Mapping[str, object]],
    matrix_values: Mapping[str, Mapping[tuple[str, str], float]],
    replications_R: int,
    sampling_rounds_J: int,
    max_rows: int | None = None,
) -> list[dict[str, object]]:
    # Welford over positive observations; merge the implicit zero group exactly.
    # State is O(touched cell/bin axes), independent of the number of rounds.
    touched = {}
    for sample in samples:
        for axis, duration in matrix_values[str(sample["matrix_id"])].items():
            if max_rows is not None and axis not in touched and len(touched) >= max_rows:
                raise ValueError(
                    "On-demand sensing statistics exceed the query row limit; narrow the cells or bins"
                )
            n, mean, m2 = touched.get(axis, (0, 0.0, 0.0))
            n += 1
            delta = duration - mean
            mean += delta / n
            m2 += delta * (duration - mean)
            touched[axis] = n, mean, m2
    rows = []
    for (cell_id, time_bin_id), (nonzero_count, nonzero_mean, m2) in sorted(touched.items()):
        mean = nonzero_mean * nonzero_count / sampling_rounds_J
        centered = (
            m2
            + nonzero_mean**2
            * nonzero_count
            * (sampling_rounds_J - nonzero_count)
            / sampling_rounds_J
        )
        variance = max(centered, 0.0) / (sampling_rounds_J - 1) if sampling_rounds_J >= 2 else None
        rows.append(
            {
                "portfolio_id": portfolio_id,
                "cell_id": cell_id,
                "time_bin_id": time_bin_id,
                "replications_R": replications_R,
                "sampling_rounds_J": sampling_rounds_J,
                "nonzero_sample_count": nonzero_count,
                "mean_duration_s": mean,
                "sample_variance_s2": variance,
                "sample_std_s": math.sqrt(variance) if variance is not None else None,
            }
        )
    return rows


def analyze_portfolio_samples(
    *,
    artifact_root: str | Path,
    samples: ArtifactRef,
    config: PortfolioConfig,
    cancellation=None,
    progress=None,
) -> PortfolioAnalysisTables:
    """Validate M08A lineage and derive all M08B tables without invoking mobility."""

    samples = ArtifactRef.model_validate(samples)
    config = PortfolioConfig.model_validate(config)
    reader = PortfolioArtifactReader(artifact_root, samples)
    required_versions = {
        "enumeration": "fleet-count-enumeration@1",
        "sampling": "joint-replication-uniform-vehicle@1",
    }
    versions = reader.artifact.manifest.scientific_identity.algorithm_versions
    if (
        versions.get("storage")
        not in {
            "portfolio-samples-parquet@1",
            "portfolio-samples-reconstruct@2",
        }
        or versions.get("sample_utility")
        not in {"sample-utility@1", "sample-utility@2", "sample-utility@3"}
        or any(versions.get(key) != value for key, value in required_versions.items())
    ):
        raise ValueError("M08B requires a compatible complete M08A sample artifact")
    source_config = _source_config(reader)
    _validate_analysis_config(source_config, config)
    exposure_reference = _exposure_reference(reader)
    if config.exposure_id != exposure_reference.artifact_id:
        raise ValueError("PortfolioConfig exposure_id differs from M08A exposure lineage")

    axes = ExposureArtifactReader(Path(artifact_root)).axes(exposure_reference)
    identity = reader.artifact.manifest.scientific_identity
    exposure_identity = axes["artifact"].manifest.scientific_identity
    if (
        identity.catalog_hash != exposure_identity.catalog_hash
        or identity.replication_set_hash != exposure_identity.replication_set_hash
        or identity.grid_axis_hash != exposure_identity.grid_axis_hash
        or identity.time_axis_hash != exposure_identity.time_axis_hash
    ):
        raise ValueError("M08A sample axes differ from the complete exposure dependency")

    metadata = reader.read("portfolio_metadata").to_pylist()
    count_rows = reader.read("portfolio_counts").to_pylist()
    sample_rows = reader.read("portfolio_samples").to_pylist()
    round_rows = reader.read("sampling_rounds").to_pylist()
    matrix_metadata = reader.read("sample_matrices").to_pylist()
    matrix_exposure = (
        reader.read("sample_exposure").to_pylist()
        if config.sensing_statistics_mode == "eager"
        else []
    )
    selection_rows = reader.read("sample_selection").to_pylist()
    if len(metadata) != 1 or not metadata[0]["complete"]:
        raise ValueError("M08A metadata must contain one complete record")
    replications_R = int(metadata[0]["replications_R"])
    sampling_rounds_J = int(metadata[0]["sampling_rounds_J"])
    if replications_R != len(axes["replication_ids"]):
        raise ValueError("recorded R differs from the complete exposure replication axis")
    if sampling_rounds_J != config.sampling_rounds:
        raise ValueError("recorded J differs from PortfolioConfig sampling_rounds")
    if len(count_rows) != int(metadata[0]["count_portfolios_P"]):
        raise ValueError("portfolio count table is incomplete")
    if len(sample_rows) != len(count_rows) * sampling_rounds_J:
        raise ValueError("portfolio sample table does not contain exactly P multiplied by J rows")
    if len(sample_rows) != int(metadata[0]["portfolio_sample_count"]):
        raise ValueError("portfolio metadata sample count disagrees with retained samples")
    if len(matrix_metadata) != int(metadata[0]["unique_matrix_count"]):
        raise ValueError("portfolio metadata matrix count disagrees with the registry")
    round_replications = {
        int(row["round_id"]): str(row["selected_joint_replication_id"]) for row in round_rows
    }
    if (
        len(round_rows) != sampling_rounds_J
        or len(round_replications) != sampling_rounds_J
        or set(round_replications) != set(range(sampling_rounds_J))
        or not set(round_replications.values()) <= set(axes["replication_ids"])
    ):
        raise ValueError("sampling-round table does not contain the exact J round axis")
    if any(not row["complete"] for row in matrix_metadata):
        raise ValueError("sample matrix registry contains an incomplete matrix")

    budgets = _resolved_budgets(config)
    _validate_reusable_count_coverage(reader, config, count_rows, budgets)
    parsed_counts: dict[str, dict[str, int]] = {}
    for row in count_rows:
        portfolio_id = str(row["portfolio_id"])
        if portfolio_id in parsed_counts:
            raise ValueError("duplicate portfolio ID in M08A count table")
        value = json.loads(str(row["count_by_fleet_json"]))
        if not isinstance(value, dict) or any(
            isinstance(item, bool) or not isinstance(item, int) or item < 0
            for item in value.values()
        ):
            raise ValueError("invalid count vector in M08A artifact")
        parsed_counts[portfolio_id] = value
        if str(row["catalog_hash"]) != identity.catalog_hash or int(
            row["total_cost_minor"]
        ) != _portfolio_cost(value, source_config):
            raise ValueError("M08A portfolio count lineage or source cost is inconsistent")
        if portfolio_id != stable_id(
            "portfolio", {"catalog_hash": identity.catalog_hash, "count_by_fleet": value}
        ):
            raise ValueError("portfolio ID does not match its catalog and count vector")

    registry = {str(row["matrix_id"]): row for row in matrix_metadata}
    if len(registry) != len(matrix_metadata):
        raise ValueError("duplicate matrix ID in M08A matrix registry")
    if config.sensing_statistics_mode == "on_demand":
        matrix_values = None
    elif versions.get("storage") == "portfolio-samples-reconstruct@2":
        from mobile_sensing.portfolio.reconstruction import ReconstructedMatrices

        if matrix_exposure:
            raise ValueError("Reconstruction storage must not contain materialized sample matrices")
        matrix_values = ReconstructedMatrices(artifact_root, reader, cancellation=cancellation)
    else:
        matrix_values: dict[str, dict[tuple[str, str], float]] = {
            matrix_id: {} for matrix_id in registry
        }
        valid_cells = set(axes["cell_ids"])
        valid_bins = set(axes["time_bin_ids"])
        for row in matrix_exposure:
            matrix_id = str(row["matrix_id"])
            axis = (str(row["cell_id"]), str(row["time_bin_id"]))
            duration = float(row["duration_s"])
            if matrix_id not in registry or axis in matrix_values.get(matrix_id, {}):
                raise ValueError("sample exposure has an unknown matrix or duplicate sparse axis")
            if axis[0] not in valid_cells or axis[1] not in valid_bins:
                raise ValueError("sample exposure axis is outside the complete exposure domain")
            if not math.isfinite(duration) or duration <= 0.0:
                raise ValueError("sample exposure must store only finite positive sparse durations")
            matrix_values[matrix_id][axis] = duration
        for matrix_id, matrix in matrix_values.items():
            record = registry[matrix_id]
            if len(matrix) != int(record["nonzero_rows"]) or not math.isclose(
                math.fsum(matrix.values()),
                float(record["total_exposure_s"]),
                rel_tol=1e-12,
                abs_tol=1e-12,
            ):
                raise ValueError("sample matrix registry disagrees with sparse matrix contents")

    grouped_samples: dict[str, list[dict[str, object]]] = defaultdict(list)
    seen_rounds: dict[str, set[int]] = defaultdict(set)
    seen_sample_ids: set[str] = set()
    for row in sample_rows:
        portfolio_id = str(row["portfolio_id"])
        round_id = int(row["round_id"])
        if portfolio_id not in parsed_counts or str(row["matrix_id"]) not in registry:
            raise ValueError("sample row references an unknown portfolio or matrix")
        if round_id in seen_rounds[portfolio_id]:
            raise ValueError("duplicate portfolio sampling round")
        sample_id = str(row["sample_id"])
        if sample_id in seen_sample_ids:
            raise ValueError("duplicate sample ID")
        seen_sample_ids.add(sample_id)
        matrix_record = registry[str(row["matrix_id"])]
        selected_replication = str(row["selected_joint_replication_id"])
        if (
            round_replications.get(round_id) != selected_replication
            or str(matrix_record["selected_joint_replication_id"]) != selected_replication
            or not math.isclose(
                float(row["total_exposure_s"]),
                float(matrix_record["total_exposure_s"]),
                rel_tol=1e-12,
                abs_tol=1e-12,
            )
            or not math.isclose(
                float(row["utility"]),
                float(matrix_record["utility"]),
                rel_tol=1e-12,
                abs_tol=1e-12,
            )
        ):
            raise ValueError("sample row disagrees with its joint round or matrix registry")
        seen_rounds[portfolio_id].add(round_id)
        grouped_samples[portfolio_id].append(row)
    expected_rounds = set(range(sampling_rounds_J))
    if any(rounds != expected_rounds for rounds in seen_rounds.values()):
        raise ValueError("each portfolio must contain the exact zero-based J round axis")
    selected_by_sample: dict[str, list[tuple[str, str]]] = defaultdict(list)
    valid_vehicles = {(key.fleet_id, key.vehicle_id) for key in axes["vehicle_keys"]}
    for row in selection_rows:
        sample_id = str(row["sample_id"])
        vehicle = (str(row["fleet_id"]), str(row["vehicle_id"]))
        if sample_id not in seen_sample_ids or vehicle not in valid_vehicles:
            raise ValueError("sample selection references an unknown sample or catalog vehicle")
        selected_by_sample[sample_id].append(vehicle)
    for row in sample_rows:
        expected_counts = parsed_counts[str(row["portfolio_id"])]
        selected = selected_by_sample[str(row["sample_id"])]
        if len(selected) != len(set(selected)) or any(
            sum(fleet_id == fleet for fleet_id, _ in selected) != expected_count
            for fleet, expected_count in expected_counts.items()
        ):
            raise ValueError("sample selection does not match its count portfolio")

    statistic_rows = []
    sensing_rows = []
    for index, portfolio_id in enumerate(sorted(parsed_counts)):
        if cancellation:
            cancellation.raise_if_cancelled()
        if progress:
            progress.update(phase="portfolio.statistics", completed=index, total=len(parsed_counts))
        samples_for_portfolio = sorted(
            grouped_samples[portfolio_id], key=lambda row: int(row["round_id"])
        )
        summary = summarize_scalar_samples(float(row["utility"]) for row in samples_for_portfolio)
        cost = _portfolio_cost(parsed_counts[portfolio_id], config)
        mean_key = quantize_objective(summary.mean, config.comparison_resolution.mean_utility)
        std_key = (
            quantize_objective(summary.sample_std, config.comparison_resolution.std_utility)
            if summary.sample_std is not None
            else None
        )
        statistic_rows.append(
            {
                "portfolio_id": portfolio_id,
                "count_by_fleet_json": json.dumps(
                    parsed_counts[portfolio_id], sort_keys=True, separators=(",", ":")
                ),
                "total_cost_minor": cost,
                "replications_R": replications_R,
                "sampling_rounds_J": sampling_rounds_J,
                "sample_count": summary.count,
                "utility_mean": summary.mean,
                "utility_sample_variance": summary.sample_variance,
                "utility_sample_std": summary.sample_std,
                "conditional_mean_se": summary.conditional_mean_se,
                "utility_min": summary.minimum,
                "utility_max": summary.maximum,
                "utility_p05": summary.p05,
                "utility_p50": summary.p50,
                "utility_p95": summary.p95,
                "mean_comparison_key": str(mean_key),
                "std_comparison_key": str(std_key) if std_key is not None else None,
            }
        )
        if matrix_values is not None:
            rows = _sparse_axis_statistics(
                portfolio_id=portfolio_id,
                samples=samples_for_portfolio,
                matrix_values=matrix_values,
                replications_R=replications_R,
                sampling_rounds_J=sampling_rounds_J,
            )
            sensing_rows.extend(rows)

    frontier_enabled = sampling_rounds_J >= 2
    budget_rows, frontier_rows = build_budget_frontiers(
        statistic_rows,
        budgets=budgets,
        unit=config.costs.unit,
        minor_unit_scale=config.costs.minor_unit_scale,
        mean_resolution=config.comparison_resolution.mean_utility,
        std_resolution=config.comparison_resolution.std_utility,
        replications_R=replications_R,
        sampling_rounds_J=sampling_rounds_J,
        frontier_enabled=frontier_enabled,
        risk_metric=config.risk_metric,
        p05_resolution=config.comparison_resolution.p05_utility,
    )
    interpretation = (
        "allocation_variability_conditional_on_one_operational_realization"
        if replications_R == 1
        else "combined_empirical_operational_and_allocation_variability"
    )
    metadata_rows = (
        {
            "sample_artifact_id": samples.artifact_id,
            "exposure_id": exposure_reference.artifact_id,
            "replications_R": replications_R,
            "sampling_rounds_J": sampling_rounds_J,
            "count_portfolios_P": len(count_rows),
            "grid_cell_count": len(axes["cell_ids"]),
            "reporting_bin_count": len(axes["time_bin_ids"]),
            "quantile_method": PORTFOLIO_QUANTILE_METHOD,
            "risk_metric": config.risk_metric,
            "variance_ddof": VARIANCE_DDOF,
            "absent_sparse_rows_are_zero": config.sensing_statistics_mode == "eager",
            "sensing_statistics_mode": config.sensing_statistics_mode,
            "variability_interpretation": interpretation,
            "frontier_enabled": frontier_enabled,
            "inference_scope": "conditional_empirical_analysis_no_confidence_or_global_optimum_claim",
            "complete": True,
        },
    )
    return PortfolioAnalysisTables(
        sample_reference=samples,
        exposure_reference=exposure_reference,
        replications_R=replications_R,
        sampling_rounds_J=sampling_rounds_J,
        variability_interpretation=interpretation,
        grid_cell_count=len(axes["cell_ids"]),
        reporting_bin_count=len(axes["time_bin_ids"]),
        resolved_budgets=budgets,
        metadata_rows=metadata_rows,
        statistic_rows=tuple(statistic_rows),
        sensing_rows=tuple(sensing_rows),
        budget_rows=budget_rows,
        frontier_rows=frontier_rows,
    )
