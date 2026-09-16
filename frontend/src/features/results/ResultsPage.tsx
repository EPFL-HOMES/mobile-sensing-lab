import { Alert, Tab, Tabs } from "@mui/material";
import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import type { JobSnapshot } from "../../api/client";
import type { RunView, AnalysisView, ArtifactRef } from "../../api/generated";
import { requestJson } from "../../api/client";
import { useWorkspace } from "../../app/workspace";
import { Page } from "../../components/Page";
import { FleetResults } from "./FleetResults";
import { PortfolioResults } from "./PortfolioResults";

type ResultsView = "fleet" | "portfolio";

export function ResultsPage() {
  const workspace = useWorkspace();
  const [params, setParams] = useSearchParams();
  const jobs = useQuery({
    queryKey: ["jobs", workspace.project?.project_id],
    enabled: Boolean(workspace.project),
    queryFn: () => requestJson<JobSnapshot[]>(`/api/v1/jobs?project_id=${workspace.project?.project_id}`),
  });
  const view: ResultsView = params.get("view") === "portfolio" ? "portfolio" : "fleet";
  const runs = useQuery({ queryKey: ["runs", workspace.project?.project_id], enabled: Boolean(workspace.project), queryFn: () => requestJson<RunView[]>(`/api/v1/workbench/runs?project_id=${workspace.project?.project_id}`) });
  const analyses = useQuery({ queryKey: ["analyses", workspace.project?.project_id], enabled: Boolean(workspace.project), queryFn: () => requestJson<AnalysisView[]>(`/api/v1/workbench/analyses?project_id=${workspace.project?.project_id}`) });
  // Adapt completed named sources to the existing result selectors. These are
  // view projections, not new execution records or newly attributed results.
  const sourceView = (id: string, artifact: ArtifactRef, record: RunView | AnalysisView): JobSnapshot => ({ job_id: id, resource_id: artifact.artifact_id,
    kind: artifact.artifact_kind === "simulation" ? "simulation" : artifact.artifact_kind === "exposure" ? "exposure" : "portfolio", project_id: workspace.project?.project_id ?? null, request_hash: artifact.content_hash,
    status: "completed", phase: "completed", attempt: 1, cancel_requested: false, counters: {}, result: { ...record, artifact, ...(artifact.artifact_kind === "portfolio" ? { portfolio_stage: "analysis" } : {}) },
    error_code: null, error_message: null, cache_source_job_id: null, created_at_utc: "", updated_at_utc: "" });
  const named = [...(runs.data ?? []).flatMap((run) => [sourceView(run.run_id + ":operations", run.simulation, run), sourceView(run.run_id + ":sensing", run.exposure, run)]),
    ...(analyses.data ?? []).map((analysis) => sourceView(analysis.analysis_id, analysis.frontier, analysis))];
  const completed = [...named, ...(jobs.data ?? []).filter((job) => job.status === "completed" && !job.kind.startsWith("studio_"))];
  const revisionForJob = (jobId: string) => (completed.find((job) => job.job_id === jobId)?.result?.source_revision_id as string | null) ?? workspace.jobRevisionIds[jobId] ?? null;
  const changeView = (next: ResultsView) => { const query = new URLSearchParams(params); query.set("view", next); setParams(query, { replace: true }); };
  return <Page title="Results" description="Average fleet performance and sensor-count portfolios, bound to their saved source settings.">
    {workspace.dirty && <Alert severity="info" sx={{ mb: 2 }}>Results use their saved source settings. Pending edits have not changed these results.</Alert>}
    <Tabs value={view} onChange={(_, value: ResultsView) => changeView(value)} sx={{ mb: 2 }}>
      <Tab value="fleet" label="Fleet results" />
      <Tab value="portfolio" label="Portfolio" />
    </Tabs>
    {!workspace.project ? <Alert severity="info">Open a project to inspect its results.</Alert> : jobs.error ? <Alert severity="error">{String(jobs.error)}</Alert> : view === "fleet" ? <FleetResults runs={runs.data ?? []} jobs={completed} revisionForJob={revisionForJob} preferredRun={new URLSearchParams((workspace.draft.authoring?.saved_views?.["Fleet results"] ?? "").split("?")[1]).get("fleet_run") ?? undefined} /> : <PortfolioResults jobs={completed} revisionForJob={revisionForJob} preferredView={workspace.draft.authoring?.saved_views?.Portfolio} />}
  </Page>;
}
