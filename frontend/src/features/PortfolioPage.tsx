import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Alert, Button, MenuItem, Paper, Stack, TextField, Typography } from "@mui/material";
import { requestJson } from "../api/client";
import type { JobSnapshot } from "../api/client";
import type { RunView, AnalysisView, PortfolioEditor, UtilityCurve, NumericRange } from "../api/generated";
import { useWorkspace } from "../app/workspace";
import { Page } from "../components/Page";
import { JobMonitor } from "../components/JobMonitor";
import { ApiIssues } from "../components/ApiIssues";
import { ScientificChart } from "../components/ScientificChart";

function NumberList({ label, values, onChange, onValidity }: { label: string; values: readonly number[]; onChange: (values: number[]) => void; onValidity: (valid: boolean) => void }) {
  const [text, setText] = useState(values.join(", "));
  const focused = useRef(false);
  const valid = /^\s*\d+(?:\.\d+)?\s*(?:,\s*\d+(?:\.\d+)?\s*)*$/.test(text);
  useEffect(() => { if (!focused.current) setText(values.join(", ")); }, [values.join(",")]);
  return <TextField label={label} value={text} error={!valid} helperText={label.startsWith("Budgets") ? "Comma-separated nonnegative amounts in the selected cost unit." : "Comma-separated nonnegative integer counts."} onFocus={() => { focused.current = true; }} onBlur={() => { focused.current = false; }} onChange={(event) => {
    const next = event.target.value; setText(next);
    const okay = /^\s*\d+(?:\.\d+)?\s*(?:,\s*\d+(?:\.\d+)?\s*)*$/.test(next);
    onValidity(okay); if (okay) onChange(next.split(",").map(Number));
  }} />;
}

function RangeFields({ value, onChange, label }: { value: NumericRange; onChange: (value: NumericRange) => void; label: string }) {
  return <>{(["minimum", "maximum", "step"] as const).map((field) => <TextField key={field} size="small" label={`${label} ${field}`} type="number" value={value[field]} onChange={(e) => onChange({ ...value, [field]: Number(e.target.value) })} />)}</>;
}

export function PortfolioPage() {
  const workspace = useWorkspace();
  const editor = workspace.draft.authoring?.portfolio;
  const [name, setName] = useState("Sensor portfolio analysis");
  const [invalidLists, setInvalidLists] = useState<Record<string, boolean>>({});
  const validity = (key: string) => (valid: boolean) => setInvalidLists((current) => ({ ...current, [key]: !valid }));
  const jobs = useQuery({ queryKey: ["jobs", workspace.project?.project_id], enabled: Boolean(workspace.project),
    queryFn: () => requestJson<JobSnapshot[]>(`/api/v1/jobs?project_id=${workspace.project?.project_id}`), refetchInterval: 1500 });
  const runs = useQuery({ queryKey: ["runs", workspace.project?.project_id], enabled: Boolean(workspace.project),
    queryFn: () => requestJson<RunView[]>(`/api/v1/workbench/runs?project_id=${workspace.project?.project_id}`) });
  const analyses = useQuery({ queryKey: ["analyses", workspace.project?.project_id, jobs.data?.filter((job) => job.status === "completed").length], enabled: Boolean(workspace.project),
    queryFn: () => requestJson<AnalysisView[]>(`/api/v1/workbench/analyses?project_id=${workspace.project?.project_id}`) });
  const selected = runs.data?.find((run) => run.run_id === editor?.source_run_id);
  const update = (patch: Partial<PortfolioEditor>) => workspace.updateDraft((draft) => {
    if (draft.authoring) draft.authoring.portfolio = { ...draft.authoring.portfolio, ...patch };
    return draft;
  });
  const curve = useQuery({ queryKey: ["utility-curve", editor?.utility, editor?.saturation_minutes], enabled: Boolean(editor && (editor.saturation_minutes ?? 0) > 0),
    queryFn: () => requestJson<UtilityCurve>("/api/v1/workbench/utility-curve", { method: "POST", body: JSON.stringify({ kind: editor?.utility, saturation_minutes: editor?.saturation_minutes }) }) });
  const chooseRun = (runId: string) => {
    setInvalidLists({});
    const run = runs.data?.find((value) => value.run_id === runId); if (!run) return;
    void requestJson<PortfolioEditor>(`/api/v1/workbench/runs/${runId}/portfolio-defaults`).then((config) => update(config));
  };
  const relevant = jobs.data?.filter((job) => job.kind === "studio_analysis") ?? [];
  const running = relevant.some((job) => !["completed", "failed", "cancelled"].includes(job.status));
  return <Page title="Portfolio" description="Select a completed run, then evaluate sensor-count portfolios over concrete joint replication and vehicle samples. Operations are reused."
    actions={<Button component={Link} to="/results?view=portfolio" disabled={!analyses.data?.length}>Next: Results</Button>}>
    <ApiIssues error={workspace.error} onClear={workspace.clearError} />
    {!runs.data?.length && <Alert severity="info">Complete a simulation first. <Link to="/simulation">Open Simulation</Link></Alert>}
    <TextField select label="Source simulation run" value={selected?.run_id ?? ""} onChange={(e) => chooseRun(e.target.value)}><MenuItem value="">Select completed run</MenuItem>{runs.data?.map((run) => <MenuItem key={run.run_id} value={run.run_id}>{run.name} · {run.config.simulation?.time_mode === "relative" ? "Generic day" : run.config.simulation?.service_date}</MenuItem>)}</TextField>
    {selected && editor && <>
      <Alert severity="info">Source: {selected.name} · {selected.replications} simulation replications · {selected.config.simulation?.start_time}–{selected.config.simulation?.end_time} · {selected.config.simulation?.temporal_resolution_minutes} minute bins. Supply remains fixed.</Alert>
      <Paper variant="outlined" sx={{ p: 3 }}><Stack gap={2}>
        <Typography variant="h6">Sensor counts and costs</Typography>
        <TextField label="Cost unit" value={editor.cost_unit} onChange={(e) => update({ cost_unit: e.target.value })} />
        <TextField select label="Budget specification" value={editor.budget_range ? "range" : "levels"} onChange={(e) => update({ budget_range: e.target.value === "range" ? { minimum: 0, maximum: editor.budgets?.at(-1) ?? 100, step: 10 } : null })}><MenuItem value="range">Minimum, maximum and interval</MenuItem><MenuItem value="levels">Explicit budget levels</MenuItem></TextField>
        {editor.budget_range ? <Stack direction="row" gap={1}><RangeFields label="Budget" value={editor.budget_range} onChange={(budget_range) => update({ budget_range })} /></Stack> : <NumberList key={selected.run_id} label={`Budgets (${editor.cost_unit})`} values={editor.budgets ?? []} onChange={(budgets) => update({ budgets })} onValidity={validity("budgets")} />}
        <Typography color="text.secondary">Ranges include both endpoints. Sensor counts select equipped vehicles from the retained physical catalog.</Typography>
        {editor.fleets?.map((fleet, index) => <Stack direction="row" alignItems="center" gap={1} key={fleet.fleet_id} sx={{ flexWrap: { xs: "wrap", lg: "nowrap" } }}>
          <Typography sx={{ minWidth: 150 }}>{selected.config.fleets?.find((value) => value.fleet_id === fleet.fleet_id)?.name ?? fleet.fleet_id}<br /><Typography component="span" color="text.secondary" variant="caption">{selected.vehicle_counts[fleet.fleet_id]} physical vehicles</Typography></Typography>
          <TextField size="small" label="Unit cost" type="number" value={fleet.unit_cost} onChange={(e) => update({ fleets: editor.fleets?.map((value, i) => i === index ? { ...value, unit_cost: Number(e.target.value) } : value) })} />
          {fleet.count_range ? <RangeFields label="Sensors" value={fleet.count_range} onChange={(count_range) => update({ fleets: editor.fleets?.map((value, i) => i === index ? { ...value, count_range } : value) })} /> : <><NumberList label="Saved sensor levels" values={fleet.counts ?? []} onChange={(counts) => update({ fleets: editor.fleets?.map((value, i) => i === index ? { ...value, counts } : value) })} onValidity={validity(fleet.fleet_id)} /><Button onClick={() => update({ fleets: editor.fleets?.map((value, i) => i === index ? { ...value, count_range: { minimum: 0, maximum: selected.vehicle_counts[fleet.fleet_id], step: 1 } } : value) })}>Use range</Button></>}
        </Stack>)}
      </Stack></Paper>
      <Paper variant="outlined" sx={{ p: 3 }}><Stack gap={2}>
        <Typography variant="h6">Utility and risk</Typography>
        <TextField select label="Risk measure" value={editor.risk_metric ?? "std"} onChange={(e) => update({ risk_metric: e.target.value as PortfolioEditor["risk_metric"] })} helperText="P05 compares the lower 5% quantile of sample utility; higher is better."><MenuItem value="p05">Worst-case utility (5th percentile)</MenuItem><MenuItem value="std">Standard deviation</MenuItem></TextField>
        <div className="form-grid"><TextField select label="Function" value={editor.utility} onChange={(e) => update({ utility: e.target.value as PortfolioEditor["utility"] })}><MenuItem value="exponential">Exponential saturation</MenuItem><MenuItem value="linear_capped">Capped linear</MenuItem><MenuItem value="binary">Any positive exposure</MenuItem></TextField>
          {editor.utility !== "binary" && <TextField label="Saturation duration (minutes)" type="number" value={editor.saturation_minutes} onChange={(e) => update({ saturation_minutes: Number(e.target.value) })} />}
          <TextField label="Utility temporal interval (minutes)" type="number" value={editor.utility_temporal_resolution_minutes ?? selected.config.simulation?.temporal_resolution_minutes ?? 1440} onChange={(e) => update({ utility_temporal_resolution_minutes: Number(e.target.value) })} helperText="Exposure is summed within this interval before applying utility. Use 1440 minutes for one whole-day utility interval." />
          <TextField select label="Spatial weights" value={editor.spatial_weight} onChange={(e) => update({ spatial_weight: e.target.value })}>{selected.config.prepared_environment?.feature_names.map((feature) => <MenuItem key={feature} value={feature}>{feature}</MenuItem>)}</TextField>
        </div>
        {curve.data && <ScientificChart traces={[{ x: [...curve.data.exposure_minutes], y: [...curve.data.utility], name: "Utility", mode: "lines" }]} xTitle="Exposure (minutes)" yTitle="Pointwise utility" fallback={<Typography>{curve.data.interpretation}</Typography>} />}
        <Typography color="text.secondary">{curve.data?.interpretation}. Fleet sampling runs quantify empirical allocation and operational variability; they do not add new simulation replications.</Typography>
      </Stack></Paper>
      <Paper variant="outlined" sx={{ p: 3 }}><Typography variant="h6" sx={{ mb: 2 }}>Sampling</Typography><div className="form-grid"><TextField label="Fleet sampling runs" type="number" value={editor.sampling_runs} onChange={(e) => update({ sampling_runs: Number(e.target.value) })} /><TextField label="Sampling seed" type="number" value={editor.seed} onChange={(e) => update({ seed: Number(e.target.value) })} /></div></Paper>
      <TextField label="Analysis name" value={name} onChange={(e) => setName(e.target.value)} />
      <Button variant="contained" disabled={Boolean(workspace.draft.authoring?.read_only) || running || !name.trim() || Object.values(invalidLists).some(Boolean)} onClick={() => void workspace.submitJob("/api/v1/workbench/analyses", "studio_analysis", { name, config: editor, ...(workspace.draft.runOptions ? { options: workspace.draft.runOptions } : {}) }).catch(() => undefined)}>Run portfolio analysis</Button>
    </>}
    {relevant.filter((job) => job.status !== "completed").slice(0, 5).map((job) => <JobMonitor key={job.job_id} jobId={job.job_id} sourceRevisionId={null} />)}
    {analyses.data?.map((analysis) => <Paper variant="outlined" sx={{ p: 2 }} key={analysis.analysis_id}><Typography fontWeight={700}>{analysis.name}</Typography><Typography>{analysis.count_portfolios} count portfolios · {analysis.replications} simulation replications · {analysis.sampling_runs} fleet sampling runs</Typography><Button disabled={false} component={Link} to={`/results?view=portfolio&resource=${analysis.frontier.artifact_id}`}>Open frontier</Button></Paper>)}
  </Page>;
}
