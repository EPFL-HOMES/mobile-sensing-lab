import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Accordion, AccordionSummary, AccordionDetails, MenuItem, Alert, Button, Paper, Stack, TextField, Typography } from "@mui/material";
import { requestJson } from "../api/client";
import type { ApiError, JobSnapshot } from "../api/client";
import type { RunOptions, RunView, SimulationEditor, TemporalPreview } from "../api/generated";
import { useWorkspace } from "../app/workspace";
import { Page } from "../components/Page";
import { JobMonitor } from "../components/JobMonitor";
import { ScientificChart } from "../components/ScientificChart";
import { RunManagement } from "./RunManagement";
import { ApiIssues } from "../components/ApiIssues";

export function SimulationPage() {
  const workspace = useWorkspace();
  const config = workspace.draft.authoring;
  const simulation = config?.simulation;
  const optionDefaults = useQuery({ queryKey: ["run-options"], queryFn: () => requestJson<RunOptions>("/api/v1/workbench/run-options") });
  const options = workspace.draft.runOptions ?? optionDefaults.data ?? { workers: 2, memory_limit_bytes: 4294967296, job_timeout_s: 7200 };
  const [name, setName] = useState("Full-day simulation");
  const jobs = useQuery({ queryKey: ["jobs", workspace.project?.project_id], enabled: Boolean(workspace.project),
    queryFn: () => requestJson<JobSnapshot[]>(`/api/v1/jobs?project_id=${workspace.project?.project_id}`), refetchInterval: 1500 });
  const runs = useQuery({ queryKey: ["runs", workspace.project?.project_id, jobs.data?.filter((job) => job.status === "completed").length], enabled: Boolean(workspace.project),
    queryFn: () => requestJson<RunView[]>(`/api/v1/workbench/runs?project_id=${workspace.project?.project_id}`) });
  const relevant = jobs.data?.filter((job) => job.kind === "studio_run") ?? [];
  const running = relevant.some((job) => !["completed", "failed", "cancelled"].includes(job.status));
  const update = (patch: Partial<SimulationEditor>) => workspace.updateDraft((draft) => {
    if (draft.authoring) draft.authoring.simulation = { ...draft.authoring.simulation, ...patch };
    return draft;
  });
  const updateWorkers = (workers: number) => workspace.updateDraft((draft) => {
    draft.runOptions = { ...options, workers };
    localStorage.setItem("mobile-sensing-run-options@1", JSON.stringify(draft.runOptions));
    return draft;
  });
  const preview = useQuery<TemporalPreview, ApiError>({ queryKey: ["temporal-preview", config], enabled: false, retry: false,
    queryFn: () => requestJson<TemporalPreview>("/api/v1/workbench/temporal-preview", { method: "POST", body: JSON.stringify({ config }) }) });
  const run = () => workspace.submitJob("/api/v1/workbench/runs", "studio_run", { name, config, options });
  return <Page title="Simulation" description="Run validation, vehicle operations and sensing exposure in one background job. The configuration is saved automatically."
    actions={<Button component={Link} to="/portfolio" disabled={!runs.data?.length}>Next: Portfolio</Button>}>
    <ApiIssues error={workspace.error} onClear={workspace.clearError} />
    {!config?.prepared_environment && <Alert severity="info">Prepare the shared environment. <Link to="/environment">Open Environment</Link></Alert>}
    {!config?.fleets?.length && <Alert severity="info">Configure at least one fleet. <Link to="/fleets">Open Fleet Configuration</Link></Alert>}
    {simulation && <Paper variant="outlined" sx={{ p: 3 }}><Stack gap={2}>
      <TextField label="Run name" value={name} onChange={(e) => setName(e.target.value)} />
      <div className="form-grid">
        <TextField select label="Simulation day" value={simulation.time_mode ?? "calendar"} onChange={(e) => update({ time_mode: e.target.value as SimulationEditor["time_mode"] })}><MenuItem value="relative">Generic day (no calendar)</MenuItem><MenuItem value="weekday">Representative weekday</MenuItem><MenuItem value="calendar">Exact calendar date</MenuItem></TextField>
        {simulation.time_mode === "weekday" && <TextField select label="Weekday" value={simulation.weekday ?? 2} onChange={(e) => update({ weekday: Number(e.target.value) })}>{["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"].map((day, i) => <MenuItem key={day} value={i}>{day}</MenuItem>)}</TextField>}
        {(!simulation.time_mode || simulation.time_mode === "calendar") && <TextField label="Date" type="date" value={simulation.service_date} onChange={(e) => update({ service_date: e.target.value })} slotProps={{ inputLabel: { shrink: true } }} />}
        <TextField label="Observation starts" value={simulation.start_time} onChange={(e) => update({ start_time: e.target.value })} />
        <TextField label="Observation ends" value={simulation.end_time} onChange={(e) => update({ end_time: e.target.value })} helperText="24:00 ends the selected natural day." />
        <TextField label="Simulation replications" type="number" value={simulation.replications} onChange={(e) => update({ replications: Number(e.target.value) })} />
        <TextField label="Simulation reporting interval (minutes)" type="number" value={simulation.temporal_resolution_minutes} onChange={(e) => update({ temporal_resolution_minutes: Number(e.target.value) })} helperText="Aggregation unit for exposure statistics and time-series charts. It does not define the Portfolio utility interval." />
        <TextField label="Simulation seed" type="number" value={simulation.seed} onChange={(e) => update({ seed: Number(e.target.value) })} />
        <TextField label="Simulation worker processes" type="number" value={options.workers ?? 2} onChange={(e) => updateWorkers(Number(e.target.value))} helperText="Parallel replication workers. Default: 2; memory use grows approximately with this value." inputProps={{ min: 1, max: 32 }} />
      </div>
      <Accordion><AccordionSummary>Calendar, warm-up and reproducibility</AccordionSummary><AccordionDetails><Stack gap={2}>
        {simulation.time_mode === "weekday" && <div className="form-grid">{(["calendar_period_start", "calendar_period_end"] as const).map((field) => <TextField key={field} type="date" label={field === "calendar_period_start" ? "Service period starts (optional)" : "Service period ends (optional)"} value={simulation[field] ?? ""} onChange={(e) => update({ [field]: e.target.value || null })} slotProps={{ inputLabel: { shrink: true } }} />)}</div>}
        {simulation.time_mode === "calendar" && <TextField label="Timezone" value={simulation.timezone ?? config?.prepared_environment?.timezone ?? config?.environment?.timezone ?? "UTC"} onChange={(e) => update({ timezone: e.target.value })} />}
        <TextField label="Generated-demand warm-up (hours)" type="number" value={simulation.warmup_hours ?? 0} onChange={(e) => update({ warmup_hours: Number(e.target.value) })} helperText="Optional, up to 48 hours. Requires a full-day observation; repeats prior-day demand on independent streams. Configure preceding-day vehicles in Supply. Pre-run exposure is excluded." />
      </Stack></AccordionDetails></Accordion>
      <Button disabled={preview.isFetching} onClick={() => void preview.refetch()}>{preview.isFetching ? "Resolving inputs…" : "Preview service day and daily profiles"}</Button>
      <ApiIssues error={preview.error} />
      {preview.data && <><Alert severity="info">{preview.data.calendar.interpretation} {preview.data.calendar.time_mode !== "relative" && `Resolved date: ${preview.data.calendar.resolved_date}.`} {preview.data.calendar.candidate_days > 0 && `${preview.data.calendar.candidate_days} covered candidate days.`}</Alert>
        {preview.data.fleets.map((fleet) => <Stack key={fleet.fleet_id}><Typography fontWeight={700}>{config?.fleets?.find((f) => f.fleet_id === fleet.fleet_id)?.name} · Expected tasks: {fleet.expected_task_total.toFixed(1)}</Typography>
          <ScientificChart xTitle="Elapsed hour of observation day" yTitle="Expected tasks per bin" traces={[{ x: fleet.bins.map((bin) => bin.start_hour), y: fleet.bins.map((bin) => bin.expected_tasks), type: "bar", name: "Task arrivals" }]} fallback={<Typography>{fleet.interpretation}</Typography>} />
          {fleet.bins.some((bin) => bin.expected_active_vehicles != null) && <ScientificChart xTitle="Elapsed hour of observation day" yTitle="Mean vehicles on shift" traces={[{ x: fleet.bins.map((bin) => bin.start_hour), y: fleet.bins.map((bin) => bin.expected_active_vehicles ?? 0), name: "Active supply", mode: "lines" }]} fallback={<Typography>{fleet.interpretation}</Typography>} />}
          <Typography color="text.secondary">{fleet.interpretation}</Typography></Stack>)}
      </>}
      <Typography color="text.secondary">Changing the reporting interval reuses completed movement but recalculates exposure bins and downstream analysis. Sensing states and depot exclusions are configured within each fleet.</Typography>
      <Button variant="contained" disabled={Boolean(config?.read_only) || !workspace.project || !config?.prepared_environment || !config.fleets?.length || running || !name.trim()} onClick={() => void run().catch(() => undefined)}>Run simulation</Button>
    </Stack></Paper>}
    {relevant.filter((job) => job.status !== "completed").slice(0, 5).map((job) => <JobMonitor key={job.job_id} jobId={job.job_id} sourceRevisionId={null} />)}
    {workspace.project && <RunManagement runs={runs.data ?? []} projectId={workspace.project.project_id} />}
  </Page>;
}
