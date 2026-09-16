"""Immutable M08B summary/frontier artifacts with lossless M08A lineage."""

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
    scientific_hash,
    scientific_projection,
)
from mobile_sensing.portfolio.analysis import (
    PORTFOLIO_FRONTIER_VERSION,
    PORTFOLIO_SENSING_STATISTICS_VERSION,
    PORTFOLIO_STATISTICS_VERSION,
    analyze_portfolio_samples,
)
from mobile_sensing.portfolio.models import PortfolioAnalysisResult
from mobile_sensing.portfolio.storage import PortfolioArtifactReader


PORTFOLIO_ANALYSIS_STORAGE_VERSION = "portfolio-analysis-parquet@3"
PORTFOLIO_ANALYSIS_STORAGE_VERSIONS = {
    "portfolio-analysis-parquet@1",
    "portfolio-analysis-parquet@2",
    PORTFOLIO_ANALYSIS_STORAGE_VERSION,
}

PORTFOLIO_ANALYSIS_METADATA_SCHEMA = pa.schema(
    [
        ("sample_artifact_id", pa.string(), False),
        ("exposure_id", pa.string(), False),
        ("replications_R", pa.int64(), False),
        ("sampling_rounds_J", pa.int64(), False),
        ("count_portfolios_P", pa.int64(), False),
        ("grid_cell_count", pa.int64(), False),
        ("reporting_bin_count", pa.int64(), False),
        ("quantile_method", pa.string(), False),
        ("risk_metric", pa.string(), False),
        ("variance_ddof", pa.int64(), False),
        ("absent_sparse_rows_are_zero", pa.bool_(), False),
        ("sensing_statistics_mode", pa.string(), False),
        ("variability_interpretation", pa.string(), False),
        ("frontier_enabled", pa.bool_(), False),
        ("inference_scope", pa.string(), False),
        ("complete", pa.bool_(), False),
    ]
)
PORTFOLIO_STATISTICS_SCHEMA = pa.schema(
    [
        ("portfolio_id", pa.string(), False),
        ("count_by_fleet_json", pa.string(), False),
        ("total_cost_minor", pa.int64(), False),
        ("replications_R", pa.int64(), False),
        ("sampling_rounds_J", pa.int64(), False),
        ("sample_count", pa.int64(), False),
        ("utility_mean", pa.float64(), False),
        ("utility_sample_variance", pa.float64()),
        ("utility_sample_std", pa.float64()),
        ("conditional_mean_se", pa.float64()),
        ("utility_min", pa.float64(), False),
        ("utility_max", pa.float64(), False),
        ("utility_p05", pa.float64(), False),
        ("utility_p50", pa.float64(), False),
        ("utility_p95", pa.float64(), False),
        ("mean_comparison_key", pa.string(), False),
        ("std_comparison_key", pa.string()),
    ]
)
PORTFOLIO_SENSING_STATISTICS_SCHEMA = pa.schema(
    [
        ("portfolio_id", pa.string(), False),
        ("cell_id", pa.string(), False),
        ("time_bin_id", pa.string(), False),
        ("replications_R", pa.int64(), False),
        ("sampling_rounds_J", pa.int64(), False),
        ("nonzero_sample_count", pa.int64(), False),
        ("mean_duration_s", pa.float64(), False),
        ("sample_variance_s2", pa.float64()),
        ("sample_std_s", pa.float64()),
    ]
)
BUDGET_LEVEL_SCHEMA = pa.schema(
    [
        ("budget_id", pa.string(), False),
        ("budget_minor", pa.int64(), False),
        ("cost_unit", pa.string(), False),
        ("minor_unit_scale", pa.int64(), False),
        ("replications_R", pa.int64(), False),
        ("sampling_rounds_J", pa.int64(), False),
        ("feasible_portfolio_count", pa.int64(), False),
        ("frontier_portfolio_count", pa.int64()),
        ("frontier_enabled", pa.bool_(), False),
        ("disabled_reason", pa.string()),
    ]
)
BUDGET_FRONTIER_SCHEMA = pa.schema(
    [
        ("budget_id", pa.string(), False),
        ("budget_minor", pa.int64(), False),
        ("portfolio_id", pa.string(), False),
        ("replications_R", pa.int64(), False),
        ("sampling_rounds_J", pa.int64(), False),
        ("total_cost_minor", pa.int64(), False),
        ("unspent_minor", pa.int64(), False),
        ("feasible", pa.bool_(), False),
        ("nondominated", pa.bool_()),
        ("tie_group_id", pa.string()),
        ("mean_comparison_key", pa.string(), False),
        ("std_comparison_key", pa.string()),
        ("risk_comparison_key", pa.string()),
    ]
)

_ANALYSIS_SCHEMAS = {
    "budget_frontiers": BUDGET_FRONTIER_SCHEMA,
    "budget_levels": BUDGET_LEVEL_SCHEMA,
    "portfolio_analysis_metadata": PORTFOLIO_ANALYSIS_METADATA_SCHEMA,
    "portfolio_sensing_statistics": PORTFOLIO_SENSING_STATISTICS_SCHEMA,
    "portfolio_statistics": PORTFOLIO_STATISTICS_SCHEMA,
}


def publish_portfolio_analysis(
    *,
    artifact_root: str | Path,
    samples: ArtifactRef,
    config: PortfolioConfig,
    cancellation=None,
    progress=None,
) -> PortfolioAnalysisResult:
    """Publish M08B summaries while retaining immutable M08A sample/matrix references."""

    samples = ArtifactRef.model_validate(samples)
    config = PortfolioConfig.model_validate(config)
    tables = analyze_portfolio_samples(
        artifact_root=artifact_root,
        samples=samples,
        config=config,
        cancellation=cancellation,
        progress=progress,
    )
    sample_reader = PortfolioArtifactReader(artifact_root, samples)
    source_identity = sample_reader.artifact.manifest.scientific_identity
    rows_by_name = {
        "budget_frontiers": tables.frontier_rows,
        "budget_levels": tables.budget_rows,
        "portfolio_analysis_metadata": tables.metadata_rows,
        "portfolio_sensing_statistics": tables.sensing_rows,
        "portfolio_statistics": tables.statistic_rows,
    }
    key_columns = {
        "budget_frontiers": ("budget_id", "portfolio_id"),
        "budget_levels": ("budget_id",),
        "portfolio_analysis_metadata": ("sample_artifact_id",),
        "portfolio_sensing_statistics": ("portfolio_id", "cell_id", "time_bin_id"),
        "portfolio_statistics": ("portfolio_id",),
    }
    artifact_tables = tuple(
        PartitionedTableData(
            name=name,
            schema=_ANALYSIS_SCHEMAS[name],
            partition_axes=(),
            rows_by_partition={(): rows_by_name[name]},
            key_columns=key_columns[name],
        )
        for name in sorted(rows_by_name)
    )
    dependency = ArtifactDependency(
        role="portfolio_samples",
        artifact_id=samples.artifact_id,
        content_hash=samples.content_hash,
    )
    portfolio_projection = scientific_projection(config)
    if config.utility.temporal_interval_s is None:
        portfolio_projection["utility"].pop("temporal_interval_s", None)
    resolved_config = {
        "portfolio": portfolio_projection,
        "sample_artifact_id": samples.artifact_id,
        "resolved_budget_levels_minor": list(tables.resolved_budgets),
        "replications_R": tables.replications_R,
        "sampling_rounds_J": tables.sampling_rounds_J,
        "variability_interpretation": tables.variability_interpretation,
        "sample_matrix_reuse": "immutable_portfolio_samples_dependency",
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
            "frontier": PORTFOLIO_FRONTIER_VERSION,
            "sensing_statistics": PORTFOLIO_SENSING_STATISTICS_VERSION,
            "statistics": PORTFOLIO_STATISTICS_VERSION,
            "storage": PORTFOLIO_ANALYSIS_STORAGE_VERSION,
        },
        random_stream_version=source_identity.random_stream_version,
        catalog_hash=source_identity.catalog_hash,
        replication_set_hash=source_identity.replication_set_hash,
        grid_axis_hash=source_identity.grid_axis_hash,
        time_axis_hash=source_identity.time_axis_hash,
    )
    reference = publish_partitioned_artifact(
        artifact_root=Path(artifact_root),
        collection="portfolios",
        identity=identity,
        dependencies=(dependency,),
        tables=artifact_tables,
    ).reference
    return PortfolioAnalysisResult(
        reference=reference,
        sample_reference=samples,
        replications_R=tables.replications_R,
        sampling_rounds_J=tables.sampling_rounds_J,
        count_portfolios=len(tables.statistic_rows),
        budget_levels=len(tables.budget_rows),
        frontier_memberships=len(tables.frontier_rows),
        sensing_statistic_rows=len(tables.sensing_rows),
    )


class PortfolioAnalysisArtifactReader:
    """Read M08B tables and the complete immutable M08A export dependency."""

    def __init__(self, artifact_root: str | Path, reference: ArtifactRef) -> None:
        self.artifact_root = Path(artifact_root)
        self.artifact = verify_partitioned_artifact(
            artifact_root=self.artifact_root,
            collection="portfolios",
            reference=reference,
        )
        if reference.artifact_kind != "portfolio":
            raise ValueError("portfolio analysis reader requires a portfolio artifact")
        if (
            self.artifact.manifest.scientific_identity.algorithm_versions.get("storage")
            not in PORTFOLIO_ANALYSIS_STORAGE_VERSIONS
        ):
            raise ValueError("reference is not an M08B portfolio analysis artifact")
        dependency = next(
            (
                item
                for item in self.artifact.manifest.dependencies
                if item.role == "portfolio_samples"
            ),
            None,
        )
        if dependency is None:
            raise ValueError("portfolio analysis lacks its M08A sample dependency")
        self.sample_reference = ArtifactRef(
            artifact_id=dependency.artifact_id,
            artifact_kind="portfolio",
            content_hash=dependency.content_hash,
        )
        self.samples = PortfolioArtifactReader(self.artifact_root, self.sample_reference)

    def read(self, table_name: str) -> pa.Table:
        """Read a derived table or delegate a complete M08A count/draw/matrix table."""

        schema = _ANALYSIS_SCHEMAS.get(table_name)
        if schema is not None:
            version = self.artifact.manifest.scientific_identity.algorithm_versions["storage"]
            if (
                version != PORTFOLIO_ANALYSIS_STORAGE_VERSION
                and table_name == "portfolio_analysis_metadata"
            ):
                schema = pa.schema(
                    [
                        field
                        for field in schema
                        if field.name != "sensing_statistics_mode"
                        and (
                            version != "portfolio-analysis-parquet@1" or field.name != "risk_metric"
                        )
                    ]
                )
            if version == "portfolio-analysis-parquet@1" and table_name == "budget_frontiers":
                schema = pa.schema(
                    [field for field in schema if field.name != "risk_comparison_key"]
                )
            return read_partitioned_table(self.artifact, table_name=table_name, schema=schema)
        return self.samples.read(table_name)
