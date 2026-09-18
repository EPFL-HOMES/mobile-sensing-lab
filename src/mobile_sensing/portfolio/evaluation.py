"""M08A count enumeration and joint-replication random allocation sampling."""

from __future__ import annotations

import itertools
import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from mobile_sensing.artifacts import read_partitioned_table
from mobile_sensing.contracts import (
    ArtifactRef,
    PortfolioConfig,
    PortfolioCountRecord,
    SamplingOrderingRecord,
    SamplingRoundRecord,
    VehicleKey,
    scientific_hash,
    stable_id,
)
from mobile_sensing.exposure import ExposureArtifactReader
from mobile_sensing.exposure.storage import (
    replication_status_schema,
    TIME_BIN_SCHEMA,
)
from mobile_sensing.portfolio.models import (
    PortfolioPreview,
    PortfolioResourceLimits,
    UtilityWeightResource,
)
from mobile_sensing.simulation import SimulationArtifactReader
from mobile_sensing.simulation.rng import SemanticRngStreams
from mobile_sensing.portfolio.utility import pointwise_utility


PORTFOLIO_ENUMERATION_VERSION = "fleet-count-enumeration@1"
PORTFOLIO_SAMPLING_VERSION = "joint-replication-uniform-vehicle@1"
PORTFOLIO_UTILITY_VERSION = "sample-utility@4"
ESTIMATED_EXPOSURE_ROW_BYTES = 128
ESTIMATED_SAMPLE_ROW_BYTES = 96
ESTIMATED_SAMPLE_METADATA_BYTES = 192
ESTIMATED_SELECTION_ROW_BYTES = 64
ESTIMATED_ORDERING_ROW_BYTES = 64


@dataclass(frozen=True, slots=True)
class PortfolioPlan:
    config_hash: str
    preview: PortfolioPreview
    counts: tuple[PortfolioCountRecord, ...]
    axes: dict[str, object]
    time_bin_durations: dict[str, float]
    utility_bin_by_time_bin: dict[str, str]


@dataclass(frozen=True, slots=True)
class SamplingDesign:
    rounds: tuple[SamplingRoundRecord, ...]
    orderings: tuple[SamplingOrderingRecord, ...]
    permutations: tuple[dict[str, tuple[VehicleKey, ...]], ...]


@dataclass(frozen=True, slots=True)
class EvaluatedSamples:
    plan: PortfolioPlan
    design: SamplingDesign
    matrix_rows: dict[str, tuple[dict[str, object], ...]]
    matrix_metadata: tuple[dict[str, object], ...]
    sample_rows: tuple[dict[str, object], ...]
    selection_rows: tuple[dict[str, object], ...]


def _range_size(minimum: int, maximum: int, step: int, include_max: bool) -> int:
    quotient, remainder = divmod(maximum - minimum, step)
    return quotient + 1 + int(include_max and remainder != 0)


def _resolved_range(
    minimum: int,
    maximum: int,
    step: int,
    include_max: bool,
    *,
    max_values: int,
    label: str,
) -> tuple[int, ...]:
    if _range_size(minimum, maximum, step, include_max) > max_values:
        raise ValueError(f"{label} range exceeds its configured resolution limit")
    values = list(range(minimum, maximum + 1, step))
    if include_max and values[-1] != maximum:
        values.append(maximum)
    return tuple(values)


def _resolved_count_levels(
    config: PortfolioConfig,
    catalog_sizes: dict[str, int],
    limits: PortfolioResourceLimits,
) -> dict[str, tuple[int, ...]]:
    result = {}
    for fleet_id in sorted(config.count_enumeration.fleets):
        value = config.count_enumeration.fleets[fleet_id]
        if hasattr(value, "count_levels"):
            levels = tuple(value.count_levels)
        else:
            if value.max_count > catalog_sizes[fleet_id]:
                raise ValueError(f"count level exceeds the physical catalog for fleet {fleet_id!r}")
            levels = _resolved_range(
                value.min_count,
                value.max_count,
                value.step,
                value.include_max,
                max_values=limits.max_count_portfolios,
                label=f"count levels for fleet {fleet_id!r}",
            )
        if len(levels) > limits.max_count_portfolios:
            raise ValueError(f"count levels for fleet {fleet_id!r} exceed the configured limit")
        result[fleet_id] = levels
    return result


def _resolved_budgets(config: PortfolioConfig, limits: PortfolioResourceLimits) -> tuple[int, ...]:
    value = config.budgets
    if hasattr(value, "levels_minor"):
        if len(value.levels_minor) > limits.max_budget_levels:
            raise ValueError("budget levels exceed max_budget_levels")
        return tuple(value.levels_minor)
    return _resolved_range(
        value.min_minor,
        value.max_minor,
        value.step_minor,
        value.include_max,
        max_values=limits.max_budget_levels,
        label="budget levels",
    )


def _verified_axes(artifact_root: Path, exposure: ArtifactRef) -> dict[str, object]:
    reader = ExposureArtifactReader(artifact_root)
    axes = reader.axes(exposure)
    artifact = axes["artifact"]
    simulation_dependency = next(
        (item for item in artifact.manifest.dependencies if item.role == "simulation"), None
    )
    if simulation_dependency is None:
        raise ValueError("exposure artifact lacks its joint simulation dependency")
    simulation = SimulationArtifactReader(
        artifact_root,
        ArtifactRef(
            artifact_id=simulation_dependency.artifact_id,
            artifact_kind="simulation",
            content_hash=simulation_dependency.content_hash,
        ),
    )
    if simulation.replication_ids != axes["replication_ids"]:
        raise ValueError("exposure and simulation replication axes differ")
    if simulation.vehicle_keys != axes["vehicle_keys"]:
        raise ValueError("exposure and simulation catalogs differ")
    identity = artifact.manifest.scientific_identity
    simulation_identity = simulation.artifact.manifest.scientific_identity
    if (
        identity.catalog_hash != simulation_identity.catalog_hash
        or identity.replication_set_hash != simulation_identity.replication_set_hash
    ):
        raise ValueError("exposure joint-replication lineage differs from its simulation")
    statuses = read_partitioned_table(
        artifact,
        table_name="replication_status",
        schema=replication_status_schema(artifact),
    ).to_pylist()
    if any(not row["complete"] for row in statuses):
        raise ValueError("portfolio input contains an incomplete replication")
    time_bins = read_partitioned_table(
        artifact, table_name="time_bins", schema=TIME_BIN_SCHEMA
    ).to_pylist()
    axes["time_bins"] = tuple(sorted(time_bins, key=lambda row: row["canonical_index"]))
    return axes


def build_portfolio_plan(
    *,
    artifact_root: str | Path,
    exposure: ArtifactRef,
    config: PortfolioConfig,
    limits: PortfolioResourceLimits | None = None,
) -> PortfolioPlan:
    """Resolve counts/budgets and block unsafe work before sampling or matrix reads."""

    artifact_root = Path(artifact_root)
    exposure = ArtifactRef.model_validate(exposure)
    config = PortfolioConfig.model_validate(config)
    limits = PortfolioResourceLimits.model_validate(limits or PortfolioResourceLimits())
    if exposure.artifact_kind != "exposure" or config.exposure_id != exposure.artifact_id:
        raise ValueError("portfolio configuration must reference the supplied exposure artifact")
    axes = _verified_axes(artifact_root, exposure)
    fleet_catalog: dict[str, list[VehicleKey]] = defaultdict(list)
    for key in axes["vehicle_keys"]:
        fleet_catalog[key.fleet_id].append(key)
    catalog_sizes = {fleet_id: len(values) for fleet_id, values in sorted(fleet_catalog.items())}
    if set(config.count_enumeration.fleets) != set(catalog_sizes):
        raise ValueError("portfolio count fleets must exactly equal the exposure catalog fleets")
    levels = _resolved_count_levels(config, catalog_sizes, limits)
    for fleet_id, values in levels.items():
        if values[-1] > catalog_sizes[fleet_id]:
            raise ValueError(f"count level exceeds the physical catalog for fleet {fleet_id!r}")
    budgets = _resolved_budgets(config, limits)
    raw_count = math.prod(len(values) for values in levels.values())
    exposure_manifest = next(
        item for item in axes["artifact"].manifest.tables if item.name == "exposure"
    )
    exposure_nnz = exposure_manifest.row_count
    max_replication_nnz = max(item.row_count for item in exposure_manifest.partitions)
    max_matrix_nnz = min(len(axes["cell_ids"]) * len(axes["time_bin_ids"]), max_replication_nnz)
    exposure_working_bytes = exposure_nnz * ESTIMATED_EXPOSURE_ROW_BYTES + max_matrix_nnz * 80
    working_bytes = exposure_working_bytes
    reasons = []
    if raw_count > limits.max_count_portfolios:
        reasons.append("count portfolio grid exceeds max_count_portfolios")
    if working_bytes > limits.max_working_bytes:
        reasons.append("sparse exposure working set exceeds max_working_bytes")
    counts: list[PortfolioCountRecord] = []
    feasible_count: int | None = None
    planned_records: int | None = None
    estimated_rows: int | None = None
    estimated_bytes: int | None = None
    if not reasons:
        fleet_ids = tuple(sorted(levels))
        maximum_budget = max(budgets)
        for values in itertools.product(*(levels[fleet_id] for fleet_id in fleet_ids)):
            count_by_fleet = dict(zip(fleet_ids, values, strict=True))
            cost = sum(
                config.costs.by_fleet_minor[fleet_id] * count_by_fleet[fleet_id]
                for fleet_id in fleet_ids
            )
            if cost <= maximum_budget:
                counts.append(
                    PortfolioCountRecord(
                        portfolio_id=stable_id(
                            "portfolio",
                            {
                                "catalog_hash": axes[
                                    "artifact"
                                ].manifest.scientific_identity.catalog_hash,
                                "count_by_fleet": count_by_fleet,
                            },
                        ),
                        catalog_hash=axes["artifact"].manifest.scientific_identity.catalog_hash,
                        count_by_fleet=count_by_fleet,
                        total_cost_minor=cost,
                    )
                )
        counts.sort(key=lambda item: tuple(item.count_by_fleet[key] for key in fleet_ids))
        feasible_count = len(counts)
        planned_records = feasible_count * config.sampling_rounds
        if planned_records > limits.max_portfolio_round_records:
            reasons.append("P multiplied by J exceeds max_portfolio_round_records")
        estimated_rows = (
            planned_records * max_matrix_nnz
            if config.sample_matrix_storage == "materialized"
            else 0
        )
        estimated_bytes = estimated_rows * ESTIMATED_SAMPLE_ROW_BYTES
        if estimated_bytes > limits.max_estimated_matrix_bytes:
            reasons.append("estimated sample matrix storage exceeds max_estimated_matrix_bytes")
        selection_rows = config.sampling_rounds * sum(
            sum(item.count_by_fleet.values()) for item in counts
        )
        ordering_rows = config.sampling_rounds * sum(catalog_sizes.values())
        working_bytes = (
            exposure_working_bytes
            + estimated_bytes
            + planned_records * ESTIMATED_SAMPLE_METADATA_BYTES
            + selection_rows * ESTIMATED_SELECTION_ROW_BYTES
            + ordering_rows * ESTIMATED_ORDERING_ROW_BYTES
        )
        if working_bytes > limits.max_working_bytes:
            reasons.append("bounded portfolio working set exceeds max_working_bytes")
    preview = PortfolioPreview(
        exposure_id=exposure.artifact_id,
        replications_R=len(axes["replication_ids"]),
        sampling_rounds_J=config.sampling_rounds,
        catalog_size_by_fleet=catalog_sizes,
        resolved_count_levels=levels,
        resolved_budget_levels_minor=budgets,
        raw_count_portfolios=raw_count,
        feasible_count_portfolios=feasible_count,
        planned_portfolio_round_records=planned_records,
        exposure_nonzero_rows=exposure_nnz,
        estimated_sample_matrix_rows=estimated_rows,
        estimated_sample_matrix_bytes=estimated_bytes,
        estimated_working_bytes=working_bytes,
        blocking_reasons=tuple(reasons),
    )
    time_bins = axes["time_bins"]
    interval = config.utility.temporal_interval_s
    if interval is None:
        utility_bin_by_time_bin = {row["time_bin_id"]: row["time_bin_id"] for row in time_bins}
        utility_durations = {row["time_bin_id"]: row["end_s"] - row["start_s"] for row in time_bins}
    else:
        origin = time_bins[0]["start_s"]
        utility_bin_by_time_bin, utility_durations = {}, {}
        for row in time_bins:
            index = math.floor((row["start_s"] - origin) / interval + 1e-12)
            boundary = origin + (index + 1) * interval
            if row["end_s"] > boundary + 1e-9:
                raise ValueError(
                    "Utility temporal interval must align with complete simulation reporting bins"
                )
            identifier = f"utility-bin-{index:05d}"
            utility_bin_by_time_bin[row["time_bin_id"]] = identifier
            utility_durations[identifier] = utility_durations.get(identifier, 0.0) + (
                row["end_s"] - row["start_s"]
            )
    return PortfolioPlan(
        config_hash=scientific_hash(config),
        preview=preview,
        counts=tuple(counts),
        axes=axes,
        time_bin_durations=utility_durations,
        utility_bin_by_time_bin=utility_bin_by_time_bin,
    )


def draw_sampling_design(config: PortfolioConfig, plan: PortfolioPlan) -> SamplingDesign:
    """Draw one joint replication and one full uniform fleet permutation per round."""

    config = PortfolioConfig.model_validate(config)
    if plan.preview.blocked:
        raise ValueError("cannot draw a blocked portfolio plan")
    if scientific_hash(config) != plan.config_hash:
        raise ValueError("portfolio sampling config differs from its enumeration plan")
    axes = plan.axes
    identity = axes["artifact"].manifest.scientific_identity
    assert identity.catalog_hash is not None and identity.replication_set_hash is not None
    by_fleet: dict[str, tuple[VehicleKey, ...]] = {}
    for key in axes["vehicle_keys"]:
        by_fleet.setdefault(key.fleet_id, ())
        by_fleet[key.fleet_id] += (key,)
    rounds = []
    ordering_rows = []
    permutations = []
    for round_id in range(config.sampling_rounds):
        rng = SemanticRngStreams(config.sampling_seed, namespace="portfolio")
        replication_rng = rng.stream(
            "sampling.replication",
            str(round_id),
            identity.catalog_hash,
            identity.replication_set_hash,
        )
        replication_id = axes["replication_ids"][
            int(replication_rng.integers(0, len(axes["replication_ids"])))
        ]
        round_permutations = {}
        for fleet_id in sorted(by_fleet):
            generator = rng.stream(
                "sampling.vehicle_permutation",
                str(round_id),
                fleet_id,
                identity.catalog_hash,
            )
            indices = generator.permutation(len(by_fleet[fleet_id]))
            permutation = tuple(by_fleet[fleet_id][int(index)] for index in indices)
            round_permutations[fleet_id] = permutation
            ordering_rows.extend(
                SamplingOrderingRecord(
                    round_id=round_id,
                    fleet_id=fleet_id,
                    rank=rank,
                    vehicle_id=key.vehicle_id,
                )
                for rank, key in enumerate(permutation)
            )
        manifest_hash = scientific_hash(rng.manifest)
        design_hash = scientific_hash(
            {
                "sampling_design": config.sampling_design,
                "sampling_seed": config.sampling_seed,
                "seed_manifest_hash": manifest_hash,
                "replication_set_hash": identity.replication_set_hash,
            }
        )
        rounds.append(
            SamplingRoundRecord(
                round_id=round_id,
                selected_joint_replication_id=replication_id,
                sampling_design=config.sampling_design,
                sampling_seed=config.sampling_seed,
                seed_manifest_hash=manifest_hash,
                replication_set_hash=identity.replication_set_hash,
                sampling_design_hash=design_hash,
            )
        )
        permutations.append(round_permutations)
    return SamplingDesign(
        rounds=tuple(rounds),
        orderings=tuple(ordering_rows),
        permutations=tuple(permutations),
    )


def _normalized_weights(
    weights: UtilityWeightResource, plan: PortfolioPlan
) -> tuple[dict[tuple[str, str], float] | None, float]:
    axes = plan.axes
    cell_ids = set(axes["cell_ids"])
    if weights.kind == "uniform_spatial_duration_temporal":
        total = len(cell_ids) * math.fsum(plan.time_bin_durations.values())
        return None, total
    if weights.kind == "spatial_duration_temporal":
        raw = {item.cell_id: item.raw_weight for item in weights.spatial_values}
        if set(raw) != cell_ids:
            raise ValueError("Factored spatial weights must cover the exact grid, including zeros")
        total = math.fsum(raw.values()) * math.fsum(plan.time_bin_durations.values())
        if total <= 0:
            raise ValueError("Spatial utility weights have no positive mass")
        return raw, total
    original_bin_ids = set(axes["time_bin_ids"])
    original = {(item.cell_id, item.time_bin_id): item.raw_weight for item in weights.values}
    if any(cell not in cell_ids or time_bin not in original_bin_ids for cell, time_bin in original):
        raise ValueError("utility weight axes are outside the exposure artifact")
    if weights.missing_policy == "error" and len(original) != len(cell_ids) * len(original_bin_ids):
        raise ValueError("utility weights do not cover the complete cell/time domain")
    raw = defaultdict(float)
    for (cell, time_bin), value in original.items():
        raw[cell, plan.utility_bin_by_time_bin[time_bin]] += value
    total = math.fsum(raw.values())
    if total <= 0.0:
        raise ValueError("utility raw weights must have positive total mass")
    return {key: value / total for key, value in raw.items() if value > 0.0}, 1.0


def _utility_matrix(matrix, plan):
    result = defaultdict(float)
    for (cell, time_bin), duration in matrix.items():
        result[cell, plan.utility_bin_by_time_bin[time_bin]] += duration
    return result


def _reference_matrix(selected, replication_id, source, weight, config, plan, matrix_id, stored):
    grouped = defaultdict(list)
    for key in selected:
        for axis, duration in source.get(
            (replication_id, key.fleet_id, key.vehicle_id), {}
        ).items():
            grouped[axis].append(duration)
    matrix = {axis: math.fsum(values) for axis, values in grouped.items()}
    if config.sample_matrix_storage == "materialized":
        stored[matrix_id] = tuple(
            {
                "matrix_id": matrix_id,
                "cell_id": cell,
                "time_bin_id": time_bin,
                "duration_s": duration,
            }
            for (cell, time_bin), duration in sorted(matrix.items())
        )
    total = math.fsum(matrix.values())
    utility = math.fsum(
        weight(axis) * pointwise_utility(duration, config.utility.kind, config.utility.saturation_s)
        for axis, duration in _utility_matrix(matrix, plan).items()
    )
    return total, utility, len(matrix)


def evaluate_samples(
    *,
    artifact_root: str | Path,
    exposure: ArtifactRef,
    config: PortfolioConfig,
    weights: UtilityWeightResource,
    limits: PortfolioResourceLimits | None = None,
    cancellation=None,
    progress=None,
) -> EvaluatedSamples:
    """Evaluate every feasible count portfolio for exactly J retained random draws."""

    config = PortfolioConfig.model_validate(config)
    weights = UtilityWeightResource.model_validate(weights)
    if weights.weights_id != config.utility.weights_ref:
        raise ValueError("utility weight resource does not match PortfolioConfig.weights_ref")
    plan = build_portfolio_plan(
        artifact_root=artifact_root, exposure=exposure, config=config, limits=limits
    )
    if plan.preview.blocked:
        raise ValueError(
            "portfolio evaluation blocked: " + "; ".join(plan.preview.blocking_reasons)
        )
    normalized_weights, uniform_denominator = _normalized_weights(weights, plan)

    def weight(axis_key: tuple[str, str]) -> float:
        if weights.kind == "spatial_duration_temporal":
            return (
                normalized_weights[axis_key[0]]
                * plan.time_bin_durations[axis_key[1]]
                / uniform_denominator
            )
        if normalized_weights is not None:
            return normalized_weights.get(axis_key, 0.0)
        return plan.time_bin_durations[axis_key[1]] / uniform_denominator

    design = draw_sampling_design(config, plan)
    reader = ExposureArtifactReader(Path(artifact_root))
    _, chunks = reader.read_sparse_batches(
        exposure,
        replication_ids=plan.axes["replication_ids"],
        vehicle_keys=plan.axes["vehicle_keys"],
    )
    vehicle_exposure: dict[tuple[str, str, str], dict[tuple[str, str], float]] = defaultdict(dict)
    for chunk in chunks:
        if cancellation:
            cancellation.raise_if_cancelled()
        for row in chunk.to_pylist():
            values = vehicle_exposure[(row["replication_id"], row["fleet_id"], row["vehicle_id"])]
            axis = (row["cell_id"], row["time_bin_id"])
            if axis in values:
                raise ValueError("Duplicate sparse physical-vehicle exposure axis")
            values[axis] = row["duration_s"]
    from mobile_sensing.portfolio.indexed import IndexedFleetPrefixes

    numerical_budget = max(
        0,
        (limits or PortfolioResourceLimits()).max_working_bytes
        - plan.preview.estimated_working_bytes,
    )
    indexed = (
        IndexedFleetPrefixes(
            vehicle_exposure,
            plan.counts,
            weight,
            max_bytes=numerical_budget,
            utility_axis=lambda axis: (
                axis[0],
                plan.utility_bin_by_time_bin[axis[1]],
            ),
        )
        if plan.counts
        else None
    )
    matrix_rows: dict[str, tuple[dict[str, object], ...]] = {}
    matrix_metadata: dict[str, dict[str, object]] = {}
    sample_rows = []
    selection_rows = []
    for round_record, permutations in zip(design.rounds, design.permutations, strict=True):
        if cancellation:
            cancellation.raise_if_cancelled()
        if progress:
            progress.update(
                phase="portfolio.sampling",
                completed=round_record.round_id,
                total=config.sampling_rounds,
            )
        replication_id = round_record.selected_joint_replication_id
        indexed_ready = (
            indexed is not None
            and config.sample_matrix_storage == "reconstruct"
            and indexed.prepare(replication_id, permutations)
        )
        for portfolio in plan.counts:
            selected = tuple(
                sorted(
                    (
                        key
                        for fleet_id in sorted(permutations)
                        for key in permutations[fleet_id][: portfolio.count_by_fleet[fleet_id]]
                    ),
                    key=lambda key: (key.fleet_id, key.vehicle_id),
                )
            )
            matrix_id = stable_id(
                "matrix",
                {
                    "exposure_hash": exposure.content_hash,
                    "replication_id": replication_id,
                    "selected_vehicles": selected,
                },
            )
            if matrix_id not in matrix_metadata:
                if indexed_ready:
                    total_exposure, utility, nonzero_rows = indexed.evaluate(
                        portfolio.count_by_fleet, config.utility.kind, config.utility.saturation_s
                    )
                else:
                    total_exposure, utility, nonzero_rows = _reference_matrix(
                        selected,
                        replication_id,
                        vehicle_exposure,
                        weight,
                        config,
                        plan,
                        matrix_id,
                        matrix_rows,
                    )
                matrix_metadata[matrix_id] = {
                    "matrix_id": matrix_id,
                    "selected_joint_replication_id": replication_id,
                    "selected_vehicle_set_hash": scientific_hash(selected),
                    "nonzero_rows": nonzero_rows,
                    "total_exposure_s": total_exposure,
                    "utility": utility,
                    "complete": True,
                }
            metadata = matrix_metadata[matrix_id]
            sample_id = stable_id(
                "sample",
                {
                    "portfolio_id": portfolio.portfolio_id,
                    "round_id": round_record.round_id,
                    "sampling_design_hash": round_record.sampling_design_hash,
                },
            )
            sample_rows.append(
                {
                    "portfolio_id": portfolio.portfolio_id,
                    "round_id": round_record.round_id,
                    "sample_id": sample_id,
                    "selected_joint_replication_id": replication_id,
                    "matrix_id": matrix_id,
                    "utility": metadata["utility"],
                    "total_exposure_s": metadata["total_exposure_s"],
                }
            )
            selection_rows.extend(
                {
                    "sample_id": sample_id,
                    "fleet_id": key.fleet_id,
                    "vehicle_id": key.vehicle_id,
                }
                for key in selected
            )
    return EvaluatedSamples(
        plan=plan,
        design=design,
        matrix_rows=matrix_rows,
        matrix_metadata=tuple(matrix_metadata[key] for key in sorted(matrix_metadata)),
        sample_rows=tuple(sample_rows),
        selection_rows=tuple(selection_rows),
    )
