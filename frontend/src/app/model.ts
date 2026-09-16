import type { ArtifactRef, EnvironmentEditor, EnvironmentResult, ProjectConfig, ProjectResolutionResult, RunOptions } from "../api/generated";

// Local view state is not a second scientific configuration contract.
// Project revisions contain only the generated ProjectConfig.
export interface WorkbenchDraft {
  authoring?: ProjectConfig;
  environment: { editor?: EnvironmentEditor; result?: EnvironmentResult; jobId?: string; prepared: ArtifactRef | null };
  resolutionJobId?: string;
  resolution?: ProjectResolutionResult;
  migrationNotices?: ReadonlyArray<string>;
  runOptions?: RunOptions;
}
export const defaultDraft = (): WorkbenchDraft => {
  let options: RunOptions | undefined;
  try { options = JSON.parse(localStorage.getItem("mobile-sensing-run-options@1") ?? "null") ?? undefined; } catch { /* Use server defaults for invalid local settings. */ }
  return { environment: { prepared: null }, runOptions: options };
};
export function fromProjectConfig(config: ProjectConfig): WorkbenchDraft {
  return { ...defaultDraft(), authoring: config, environment: { editor: config.environment, result: config.prepared_environment ?? undefined, prepared: config.prepared_environment?.artifact ?? null } };
}
