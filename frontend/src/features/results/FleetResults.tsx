import { Accordion, AccordionDetails, AccordionSummary, Alert, Box, Chip, MenuItem, Paper, Slider, Stack, Table, TableBody, TableCell, TableHead, TableRow, TextField, Typography } from "@mui/material";
import { useQuery } from "@tanstack/react-query";
import { useCallback, useMemo, useEffect } from "react";
import { useSearchParams } from "react-router-dom";
import type { JobSnapshot } from "../../api/client";
import { requestJson } from "../../api/client";
import type { RunView, MeanFleetView } from "../../api/generated";
import { ScientificMap } from "../../components/ScientificMap";
import { ScientificChart } from "../../components/ScientificChart";
import { matrixQuery, queryString, resultTable } from "./api";
import type { ArtifactManifestValue, GeoJsonValue } from "./types";
import { artifactFromJob, dependency } from "./types";
import { ExportActions } from "./ExportActions";

interface Bin { time_bin_id: string; start_s: number; end_s: number }
interface Vehicle { fleet_id: string; vehicle_id: string }
const number = (value: number | null | undefined, digits = 2) => value == null ? "—" : value.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
const hour = (seconds: number, origin: number) => { const minutes = Math.round((seconds-origin)/60); return `${Math.floor(minutes/60).toString().padStart(2,"0")}:${(minutes%60).toString().padStart(2,"0")}`; };

export function FleetResults({ runs, jobs, revisionForJob, preferredRun }: { runs: RunView[]; jobs: JobSnapshot[]; revisionForJob: (id: string) => string | null; preferredRun?: string }) {
  const [params, setParams] = useSearchParams();
  const set = useCallback((values: Record<string, string>) => setParams(previous => { const next = new URLSearchParams(previous); Object.entries(values).forEach(([key,value]) => value ? next.set(key,value) : next.delete(key)); return next; }, { replace: true }), [setParams]);
  const sources = useMemo(() => {
    const named = runs.map(run => ({ id: run.run_id, exposure: run.exposure.artifact_id, simulation: run.simulation.artifact_id, name: run.name, run, revision: run.source_revision_id }));
    return [...named, ...jobs.filter(job => artifactFromJob(job)?.artifact_kind === "exposure" && !named.some(row => row.exposure === artifactFromJob(job)?.artifact_id)).map(job => ({ id: job.job_id, exposure: artifactFromJob(job)!.artifact_id, simulation: "", name: String(job.result?.name ?? "Saved sensing run"), run: undefined, revision: revisionForJob(job.job_id) }))];
  }, [runs, jobs, revisionForJob]);
  const source = sources.find(s => s.id === params.get("fleet_run")) ?? sources.find(s => [s.exposure,s.simulation].includes(params.get("resource") ?? "")) ?? sources.find(s => s.id === preferredRun) ?? sources[0];
  useEffect(() => { if (source && params.get("fleet_run") !== source.id) set({fleet_run:source.id}); }, [source,params,set]);
  const resource = source?.exposure ?? "";
  const fleet = params.get("fleet") ?? "";
  const vehicle = params.get("vehicle") ?? "";
  const layer = params.get("layer") === "activity" ? "activity" : "sensing";
  const metric = params.get("metric") ?? "sensing";
  const manifest = useQuery({ queryKey: ["exposure-manifest",resource], enabled: !!resource, queryFn: () => requestJson<ArtifactManifestValue>(`/api/v1/exposures/${resource}`) });
  const simulation = source?.simulation || dependency(manifest.data,"simulation")?.artifact_id || "";
  const environment = dependency(manifest.data,"environment")?.artifact_id;
  const catalog = useQuery({ queryKey: ["fleet-catalog",resource], enabled: !!resource, queryFn: () => resultTable<Vehicle>(resource,"vehicle_catalog") });
  const binsQuery = useQuery({ queryKey: ["time-bins",resource], enabled: !!resource, queryFn: () => resultTable<Bin>(resource,"time_bins") });
  const bins = useMemo(() => [...(binsQuery.data?.items ?? [])].sort((a,b) => a.start_s-b.start_s), [binsQuery.data]);
  const origin = bins[0]?.start_s ?? 0;
  const last = bins.at(-1)?.end_s ?? 86400;
  const start = bins.some(b => String(b.start_s) === params.get("from")) ? Number(params.get("from")) : origin;
  const end = bins.some(b => String(b.end_s) === params.get("to")) && Number(params.get("to")) > start ? Number(params.get("to")) : last;
  const keys = useMemo(() => (catalog.data?.items ?? []).filter(v => (!fleet || v.fleet_id === fleet) && (!vehicle || v.vehicle_id === vehicle)).map(v => ({fleet_id:v.fleet_id,vehicle_id:v.vehicle_id})), [catalog.data, fleet, vehicle]);
  const body = useMemo(() => ({ resource_id: resource, kind: vehicle ? "vehicle_exposure" : "operational_aggregate", vehicle_keys: keys, statistic: "mean", temporal_aggregation: "sum" }), [resource, vehicle, keys]);
  const selectedBins = useMemo(() => bins.filter(b => b.start_s >= start && b.end_s <= end).map(b => b.time_bin_id), [bins, start,end]);
  const ready = !!resource && !!keys.length && !!bins.length && catalog.data?.is_complete && binsQuery.data?.is_complete;
  const summary = useQuery({ queryKey: ["fleet-sensing",body], enabled: !!ready, queryFn: () => matrixQuery(body) });
  const windowBody = selectedBins.length === bins.length ? body : {...body,time_bin_ids:selectedBins};
  const window = useQuery({ queryKey: ["fleet-sensing",windowBody], enabled: !!ready, queryFn: () => matrixQuery(windowBody) });
  const operations = useQuery({ queryKey: ["mean-fleet",resource,fleet,vehicle,start,end], enabled: !!ready && !!simulation, queryFn: () => requestJson<MeanFleetView>(`/api/v1/fleet-summaries/${resource}?${queryString({fleet_id:fleet,vehicle_id:vehicle,time_start_s:start,time_end_s:end})}`) });
  const grid = useQuery({ queryKey: ["grid-map",environment], enabled: !!environment, queryFn: () => requestJson<GeoJsonValue>(`/api/v1/maps/${environment}/grid`) });
  const boundary = useQuery({ queryKey: ["boundary-map",environment], enabled: !!environment, queryFn: () => requestJson<GeoJsonValue>(`/api/v1/maps/${environment}/boundary`) });
  const activity = useQuery({ queryKey: ["mean-movement",simulation,fleet,vehicle,start,end], enabled: !!simulation && layer === "activity", queryFn: () => requestJson<GeoJsonValue>(`/api/v1/maps/${simulation}/movements?${queryString({aggregation:"mean_edge_usage",fleet_id:fleet,vehicle_id:vehicle,time_start_s:start,time_end_s:end})}`) });
  const sensingMap = useMemo(() => { if (!grid.data || !window.data) return null; const values = new Map(window.data.values.map(row => [row.cell_id,row.value])); const domain = new Set(window.data.cell_ids); return { ...grid.data,features:grid.data.features.filter(f => domain.has(String(f.properties.cell_id))).map(f => ({...f,properties:{...f.properties,value:values.get(String(f.properties.cell_id)) ?? 0}})) }; }, [grid.data,window.data]);
  const context = useMemo(() => boundary.data ? [boundary.data] : undefined,[boundary.data]);
  const names = new Map(source?.run?.config.fleets?.map(f => [f.fleet_id,f.name]) ?? []);
  const label = (id: string) => names.get(id) ?? id;
  const fleetIds = [...new Set(catalog.data?.items.map(v => v.fleet_id) ?? [])];
  const selectBin = useCallback((value: unknown) => { const bin = bins.find(b => b.time_bin_id === value); if (bin) set({from:String(bin.start_s),to:String(bin.end_s)}); }, [bins,set]);
  const traces = useMemo(() => [{ type: "bar" as const, name: "Mean", x: bins.map(b => (b.start_s-origin)/3600),
    y: bins.map(b => metric === "sensing" ? (summary.data?.time_summary.find(row => row.time_bin_id === b.time_bin_id)?.value ?? 0)/3600 : operations.data?.time_summary.find(row => row.time_bin_id === b.time_bin_id)?.[metric === "arrivals" ? "mean_arrivals" : "mean_active_vehicles"] ?? 0),
    customdata: bins.map(b => b.time_bin_id), marker:{color:bins.map(b => b.start_s>=start && b.end_s<=end ? "#087f8c" : "#c8dce2")} }], [bins,origin,metric,summary.data,operations.data,start,end]);
  if (!source) return <Alert severity="info">Run a simulation to view average fleet performance.</Alert>;
  return <Stack gap={2}>
    <Paper variant="outlined" className="analysis-panel"><div className="form-grid">
      <TextField disabled={false} select label="Run" value={source.id} onChange={e => set({fleet_run:e.target.value,resource:"",fleet:"",vehicle:"",from:"",to:""})}>{sources.map(s => <MenuItem key={s.id} value={s.id}>{s.name}</MenuItem>)}</TextField>
      <TextField disabled={false} select slotProps={{ select:{displayEmpty:true},inputLabel:{shrink:true} }} label="Fleet" value={fleet} onChange={e => set({fleet:e.target.value,vehicle:""})}><MenuItem value="">All fleets</MenuItem>{fleetIds.map(id => <MenuItem key={id} value={id}>{label(id)}</MenuItem>)}</TextField>
      <TextField disabled={false} select label="Map layer" value={layer} onChange={e => set({layer:e.target.value})}><MenuItem value="sensing">Mean sensing duration</MenuItem><MenuItem value="activity">Mean road activity</MenuItem></TextField>
    </div><Stack direction="row" alignItems="center" gap={2} sx={{mt:2}}><Chip variant="outlined" label={`Mean of ${window.data?.replications_R ?? source.run?.replications ?? "…"} runs`} /><Typography variant="body2" color="text.secondary">{hour(start,origin)}–{hour(end,origin)} · all physical vehicles{vehicle ? ` · ${vehicle}` : ""}</Typography></Stack></Paper>
    {[manifest.error,catalog.error,binsQuery.error,summary.error,window.error,operations.error,grid.error,boundary.error,activity.error].filter(Boolean).map((e,i) => <Alert severity="error" key={i}>{String(e)}</Alert>)}
    {(catalog.data && !catalog.data.is_complete || binsQuery.data && !binsQuery.data.is_complete) && <Alert severity="error">This result exceeds the selector limit. Export the complete data to inspect it.</Alert>}
    <Box className="form-grid"><Paper variant="outlined" className="analysis-panel"><Typography color="text.secondary">Mean cumulative sensing</Typography><Typography variant="h4">{number(window.data?.overall_value == null ? null : window.data.overall_value/3600)} <Typography component="span">vehicle-hours</Typography></Typography></Paper><Paper variant="outlined" className="analysis-panel"><Typography color="text.secondary">Mean spatial grid coverage</Typography><Typography variant="h4">{number(window.data?.mean_coverage_fraction == null ? null : window.data.mean_coverage_fraction*100)}%</Typography><Typography variant="caption" color="text.secondary">{window.data?.coverage_semantics === "mean_within_observation_any_time_spatial_coverage_road_intersecting_cells" ? `Mean fraction of ${window.data.coverage_denominator_cell_count?.toLocaleString() ?? "all"} road-intersecting grids visited at least once within the selected time range. Only positive-length road intersections enter the denominator; visit timing and repetition do not change coverage.` : `Mean fraction of ${window.data?.coverage_denominator_cell_count?.toLocaleString() ?? "all"} prepared grids visited at least once within the selected time range. This historical result predates the road-intersection coverage domain.`}</Typography></Paper></Box>
    <Paper variant="outlined" className="analysis-panel fleet-main-map"><Typography variant="h6" sx={{mb:2}}>{layer === "sensing" ? "Mean sensing duration" : "Mean road activity"}</Typography><ScientificMap data={layer === "sensing" ? sensingMap : activity.data ?? null} mode={layer === "sensing" ? "duration" : "movement"} unit="s" contextData={context} fitKey={resource} loading={layer === "sensing" ? window.isFetching || grid.isFetching : activity.isFetching} fallback={<Typography>Use the export below to inspect complete scientific tables.</Typography>} />{layer === "activity" && <Typography variant="caption" color="text.secondary">Mean time spent on each directed road edge across all runs; displayed geometry is the union of recorded edge portions.</Typography>}</Paper>
    <Paper variant="outlined" className="analysis-panel"><Stack direction="row" justifyContent="space-between" alignItems="center" gap={2}><Typography variant="h6">Daily profile</Typography><TextField disabled={false} select label="Time metric" value={metric} sx={{minWidth:230}} onChange={e => set({metric:e.target.value})}><MenuItem value="sensing">Sensing duration</MenuItem><MenuItem value="arrivals">Task arrivals</MenuItem><MenuItem value="active">Active vehicles</MenuItem></TextField></Stack>
      <Typography variant="body2" color="text.secondary" sx={{mt:1}}>Full observation day. Click a bar or adjust the interval to update the map and summaries.</Typography>
      <ScientificChart traces={bins.length ? traces : []} xTitle="Hour from observation start" yTitle={metric === "sensing" ? "Mean sensing (vehicle-hours)" : metric === "arrivals" ? "Mean arrivals (tasks)" : "Mean active vehicles"} onPoint={selectBin} height={260} fallback={<Typography>Time summaries are available in the exported files.</Typography>} />
      <Slider disabled={false} aria-label="Result time interval" min={0} max={Math.max(1,bins.length)} step={1} disableSwap value={[Math.max(0,bins.findIndex(b => b.start_s === start)),Math.max(1,bins.findIndex(b => b.end_s === end)+1)]} onChange={(_,v) => {const [a,b] = v as number[]; if(a<b && bins[a] && bins[b-1]) set({from:String(bins[a].start_s),to:String(bins[b-1].end_s)});}} valueLabelDisplay="auto" valueLabelFormat={v => hour(v === bins.length ? last : bins[v]?.start_s ?? origin,origin)} />
      <Typography variant="caption">Selected interval: {hour(start,origin)}–{hour(end,origin)}</Typography>
    </Paper>
    <Paper variant="outlined" className="analysis-panel"><Typography variant="h6">Fleet performance</Typography><Table size="small"><TableHead><TableRow><TableCell>Fleet</TableCell><TableCell align="right">Mean arrivals</TableCell><TableCell align="right">Mean completed</TableCell><TableCell align="right">Completion rate</TableCell><TableCell align="right">Mean active vehicles</TableCell></TableRow></TableHead><TableBody>{operations.data?.fleets.map(row => <TableRow key={row.fleet_id}><TableCell>{label(row.fleet_id)}</TableCell><TableCell align="right">{number(row.mean_released_tasks)}</TableCell><TableCell align="right">{number(row.mean_completed_tasks)}</TableCell><TableCell align="right" title={`Based on ${row.completion_rate_replications} runs with arrivals`}>{number(row.mean_completion_rate == null ? null : row.mean_completion_rate*100)}%</TableCell><TableCell align="right">{number(row.mean_active_vehicles)}</TableCell></TableRow>)}</TableBody></Table><Typography variant="caption" color="text.secondary">Final outcomes of tasks released in the selected interval. Empty arrival cohorts are excluded from completion-rate averages. Active vehicles reflect availability windows; sensing additionally excludes depot stays and inferred power-off gaps.</Typography></Paper>
    <Accordion><AccordionSummary>Vehicle inspection and source details</AccordionSummary><AccordionDetails><Stack gap={2}><TextField disabled={false} select slotProps={{ select:{displayEmpty:true},inputLabel:{shrink:true} }} label="Physical vehicle" value={vehicle ? `${fleet}:${vehicle}` : ""} onChange={e => {const selected = (catalog.data?.items ?? []).find(v => `${v.fleet_id}:${v.vehicle_id}` === e.target.value); set({vehicle:selected?.vehicle_id ?? "",fleet:selected?.fleet_id ?? fleet});}}><MenuItem value="">All vehicles</MenuItem>{(catalog.data?.items ?? []).filter(v => !fleet || v.fleet_id===fleet).map(v => <MenuItem key={`${v.fleet_id}:${v.vehicle_id}`} value={`${v.fleet_id}:${v.vehicle_id}`}>{label(v.fleet_id)} · {v.vehicle_id}</MenuItem>)}</TextField><Typography>Vehicle statistics use the same complete run set, including inactive runs. Select a fleet before inspecting vehicles with repeated names.</Typography><Typography variant="body2">{source.run?.assumptions.join(". ")}</Typography>{operations.data?.fleets.map(row => <Typography variant="body2" key={row.fleet_id}>{label(row.fleet_id)}: mean carry-in {number(row.mean_carry_in_tasks)} · {Object.entries(row.mean_status_counts).map(([name,value]) => `${name}: ${number(value)}`).join(" · ")}</Typography>)}<Typography variant="caption">Source revision: {source.revision ?? "Recorded artifact source"}</Typography></Stack></AccordionDetails></Accordion>
    <Accordion><AccordionSummary>Export complete records</AccordionSummary><AccordionDetails><Stack gap={2}><Typography>Sparse vehicle exposure · all retained runs</Typography><ExportActions resourceId={resource} table="exposure" sourceRevisionId={source.revision} />{simulation && <><Typography>Task outcomes</Typography><ExportActions resourceId={simulation} table="task_outcomes" sourceRevisionId={source.revision} /><Typography>Recorded movement intervals</Typography><ExportActions resourceId={simulation} table="movements" sourceRevisionId={source.revision} /></>}</Stack></AccordionDetails></Accordion>
  </Stack>;
}
