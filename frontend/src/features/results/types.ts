import type { JobSnapshot } from "../../api/client";

export interface ArtifactRefValue {
  artifact_id: string;
  artifact_kind: "dataset" | "environment" | "simulation" | "exposure" | "portfolio" | "export";
  content_hash: string;
}

export interface ArtifactDependencyValue {
  role: string;
  artifact_id: string;
  content_hash: string;
}

export interface ArtifactManifestValue {
  artifact_id: string;
  artifact_kind: ArtifactRefValue["artifact_kind"];
  content_fingerprint: string;
  dependencies: ArtifactDependencyValue[];
  expected_table_names: string[];
  complete: true;
  scientific_identity: {
    resolved_config: Record<string, unknown>;
    algorithm_versions: Record<string, string>;
  };
}

export interface ResultPageValue<T = Record<string, unknown>> {
  items: T[];
  returned_count: number;
  total_matching_count: number;
  next_cursor: string | null;
  is_complete: boolean;
}

export interface MatrixValue {
  replication_id?: string | null;
  cell_id: string;
  time_bin_id: string;
  value: number;
}

export interface MatrixTimeValue {
  replication_or_round_id?: string;
  time_bin_id: string;
  value: number;
}

export interface MatrixSliceValue {
  resource_id?: string;
  exposure_id?: string;
  kind: "vehicle_exposure" | "operational_aggregate" | "portfolio_sample" | "portfolio_summary";
  statistic: "realization" | "mean" | "variance" | "std" | "raw" | "sample_variance" | "sample_std";
  replication_ids?: string[];
  vehicle_keys?: Array<{ fleet_id: string; vehicle_id: string }>;
  cell_ids: string[];
  time_bin_ids: string[];
  expected_shape: number[];
  values: MatrixValue[];
  unit: "s" | "s^2";
  zero_fill: "absent_sparse_rows_are_zero";
  complete: boolean;
  replications_R: number;
  selected_replication_count?: number;
  sampling_rounds_J: number | null;
  portfolio_id?: string | null;
  round_id?: number | null;
  time_summary: MatrixTimeValue[];
  overall_value: number | null;
  positive_cell_count: number;
  coverage_denominator_cell_count?: number;
  mean_coverage_fraction?: number | null;
  coverage_p05_fraction?: number | null;
  coverage_p50_fraction?: number | null;
  coverage_p95_fraction?: number | null;
  coverage_semantics?: "mean_within_observation_any_time_spatial_coverage_road_intersecting_cells" | "mean_within_observation_any_time_spatial_coverage_prepared_grid";
  summary_semantics: "statistic_of_within_observation_cell_sum";
}

export interface GeoJsonValue {
  type: "FeatureCollection";
  features: Array<{
    type: "Feature";
    geometry: { type: string; coordinates: unknown };
    properties: Record<string, unknown>;
  }>;
  returned_count: number;
  total_matching_count: number;
  is_complete: boolean;
  crs: "EPSG:4326";
  aggregation: string | null;
  filters?: Record<string, unknown>;
}

export function artifactFromJob(job: JobSnapshot): ArtifactRefValue | null {
  const artifact = job.result?.artifact;
  if (!artifact || typeof artifact !== "object") return null;
  const candidate = artifact as Partial<ArtifactRefValue>;
  return candidate.artifact_id && candidate.artifact_kind && candidate.content_hash
    ? candidate as ArtifactRefValue
    : null;
}

export function dependency(manifest: ArtifactManifestValue | undefined, role: string) {
  return manifest?.dependencies.find((item) => item.role === role) ?? null;
}
