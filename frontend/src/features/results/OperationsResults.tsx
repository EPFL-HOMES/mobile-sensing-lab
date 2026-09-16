import {
  Alert,
  Box,
  Chip,
  FormControl,
  InputLabel,
  MenuItem,
  Paper,
  Select,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  TextField,
  Typography,
} from "@mui/material";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo } from "react";
import { useSearchParams } from "react-router-dom";
import type { JobSnapshot } from "../../api/client";
import { requestJson } from "../../api/client";
import { ScientificMap } from "../../components/ScientificMap";
import { ExportActions } from "./ExportActions";
import { queryString, resultTable } from "./api";
import { ResultSourceSelect } from "./ResultSourceSelect";
import type { ArtifactManifestValue, GeoJsonValue } from "./types";
import { artifactFromJob, dependency } from "./types";

interface ReplicationRow { replication_id: string; complete: boolean; }
interface VehicleRow { fleet_id: string; vehicle_id: string; }
interface TaskRow {
  task_id: string; kind: string; status: string; release_s: number; vehicle_id: string | null;
  assignment_wait_s: number | null; first_service_wait_s: number | null; lateness_s: number | null;
}
interface ActivityRow { activity_index: number; start_s: number; end_s: number; activity_kind: string; movement_kind: string | null; vehicle_id: string; task_id: string | null; }
interface OperationSummary {
  released_task_count: number;
  carry_in_task_count: number;
  status_counts: Record<string, number>;
  assignment_wait_denominator: number;
  mean_assignment_wait_s: number | null;
  active_time_utilization: number | null;
  first_service_wait_denominator: number;
  mean_first_service_wait_s: number | null;
  service_movement_time_s: number;
  operational_movement_time_s: number;
  complete: true;
}

function FieldSelect({ label, value, values, onChange }: { label: string; value: string; values: string[]; onChange: (value: string) => void }) {
  return <FormControl size="small" sx={{ minWidth: 150 }}><InputLabel>{label}</InputLabel><Select inputProps={{ "aria-label": label }} label={label} value={value} onChange={(event) => onChange(event.target.value)}>{values.map((item) => <MenuItem key={item} value={item}>{item || "All"}</MenuItem>)}</Select></FormControl>;
}

function Kpi({ label, value, detail }: { label: string; value: string | number; detail?: string }) {
  return <Paper variant="outlined" sx={{ p: 1.75 }}><Typography variant="caption" color="text.secondary">{label}</Typography><Typography variant="h5" sx={{ my: 0.25 }}>{value}</Typography>{detail && <Typography variant="caption" color="text.secondary">{detail}</Typography>}</Paper>;
}

export function OperationsResults({ jobs, revisionForJob }: { jobs: JobSnapshot[]; revisionForJob: (jobId: string) => string | null }) {
  const [params, setParams] = useSearchParams();
  const sources = jobs.filter((job) => artifactFromJob(job)?.artifact_kind === "simulation");
  const fallbackId = artifactFromJob(sources[0] ?? ({} as JobSnapshot))?.artifact_id ?? "";
  const resourceId = sources.some((job) => artifactFromJob(job)?.artifact_id === params.get("resource")) ? params.get("resource")! : fallbackId;
  const set = (key: string, value: string) => { const next = new URLSearchParams(params); value ? next.set(key, value) : next.delete(key); setParams(next, { replace: true }); };
  const manifest = useQuery({ queryKey: ["simulation-manifest", resourceId], enabled: Boolean(resourceId), queryFn: () => requestJson<ArtifactManifestValue>(`/api/v1/simulations/${resourceId}`) });
  const environmentId = dependency(manifest.data, "environment")?.artifact_id ?? "";
  const boundary = useQuery({ queryKey: ["operations-boundary", environmentId], enabled: Boolean(environmentId), queryFn: () => requestJson<GeoJsonValue>(`/api/v1/maps/${environmentId}/boundary`) });
  const replications = useQuery({ queryKey: ["result", resourceId, "replications"], enabled: Boolean(resourceId), queryFn: () => resultTable<ReplicationRow>(resourceId, "replication_status") });
  const catalog = useQuery({ queryKey: ["result", resourceId, "catalog"], enabled: Boolean(resourceId), queryFn: () => resultTable<VehicleRow>(resourceId, "vehicle_catalog") });
  const replicationIds = replications.data?.items.map((row) => row.replication_id) ?? [];
  const replication = replicationIds.includes(params.get("replication") ?? "") ? params.get("replication")! : (replicationIds[0] ?? "");
  const fleets = ["", ...new Set(catalog.data?.items.map((row) => row.fleet_id) ?? [])];
  const fleet = fleets.includes(params.get("fleet") ?? "") ? params.get("fleet") ?? "" : "";
  const vehicles = ["", ...(catalog.data?.items.filter((row) => !fleet || row.fleet_id === fleet).map((row) => row.vehicle_id) ?? [])];
  const vehicle = vehicles.includes(params.get("vehicle") ?? "") ? params.get("vehicle") ?? "" : "";
  const sourceClock = ((manifest.data?.scientific_identity.resolved_config.simulation as { scenario?: { clock?: { observation_start_s: number; end_s: number } } } | undefined)?.scenario?.clock);
  const observationStart = sourceClock?.observation_start_s ?? 0;
  const timeStart = params.get("time_start") ?? String(observationStart);
  const timeEnd = params.get("time_end") ?? (sourceClock ? String(sourceClock.end_s) : "");
  const filters = { replication_id: replication, fleet_id: fleet, vehicle_id: vehicle, time_start_s: timeStart, time_end_s: timeEnd };
  const tasks = useQuery({ queryKey: ["operations-tasks", resourceId, filters], enabled: Boolean(resourceId && replication), queryFn: () => resultTable<TaskRow>(resourceId, "task_outcomes", { ...filters, page_size: 100 }) });
  const activities = useQuery({ queryKey: ["operations-activities", resourceId, filters], enabled: Boolean(resourceId && replication), queryFn: () => resultTable<ActivityRow>(resourceId, "activity_intervals", { ...filters, page_size: 100 }) });
  const movementMap = useQuery({ queryKey: ["operations-map", resourceId, filters], enabled: Boolean(resourceId && replication), queryFn: () => requestJson<GeoJsonValue>(`/api/v1/maps/${resourceId}/movements?${queryString({ ...filters, aggregation: vehicle ? null : "edge_usage" })}`) });
  const summary = useQuery({ queryKey: ["operations-summary", resourceId, filters], enabled: Boolean(resourceId && replication), queryFn: () => requestJson<OperationSummary>(`/api/v1/operation-summaries/${resourceId}?${queryString(filters)}`) });
  const mapContext = useMemo(() => boundary.data ? [boundary.data] : [], [boundary.data]);
  useEffect(() => { if (resourceId && params.get("resource") !== resourceId) set("resource", resourceId); }, [resourceId]);
  const complete = Boolean(summary.data?.complete);
  if (sources.length === 0) return <Alert severity="info">No completed simulation artifact is available for this project.</Alert>;
  const sourceJob = sources.find((job) => artifactFromJob(job)?.artifact_id === resourceId);
  return <Stack gap={2.5}>
    <Stack direction="row" gap={1.5} flexWrap="wrap" alignItems="center">
      <ResultSourceSelect label="Simulation run" jobs={sources} value={resourceId} onChange={(value) => set("resource", value)} />
      <FieldSelect label="Replication" value={replication} values={replicationIds} onChange={(value) => set("replication", value)} />
      <FieldSelect label="Fleet" value={fleet} values={fleets} onChange={(value) => { const next = new URLSearchParams(params); value ? next.set("fleet", value) : next.delete("fleet"); next.delete("vehicle"); setParams(next, { replace: true }); }} />
      <FieldSelect label="Vehicle" value={vehicle} values={vehicles} onChange={(value) => set("vehicle", value)} />
      <TextField size="small" label="From hour" helperText="Hours from observation start" type="number" value={timeStart ? (Number(timeStart) - observationStart) / 3600 : ""} onChange={(event) => set("time_start", event.target.value ? String(observationStart + Number(event.target.value) * 3600) : "")} sx={{ width: 150 }} />
      <TextField size="small" label="To hour" helperText="Hours from observation start" type="number" value={timeEnd ? (Number(timeEnd) - observationStart) / 3600 : ""} onChange={(event) => set("time_end", event.target.value ? String(observationStart + Number(event.target.value) * 3600) : "")} sx={{ width: 150 }} />
    </Stack>
    <Stack direction="row" gap={1} alignItems="center" flexWrap="wrap"><Chip label={`One joint replication: ${replication || "loading"}`} color="primary" variant="outlined" /><Typography variant="caption" className="mono">source {manifest.data?.content_fingerprint.slice(0, 12) ?? "…"}</Typography></Stack>
    {(tasks.error || activities.error || movementMap.error || summary.error || boundary.error) && <Alert severity="error">{String((tasks.error ?? activities.error ?? movementMap.error ?? summary.error ?? boundary.error) as Error)}</Alert>}
    {tasks.data && !tasks.data.is_complete && <Alert severity="info">Showing {tasks.data.returned_count} of {tasks.data.total_matching_count} task rows. Summary indicators use the complete selection. Export all rows or narrow vehicle and time filters.</Alert>}
    {complete && summary.data && <Box className="result-kpis"><Kpi label="Released in selected window" value={summary.data.released_task_count} detail={`${summary.data.carry_in_task_count} carry-in tasks active`} /><Kpi label="Completed" value={summary.data.status_counts.completed ?? 0} detail={Object.entries(summary.data.status_counts).filter(([status]) => status !== "completed").map(([status, count]) => `${status.replaceAll("_", " ")} ${count}`).join("; ") || "No other terminal statuses"} /><Kpi label="Mean assignment wait" value={summary.data.mean_assignment_wait_s === null ? "—" : `${summary.data.mean_assignment_wait_s.toFixed(2)} s`} detail={`${summary.data.assignment_wait_denominator} assigned tasks`} /><Kpi label="Mean first-service wait" value={summary.data.mean_first_service_wait_s === null ? "—" : `${summary.data.mean_first_service_wait_s.toFixed(2)} s`} detail={`${summary.data.first_service_wait_denominator} served tasks`} /><Kpi label="Active-time utilization" value={summary.data.active_time_utilization === null ? "—" : `${(summary.data.active_time_utilization * 100).toFixed(1)}%`} detail="Busy / recorded active interval" /><Kpi label="Movement composition" value={`${summary.data.service_movement_time_s.toFixed(1)} s service`} detail={`${summary.data.operational_movement_time_s.toFixed(1)} s operational`} /></Box>}
    <Box className="result-workspace">
      <Paper variant="outlined" sx={{ p: 2 }}><Typography variant="h6" sx={{ mb: 1.5 }}>{vehicle ? "Recorded vehicle path" : "Road usage in the selected window"}</Typography><ScientificMap loading={movementMap.isFetching} data={movementMap.data ?? null} contextData={mapContext} mode="movement" fallback={<ActivityTable rows={activities.data?.items ?? []} />} /></Paper>
      <Paper variant="outlined" sx={{ p: 2 }}><Typography variant="h6">Activity timeline</Typography><Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>Exact half-open intervals; no trajectory interpolation is stored in browser state.</Typography>{activities.data && !activities.data.is_complete && <Typography variant="caption">Showing {activities.data.returned_count} of {activities.data.total_matching_count} intervals. Narrow the vehicle/time selection or export all intervals.</Typography>}<ActivityTable rows={activities.data?.items ?? []} />{sourceJob && <ExportActions resourceId={resourceId} table="activity_intervals" sourceRevisionId={revisionForJob(sourceJob.job_id)} />}</Paper>
    </Box>
    <Paper variant="outlined" sx={{ p: 2 }}><Stack direction="row" justifyContent="space-between" gap={2}><Box><Typography variant="h6">Task outcomes</Typography><Typography variant="body2" color="text.secondary">Denominators remain explicit; unserved tasks are not assigned zero waits.</Typography></Box>{sourceJob && <ExportActions resourceId={resourceId} table="task_outcomes" sourceRevisionId={revisionForJob(sourceJob.job_id)} />}</Stack><TableContainer sx={{ mt: 1.5, maxHeight: 360 }}><Table size="small" stickyHeader><TableHead><TableRow><TableCell>Task</TableCell><TableCell>Kind</TableCell><TableCell>Status</TableCell><TableCell>Vehicle</TableCell><TableCell>Release s</TableCell><TableCell>Assignment wait s</TableCell><TableCell>First-service wait s</TableCell><TableCell>Lateness s</TableCell></TableRow></TableHead><TableBody>{(tasks.data?.items ?? []).map((row) => <TableRow key={row.task_id}><TableCell className="mono">{row.task_id}</TableCell><TableCell>{row.kind}</TableCell><TableCell>{row.status}</TableCell><TableCell>{row.vehicle_id ?? "—"}</TableCell><TableCell>{row.release_s}</TableCell><TableCell>{row.assignment_wait_s ?? "—"}</TableCell><TableCell>{row.first_service_wait_s ?? "—"}</TableCell><TableCell>{row.lateness_s ?? "—"}</TableCell></TableRow>)}</TableBody></Table></TableContainer></Paper>
  </Stack>;
}

function ActivityTable({ rows }: { rows: ActivityRow[] }) {
  return <TableContainer sx={{ maxHeight: 330 }}><Table size="small" stickyHeader><TableHead><TableRow><TableCell>Vehicle</TableCell><TableCell>Activity</TableCell><TableCell>Start</TableCell><TableCell>End</TableCell><TableCell>Task</TableCell></TableRow></TableHead><TableBody>{rows.map((row) => <TableRow key={`${row.vehicle_id}-${row.activity_index}`}><TableCell>{row.vehicle_id}</TableCell><TableCell>{row.movement_kind ?? row.activity_kind}</TableCell><TableCell>{row.start_s}</TableCell><TableCell>{row.end_s}</TableCell><TableCell className="mono">{row.task_id ?? "—"}</TableCell></TableRow>)}</TableBody></Table></TableContainer>;
}
