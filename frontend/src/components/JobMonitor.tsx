import CancelRounded from "@mui/icons-material/CancelRounded";
import CheckCircleRounded from "@mui/icons-material/CheckCircleRounded";
import ErrorRounded from "@mui/icons-material/ErrorRounded";
import HourglassTopRounded from "@mui/icons-material/HourglassTopRounded";
import SyncRounded from "@mui/icons-material/SyncRounded";
import {
  Alert,
  Box,
  Button,
  Chip,
  LinearProgress,
  Paper,
  Stack,
  Typography,
} from "@mui/material";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { requestJson } from "../api/client";
import type { JobSnapshot } from "../api/client";

const terminal = new Set(["completed", "failed", "cancelled"]);

const statusColor = (status: JobSnapshot["status"]) => {
  if (status === "completed") return "success" as const;
  if (status === "failed" || status === "cancelled") return "error" as const;
  if (status === "queued") return "default" as const;
  return "primary" as const;
};

export const jobPhaseLabel = (kind: JobSnapshot["kind"], phase: string) => {
  if (kind === "studio_environment") {
    if (phase === "queued" || phase === "initializing" || phase === "running") return "Starting environment worker";
    if (phase === "environment.validate") return "Step 1 of 8 · Validate environment settings";
    if (phase === "environment.boundary.osm") return "Step 2 of 8 · Download study boundary from OpenStreetMap";
    if (phase === "environment.boundary.local") return "Step 2 of 8 · Load registered study boundary";
    if (phase === "environment.boundary.drawn") return "Step 2 of 8 · Validate drawn study boundary";
    if (phase === "environment.crs") return "Step 3 of 8 · Resolve the metric coordinate system";
    if (phase === "environment.network.osm") return "Step 4 of 8 · Download road network from OpenStreetMap";
    if (phase.startsWith("environment.network.osm.tile_")) {
      const tile = phase.match(/tile_(\d+)_of_(\d+)/);
      const server = phase.match(/server_\d+_of_\d+\.([a-z0-9_-]+)$/);
      return `Step 4 of 8 · Download road tile ${tile?.[1]} of ${tile?.[2]}${server ? ` (${server[1].replaceAll("_", ".")})` : ""}`;
    }
    if (phase.startsWith("environment.network.osm.server_")) {
      const match = phase.match(/server_(\d+)_of_(\d+)\.([a-z0-9_-]+)$/);
      const server = match?.[3].replaceAll("_", ".") ?? "Overpass";
      return `Step 4 of 8 · Download road network · server ${match?.[1] ?? "?"} of ${match?.[2] ?? "?"} (${server})`;
    }
    if (phase === "environment.network.osm.subdivide") return "Step 4 of 8 · Query timed out; retry smaller road tiles (completed tiles are cached)";
    if (phase === "environment.network.osm.assemble") return "Step 4 of 8 · Road tiles downloaded; assemble complete network";
    if (phase === "environment.network.local") return "Step 4 of 8 · Load registered road network";
    if (phase === "environment.speed") return "Step 5 of 8 · Apply the road-speed model";
    if (phase === "environment.acquire.hash_local_resources") return "Step 6 of 8 · Verify staged geographic inputs";
    if (phase === "environment.prepare.load_boundaries") return "Step 6 of 8 · Prepare boundary and routing extent";
    if (phase === "environment.prepare.prepare_network") return "Step 6 of 8 · Build the directed routing network";
    if (phase === "environment.prepare.prepare_grid") return "Step 6 of 8 · Build the sensing grid";
    if (phase === "environment.prepare.publish_environment") return "Step 6 of 8 · Publish the routing environment";
    if (phase === "environment.core") return "Step 6 of 8 · Prepare routing and sensing geometry";
    if (phase === "environment.features.local") return "Step 7 of 8 · Aggregate registered spatial features";
    if (phase.startsWith("environment.features.osm.")) {
      const detail = phase.slice("environment.features.osm.".length);
      const tile = detail.match(/^([a-z_]+)\.tile_(\d+)_of_(\d+)(?:\.server_\d+_of_\d+\.([a-z0-9_-]+))?$/);
      if (tile) return `Step 7 of 8 · Download OSM ${tile[1] === "batch" ? "feature" : tile[1].replaceAll("_", " ")} tile ${tile[2]} of ${tile[3]}${tile[4] ? ` (${tile[4].replaceAll("_", ".")})` : ""}`;
      if (detail.endsWith(".subdivide")) return "Step 7 of 8 · Feature query timed out; retry smaller tiles";
      const [category, attempt] = detail.split(".server_");
      const source = category === "batch" ? "selected spatial features" : category.replaceAll("_", " ");
      if (attempt) {
        const match = attempt.match(/(\d+)_of_(\d+)\.([a-z0-9_-]+)$/);
        const server = match?.[3].replaceAll("_", ".") ?? "Overpass";
        return `Step 7 of 8 · Download OSM ${source} · server ${match?.[1] ?? "?"} of ${match?.[2] ?? "?"} (${server})`;
      }
      return `Step 7 of 8 · Download and aggregate OSM ${source}`;
    }
    if (phase === "environment.publish_features") return "Step 8 of 8 · Publish spatial features and provenance";
    if (phase === "environment.complete" || phase === "finalizing") return "Step 8 of 8 · Finalize the prepared environment";
  }
  if (phase.startsWith("planning.costs.")) return `Road travel-time costs · ${phase.slice("planning.costs.".length)}`;
  if (phase.startsWith("resolve.reuse.")) return `Reuse prepared ${phase.slice("resolve.reuse.".length).replaceAll("_", " ")}`;
  if (phase === "resolve.replications") return "Prepare simulation replications";
  if (phase.startsWith("check.")) return "Check configuration and input references";
  return phase;
};

export function JobMonitor({
  jobId,
  sourceRevisionId,
  onCompleted,
  onTerminal,
}: {
  jobId: string;
  sourceRevisionId: string | null;
  onCompleted?: (job: JobSnapshot) => void;
  onTerminal?: (job: JobSnapshot) => void;
}) {
  const queryClient = useQueryClient();
  const [connection, setConnection] = useState<"connecting" | "live" | "polling">("connecting");
  const completionNotified = useRef<string | null>(null);
  const query = useQuery({
    queryKey: ["job", jobId],
    queryFn: () => requestJson<JobSnapshot>(`/api/v1/jobs/${jobId}`),
    refetchInterval: (value) => (terminal.has(value.state.data?.status ?? "") ? false : 1000),
  });

  const terminalNotified = useRef<string | null>(null);
  const status = query.data?.status;
  useEffect(() => {
    if (query.data && terminal.has(query.data.status) && terminalNotified.current !== jobId) { terminalNotified.current = jobId; onTerminal?.(query.data); }
  }, [onTerminal, query.data, jobId]);
  useEffect(() => {
    if (query.data?.status === "completed" && completionNotified.current !== jobId) {
      completionNotified.current = jobId;
      onCompleted?.(query.data);
    }
  }, [onCompleted, query.data]);

  useEffect(() => {
    if (status && terminal.has(status)) return;
    if (typeof EventSource === "undefined") {
      setConnection("polling");
      return;
    }
    let projectId = "";
    try { projectId = JSON.parse(localStorage.getItem("mobile-sensing-workbench@1") ?? "{}").activeProjectId ?? ""; } catch { /* Recover by unique job identity. */ }
    const stream = new EventSource(`/api/v1/jobs/${jobId}/events${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ""}`);
    stream.onopen = () => setConnection("live");
    stream.onmessage = () => void queryClient.invalidateQueries({ queryKey: ["job", jobId] });
    for (const event of ["status", "progress", "completed", "failed", "cancelled", "reset"]) {
      stream.addEventListener(event, () => {
        void queryClient.invalidateQueries({ queryKey: ["job", jobId] });
      });
    }
    stream.onerror = () => {
      setConnection("polling");
      stream.close();
    };
    return () => stream.close();
  }, [jobId, queryClient, status]);

  const cancel = async () => {
    await requestJson<JobSnapshot>(`/api/v1/jobs/${jobId}/cancel`, { method: "POST" });
    await query.refetch();
  };
  const retry = async () => {
    await requestJson(`/api/v1/jobs/${jobId}/retry`, { method: "POST" });
    completionNotified.current = null;
    await query.refetch();
    void queryClient.invalidateQueries({ queryKey: ["jobs"] });
  };
  const job = query.data;
  if (query.isLoading) return <LinearProgress aria-label="Loading job" />;
  if (!job) return <Alert severity="error">Job {jobId} could not be restored.</Alert>;
  const completed = job.counters.completed ?? 0;
  const total = job.counters.total;
  const percentage = total ? Math.min(100, (100 * completed) / total) : undefined;
  const icon =
    job.status === "completed" ? (
      <CheckCircleRounded />
    ) : job.status === "failed" ? (
      <ErrorRounded />
    ) : job.status === "cancelled" ? (
      <CancelRounded />
    ) : (
      <HourglassTopRounded />
    );
  return (
    <Paper variant="outlined" sx={{ p: 2 }} data-testid={`job-${jobId}`}>
      <Stack direction="row" justifyContent="space-between" alignItems="center" gap={2}>
        <Stack direction="row" alignItems="center" gap={1} minWidth={0}>
          {icon}
          <Box minWidth={0}>
            <Typography fontWeight={700}>{({ studio_run: "Simulation and sensing", studio_analysis: "Portfolio analysis", studio_environment: "Environment preparation", studio_resolve: "Fleet validation" } as Record<string, string>)[job.kind] ?? job.kind.replaceAll("_", " ")}</Typography>
          </Box>
        </Stack>
        <Stack direction="row" gap={1} alignItems="center">
          {!terminal.has(job.status) && connection === "polling" && (
            <Chip icon={<SyncRounded />} label="Reconnecting · polling" color="warning" />
          )}
          <Chip label={job.cancel_requested && !terminal.has(job.status) ? "Cancelling" : job.status} color={statusColor(job.status)} />
        </Stack>
      </Stack>
      {!terminal.has(job.status) && <LinearProgress variant={percentage ? "determinate" : "indeterminate"} value={percentage} sx={{ my: 1.5 }} />}
      <Stack direction={{ xs: "column", sm: "row" }} justifyContent="space-between" gap={1}>
        <Typography variant="body2" color="text.secondary">
          Current step: {jobPhaseLabel(job.kind, job.phase)}
          {total != null && job.status !== "completed" ? ` · ${completed}/${total}` : ""}
        </Typography>
        <Typography variant="caption" color="text.secondary">
          Attempt {job.attempt}{sourceRevisionId || job.result?.source_revision_id ? " · Saved source configuration retained" : ""}
        </Typography>
      </Stack>
      {job.error_message && <Alert severity="error" sx={{ mt: 1.5 }}>{job.error_code}: {job.error_message}</Alert>}
      {job.status === "completed" && ["export","project_export"].includes(job.kind) && job.resource_id && <Button component="a" href={`/api/v1/artifacts/${encodeURIComponent(job.resource_id)}/download`} download size="small">Download exported file</Button>}
      {["failed", "cancelled"].includes(job.status) && <Button onClick={() => void retry().catch(() => undefined)} size="small">Retry same configuration</Button>}
      {!terminal.has(job.status) && (
        <Button onClick={() => void cancel().catch(() => undefined)} color="error" size="small" sx={{ mt: 1 }}>
          Request cancellation
        </Button>
      )}
    </Paper>
  );
}
