"""Atomic M08A portfolio sample artifact publication and verified reads."""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa

from mobile_sensing.artifacts import (
    PartitionedTableData,
    publish_partitioned_artifact,
    read_partitioned_table,
    verify_partitioned_artifact,
)
from mobile_sensing.contracts import (
    ArtifactDependency,
    ArtifactRef,
    PortfolioConfig,
    ScientificIdentity,
    canonical_json_text,
    scientific_hash,
    scientific_projection,
)
from mobile_sensing.portfolio.evaluation import (
    PORTFOLIO_ENUMERATION_VERSION,
    PORTFOLIO_SAMPLING_VERSION,
    PORTFOLIO_UTILITY_VERSION,
    evaluate_samples,
)
from mobile_sensing.portfolio.models import (
    PortfolioEvaluationResult,
    PortfolioResourceLimits,
    UtilityWeightResource,
)
from mobile_sensing.simulation.rng import RNG_DERIVATION_VERSION


PORTFOLIO_STORAGE_VERSION = "portfolio-samples-parquet@1"

PORTFOLIO_COUNT_SCHEMA = pa.schema(
    [
        ("portfolio_id", pa.string(), False),
        ("catalog_hash", pa.string(), False),
        ("count_by_fleet_json", pa.string(), False),
        ("total_cost_minor", pa.int64(), False),
    ]
)
SAMPLING_ROUND_SCHEMA = pa.schema(
    [
        ("round_id", pa.int64(), False),
        ("selected_joint_replication_id", pa.string(), False),
        ("sampling_design", pa.string(), False),
        ("sampling_seed", pa.uint64(), False),
        ("seed_manifest_hash", pa.string(), False),
        ("replication_set_hash", pa.string(), False),
        ("sampling_design_hash", pa.string(), False),
    ]
)
SAMPLING_ORDERING_SCHEMA = pa.schema(
    [
        ("round_id", pa.int64(), False),
        ("fleet_id", pa.string(), False),
        ("rank", pa.int64(), False),
        ("vehicle_id", pa.string(), False),
    ]
)
PORTFOLIO_SAMPLE_SCHEMA = pa.schema(
    [
        ("portfolio_id", pa.string(), False),
        ("round_id", pa.int64(), False),
        ("sample_id", pa.string(), False),
        ("selected_joint_replication_id", pa.string(), False),
        ("matrix_id", pa.string(), False),
        ("utility", pa.float64(), False),
        ("total_exposure_s", pa.float64(), False),
    ]
)
SAMPLE_SELECTION_SCHEMA = pa.schema(
    [
        ("sample_id", pa.string(), False),
        ("fleet_id", pa.string(), False),
        ("vehicle_id", pa.string(), False),
    ]
)
SAMPLE_MATRIX_SCHEMA = pa.schema(
    [
        ("matrix_id", pa.string(), False),
        ("selected_joint_replication_id", pa.string(), False),
        ("selected_vehicle_set_hash", pa.string(), False),
        ("nonzero_rows", pa.int64(), False),
        ("total_exposure_s", pa.float64(), False),
        ("utility", pa.float64(), False),
        ("complete", pa.bool_(), False),
    ]
)
SAMPLE_EXPOSURE_SCHEMA = pa.schema(
    [
        ("matrix_id", pa.string(), False),
        ("cell_id", pa.string(), False),
        ("time_bin_id", pa.string(), False),
        ("duration_s", pa.float64(), False),
    ]
)
PORTFOLIO_METADATA_SCHEMA = pa.schema(
    [
        ("exposure_id", pa.string(), False),
        ("replications_R", pa.int64(), False),
        ("sampling_rounds_J", pa.int64(), False),
        ("count_portfolios_P", pa.int64(), False),
        ("portfolio_sample_count", pa.int64(), False),
        ("unique_matrix_count", pa.int64(), False),
        ("variability_interpretation", pa.string(), False),
        ("complete", pa.bool_(), False),
    ]
)


def publish_portfolio_samples(
    *,
    artifact_root: str | Path,
    exposure: ArtifactRef,
    config: PortfolioConfig,
    weights: UtilityWeightResource,
    limits: PortfolioResourceLimits | None = None,
    cancellation=None,
    progress=None,
) -> PortfolioEvaluationResult:
    """Evaluate and atomically publish the complete P by J sample design."""

    evaluated = evaluate_samples(
        artifact_root=artifact_root,
        exposure=exposure,
        config=config,
        weights=weights,
        limits=limits,
        cancellation=cancellation,
        progress=progress,
    )
    plan = evaluated.plan
    axes = plan.axes
    identity_source = axes["artifact"].manifest.scientific_identity
    assert identity_source.catalog_hash is not None
    assert identity_source.replication_set_hash is not None
    assert identity_source.grid_axis_hash is not None
    assert identity_source.time_axis_hash is not None
    count_rows = [
        {
            "portfolio_id": item.portfolio_id,
            "catalog_hash": item.catalog_hash,
            "count_by_fleet_json": canonical_json_text(item.count_by_fleet),
            "total_cost_minor": item.total_cost_minor,
        }
        for item in plan.counts
    ]
    round_rows = [item.model_dump(mode="json") for item in evaluated.design.rounds]
    ordering_rows = [item.model_dump(mode="json") for item in evaluated.design.orderings]
    matrix_rows = [
        row
        for matrix_id in sorted(evaluated.matrix_rows)
        for row in evaluated.matrix_rows[matrix_id]
    ]
    metadata_rows = [
        {
            "exposure_id": exposure.artifact_id,
            "replications_R": plan.preview.replications_R,
            "sampling_rounds_J": plan.preview.sampling_rounds_J,
            "count_portfolios_P": len(plan.counts),
            "portfolio_sample_count": len(evaluated.sample_rows),
            "unique_matrix_count": len(evaluated.matrix_metadata),
            "variability_interpretation": (
                "allocation_variability_conditional_on_one_operational_realization"
                if plan.preview.replications_R == 1
                else "combined_empirical_operational_and_allocation_variability"
            ),
            "complete": True,
        }
    ]
    rows_by_name = {
        "portfolio_counts": count_rows,
        "portfolio_metadata": metadata_rows,
        "portfolio_samples": list(evaluated.sample_rows),
        "sample_exposure": matrix_rows,
        "sample_matrices": list(evaluated.matrix_metadata),
        "sample_selection": list(evaluated.selection_rows),
        "sampling_orderings": ordering_rows,
        "sampling_rounds": round_rows,
    }
    schemas = {
        "portfolio_counts": (PORTFOLIO_COUNT_SCHEMA, ("portfolio_id",)),
        "portfolio_metadata": (PORTFOLIO_METADATA_SCHEMA, ("exposure_id",)),
        "portfolio_samples": (PORTFOLIO_SAMPLE_SCHEMA, ("portfolio_id", "round_id")),
        "sample_exposure": (
            SAMPLE_EXPOSURE_SCHEMA,
            ("matrix_id", "cell_id", "time_bin_id"),
        ),
        "sample_matrices": (SAMPLE_MATRIX_SCHEMA, ("matrix_id",)),
        "sample_selection": (
            SAMPLE_SELECTION_SCHEMA,
            ("sample_id", "fleet_id", "vehicle_id"),
        ),
        "sampling_orderings": (
            SAMPLING_ORDERING_SCHEMA,
            ("round_id", "fleet_id", "rank"),
        ),
        "sampling_rounds": (SAMPLING_ROUND_SCHEMA, ("round_id",)),
    }
    tables = tuple(
        PartitionedTableData(
            name=name,
            schema=schemas[name][0],
            partition_axes=(),
            rows_by_partition={(): rows_by_name[name]},
            key_columns=schemas[name][1],
        )
        for name in sorted(rows_by_name)
    )
    dependency = ArtifactDependency(
        role="exposure", artifact_id=exposure.artifact_id, content_hash=exposure.content_hash
    )
    portfolio_projection = scientific_projection(config)
    if config.utility.temporal_interval_s is None:
        portfolio_projection["utility"].pop("temporal_interval_s", None)
    resolved_config = {
        "portfolio": portfolio_projection,
        "weights": scientific_projection(weights),
        "resolved_count_levels": plan.preview.model_dump(mode="json")["resolved_count_levels"],
        "resolved_budget_levels_minor": plan.preview.model_dump(mode="json")[
            "resolved_budget_levels_minor"
        ],
        "replications_R": plan.preview.replications_R,
        "sampling_rounds_J": plan.preview.sampling_rounds_J,
        "output_hashes": {
            name: scientific_hash(rows_by_name[name]) for name in sorted(rows_by_name)
        },
    }
    identity = ScientificIdentity(
        schema_version="2.0",
        artifact_kind="portfolio",
        resolved_config=resolved_config,
        resolved_config_hash=scientific_hash(resolved_config),
        dependency_hashes={dependency.role: dependency.content_hash},
        algorithm_versions={
            "enumeration": PORTFOLIO_ENUMERATION_VERSION,
            "sampling": PORTFOLIO_SAMPLING_VERSION,
            "sample_utility": (
                PORTFOLIO_UTILITY_VERSION
                if config.utility.temporal_interval_s is not None
                else "sample-utility@2"
            ),
            "storage": (
                "portfolio-samples-reconstruct@2"
                if config.sample_matrix_storage == "reconstruct"
                else PORTFOLIO_STORAGE_VERSION
            ),
        },
        random_stream_version=RNG_DERIVATION_VERSION,
        catalog_hash=identity_source.catalog_hash,
        replication_set_hash=identity_source.replication_set_hash,
        grid_axis_hash=identity_source.grid_axis_hash,
        time_axis_hash=identity_source.time_axis_hash,
    )
    reference = publish_partitioned_artifact(
        artifact_root=Path(artifact_root),
        collection="portfolios",
        identity=identity,
        dependencies=(dependency,),
        tables=tables,
    ).reference
    return PortfolioEvaluationResult(
        reference=reference,
        preview=plan.preview,
        count_portfolios=len(plan.counts),
        sampling_rounds_J=plan.preview.sampling_rounds_J,
        unique_sample_matrices=len(evaluated.matrix_metadata),
        portfolio_samples=len(evaluated.sample_rows),
    )


class PortfolioArtifactReader:
    """Verify and read the complete M08A sample artifact without sparse ambiguity."""

    def __init__(self, artifact_root: str | Path, reference: ArtifactRef) -> None:
        self.artifact = verify_partitioned_artifact(
            artifact_root=Path(artifact_root),
            collection="portfolios",
            reference=reference,
        )
        if reference.artifact_kind != "portfolio":
            raise ValueError("portfolio reader requires a portfolio artifact")

    def read(self, table_name: str, *, filters=None) -> pa.Table:
        schemas = {
            "portfolio_counts": PORTFOLIO_COUNT_SCHEMA,
            "portfolio_metadata": PORTFOLIO_METADATA_SCHEMA,
            "portfolio_samples": PORTFOLIO_SAMPLE_SCHEMA,
            "sample_exposure": SAMPLE_EXPOSURE_SCHEMA,
            "sample_matrices": SAMPLE_MATRIX_SCHEMA,
            "sample_selection": SAMPLE_SELECTION_SCHEMA,
            "sampling_orderings": SAMPLING_ORDERING_SCHEMA,
            "sampling_rounds": SAMPLING_ROUND_SCHEMA,
        }
        try:
            schema = schemas[table_name]
        except KeyError as exc:
            raise KeyError(f"unknown M08A portfolio table: {table_name!r}") from exc
        return read_partitioned_table(
            self.artifact, table_name=table_name, schema=schema, filters=filters
        )
