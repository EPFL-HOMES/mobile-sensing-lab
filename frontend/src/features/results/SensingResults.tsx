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
  Typography,
  Button,
} from "@mui/material";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo } from "react";
import { Link, useSearchParams } from "react-router-dom";
import type { JobSnapshot } from "../../api/client";
import { requestJson } from "../../api/client";
import { ScientificChart } from "../../components/ScientificChart";
import { ScientificMap } from "../../components/ScientificMap";
import { ExportActions } from "./ExportActions";
import { matrixQuery, resultTable } from "./api";
import { ResultSourceSelect } from "./ResultSourceSelect";
import type { ArtifactManifestValue, GeoJsonValue, MatrixSliceValue } from "./types";
import { artifactFromJob, dependency } from "./types";

interface ReplicationRow { replication_id: string; complete: boolean; }
interface VehicleRow { fleet_id: string; vehicle_id: string; }
interface TimeBinRow { time_bin_id: string; canonical_index: number; start_s: number; end_s: number; }

function matrixBody(resourceId: string, mode: string, replication: string, vehicleKeys: VehicleRow[], timeBinIds: string[]) {
  return {
    resource_id: resourceId,
    kind: vehicleKeys.length === 1 ? "vehicle_exposure" : "operational_aggregate",
    replication_ids: mode === "realization" ? [replication] : [],
    vehicle_keys: vehicleKeys.map(({ fleet_id, vehicle_id }) => ({ fleet_id, vehicle_id })),
    time_bin_ids: timeBinIds,
    statistic: mode, temporal_aggregation: "sum",
  };
}

function attachValues(grid: GeoJsonValue | undefined, matrix: MatrixSliceValue | undefined): GeoJsonValue | null {
  if (!grid || !matrix) return null;
  const values = new Map(matrix.values.map((row) => [row.cell_id, row.value]));
  return { ...grid, features: grid.features.map((feature) => ({ ...feature, properties: { ...feature.properties, value: values.get(String(feature.properties.cell_id)) ?? 0 } })) };
}

export function SensingResults({ jobs, revisionForJob }: { jobs: JobSnapshot[]; revisionForJob: (jobId: string) => string | null }) {
  const [params, setParams] = useSearchParams();
  const sources = jobs.filter((job) => artifactFromJob(job)?.artifact_kind === "exposure");
  const fallbackId = sources.map(artifactFromJob).find(Boolean)?.artifact_id ?? "";
  const resourceId = sources.some((job) => artifactFromJob(job)?.artifact_id === params.get("resource")) ? params.get("resource")! : fallbackId;
  const set = (key: string, value: string) => { const next = new URLSearchParams(params); value ? next.set(key, value) : next.delete(key); setParams(next, { replace: true }); };
  const manifest = useQuery({ queryKey: ["exposure-manifest", resourceId], enabled: Boolean(resourceId), queryFn: () => requestJson<ArtifactManifestValue>(`/api/v1/exposures/${resourceId}`) });
  const replications = useQuery({ queryKey: ["exposure-axis", resourceId, "replications"], enabled: Boolean(resourceId), queryFn: () => resultTable<ReplicationRow>(resourceId, "replication_status") });
  const catalog = useQuery({ queryKey: ["exposure-axis", resourceId, "vehicles"], enabled: Boolean(resourceId), queryFn: () => resultTable<VehicleRow>(resourceId, "vehicle_catalog") });
  const bins = useQuery({ queryKey: ["exposure-axis", resourceId, "bins"], enabled: Boolean(resourceId), queryFn: () => resultTable<TimeBinRow>(resourceId, "time_bins") });
  const environmentId = dependency(manifest.data, "environment")?.artifact_id ?? "";
  const grid = useQuery({ queryKey: ["sensing-grid", environmentId], enabled: Boolean(environmentId), queryFn: () => requestJson<GeoJsonValue>(`/api/v1/maps/${environmentId}/grid`) });
  const replicationIds = replications.data?.items.map((row) => row.replication_id) ?? [];
  const replication = replicationIds.includes(params.get("replication") ?? "") ? params.get("replication")! : (replicationIds[0] ?? "");
  const mode = ["realization", "mean", "std"].includes(params.get("statistic") ?? "") ? params.get("statistic")! : "realization";
  const scope = ["vehicle", "fleet", "all"].includes(params.get("scope") ?? "") ? params.get("scope")! : "all";
  const fleets = [...new Set(catalog.data?.items.map((row) => row.fleet_id) ?? [])];
  const fleet = fleets.includes(params.get("fleet") ?? "") ? params.get("fleet")! : (fleets[0] ?? "");
  const fleetVehicles = catalog.data?.items.filter((row) => row.fleet_id === fleet) ?? [];
  const vehicle = fleetVehicles.some((row) => row.vehicle_id === params.get("vehicle")) ? params.get("vehicle")! : (fleetVehicles[0]?.vehicle_id ?? "");
  const selectedVehicles = useMemo(() => scope === "vehicle" ? fleetVehicles.filter((row) => row.vehicle_id === vehicle) : scope === "fleet" ? fleetVehicles : (catalog.data?.items ?? []), [catalog.data, fleetVehicles, scope, vehicle]);
  const timeBins = [...(bins.data?.items ?? [])].sort((left, right) => left.canonical_index - right.canonical_index);
  const timeBin = timeBins.some((row) => row.time_bin_id === params.get("time_bin")) ? params.get("time_bin")! : "all";
  const summary = useQuery({ queryKey: ["sensing-summary", resourceId, mode, replication, selectedVehicles], enabled: Boolean(resourceId && selectedVehicles.length && (mode !== "realization" || replication)), queryFn: () => matrixQuery(matrixBody(resourceId, mode, replication, selectedVehicles, [])) });
  const mapMatrix = useQuery({ queryKey: ["sensing-map", resourceId, mode, replication, selectedVehicles, timeBin], enabled: Boolean(resourceId && timeBin && selectedVehicles.length && (mode !== "realization" || replication)), queryFn: () => matrixQuery(matrixBody(resourceId, mode, replication, selectedVehicles, timeBin === "all" ? [] : [timeBin])) });
  const mapped = useMemo(() => attachValues(grid.data, mapMatrix.data), [grid.data, mapMatrix.data]);
  useEffect(() => { if (resourceId && params.get("resource") !== resourceId) set("resource", resourceId); }, [resourceId]);
  if (sources.length === 0) return <Alert severity="info">No completed exposure artifact is available. Run a simulation to compute operations and sensing together.</Alert>;
  const sourceJob = sources.find((job) => artifactFromJob(job)?.artifact_id === resourceId);
  const error = manifest.error ?? replications.error ?? catalog.error ?? bins.error ?? grid.error ?? summary.error ?? mapMatrix.error;
  return <Stack gap={2.5}>
    <Stack direction="row" gap={1.5} flexWrap="wrap">
      <ResultSourceSelect label="Sensing run" jobs={sources} value={resourceId} onChange={(value) => set("resource", value)} />
      <FormControl size="small" sx={{ minWidth: 170 }}><InputLabel>Statistic</InputLabel><Select inputProps={{ "aria-label": "Statistic" }} label="Statistic" value={mode} onChange={(event) => set("statistic", event.target.value)}><MenuItem value="realization">Selected replication</MenuItem><MenuItem value="mean">Mean across replications</MenuItem><MenuItem value="std" disabled={replicationIds.length < 2}>Std across replications</MenuItem></Select></FormControl>
      <FormControl size="small" sx={{ minWidth: 180 }}><InputLabel>Vehicle scope</InputLabel><Select inputProps={{ "aria-label": "Vehicle scope" }} label="Vehicle scope" value={scope} onChange={(event) => set("scope", event.target.value)}><MenuItem value="vehicle">One physical vehicle</MenuItem><MenuItem value="fleet">Complete fleet population</MenuItem><MenuItem value="all">All operational vehicles</MenuItem></Select></FormControl>
      <FormControl size="small" sx={{ minWidth: 150 }}><InputLabel>Fleet</InputLabel><Select disabled={scope === "all"} inputProps={{ "aria-label": "Fleet" }} label="Fleet" value={fleet} onChange={(event) => set("fleet", event.target.value)}>{fleets.map((item) => <MenuItem key={item} value={item}>{item}</MenuItem>)}</Select></FormControl>
      {scope === "vehicle" && <FormControl size="small" sx={{ minWidth: 160 }}><InputLabel>Vehicle</InputLabel><Select inputProps={{ "aria-label": "Vehicle" }} label="Vehicle" value={vehicle} onChange={(event) => set("vehicle", event.target.value)}>{fleetVehicles.map((item) => <MenuItem key={item.vehicle_id} value={item.vehicle_id}>{item.vehicle_id}</MenuItem>)}</Select></FormControl>}
      {mode === "realization" && <FormControl size="small" sx={{ minWidth: 160 }}><InputLabel>Simulation replication</InputLabel><Select inputProps={{ "aria-label": "Simulation replication" }} label="Simulation replication" value={replication} onChange={(event) => set("replication", event.target.value)}>{replicationIds.map((item) => <MenuItem key={item} value={item}>{item}</MenuItem>)}</Select></FormControl>}
      <FormControl size="small" sx={{ minWidth: 180 }}><InputLabel>Reporting bin</InputLabel><Select inputProps={{ "aria-label": "Reporting bin" }} label="Reporting bin" value={timeBin} onChange={(event) => set("time_bin", event.target.value)}><MenuItem value="all">Whole observation window</MenuItem>{timeBins.map((item) => <MenuItem key={item.time_bin_id} value={item.time_bin_id}>{(item.start_s/3600).toFixed(2)}–{(item.end_s/3600).toFixed(2)} elapsed h</MenuItem>)}</Select></FormControl>
    </Stack>
    {error && <Alert severity="error">{String(error)}</Alert>}
    {summary.data && <Stack direction="row" gap={1} flexWrap="wrap"><Chip color="primary" variant="outlined" label={`Simulation replications: ${summary.data.replications_R}`} /><Chip variant="outlined" label="All physical vehicles remain eligible for sensor sampling" /><Chip variant="outlined" label={`${summary.data.positive_cell_count} positive cells`} /><Chip variant="outlined" label={`Total ${mode}: ${summary.data.overall_value?.toPrecision(5) ?? "—"} ${summary.data.unit}`} /></Stack>}
    <Alert severity="info">Vehicle matrices are summed inside each replication before statistics across replications. Aggregate standard deviation therefore preserves joint covariance; sparse absent rows are certified zeros.</Alert>
    <Box className="result-workspace">
      <Paper variant="outlined" sx={{ p: 2 }}><Typography variant="h6" sx={{ mb: 1.5 }}>Sensing duration</Typography><ScientificMap loading={mapMatrix.isFetching || grid.isFetching} data={mapped} mode="duration" unit={mapMatrix.data?.unit} fallback={<MatrixTable matrix={mapMatrix.data} />} /></Paper>
      <Paper variant="outlined" sx={{ p: 2 }}><Typography variant="h6">Time summary</Typography><Typography variant="body2" color="text.secondary">Sensing movement duration depends on arrivals, active supply and dispatch; this is not a demand curve. Backend statistic of the within-observation cell sum; unit {summary.data?.unit ?? "s"}.</Typography><ScientificChart traces={summary.data ? [{ x: summary.data.time_summary.map((row) => (timeBins.find((bin) => bin.time_bin_id === row.time_bin_id)?.start_s ?? 0) / 3600), y: summary.data.time_summary.map((row) => row.value), name: mode, type: "bar" }] : []} xTitle="Hour from observation start" yTitle={`${mode} duration (${summary.data?.unit ?? "s"})`} fallback={<MatrixTable matrix={summary.data} />} /></Paper>
    </Box>
    <Paper variant="outlined" sx={{ p: 2 }}><Stack direction="row" gap={2}><Button component={Link} to="/simulation">Change temporal resolution in Simulation</Button>{sourceJob && <ExportActions resourceId={resourceId} table="exposure" sourceRevisionId={revisionForJob(sourceJob.job_id)} />}</Stack></Paper>
  </Stack>;
}

function MatrixTable({ matrix }: { matrix: MatrixSliceValue | undefined }) {
  return <TableContainer sx={{ maxHeight: 330 }}><Table size="small" stickyHeader><TableHead><TableRow><TableCell>Cell</TableCell><TableCell>Reporting bin</TableCell><TableCell align="right">Value</TableCell><TableCell>Unit</TableCell></TableRow></TableHead><TableBody>{(matrix?.values ?? []).map((row) => <TableRow key={`${row.cell_id}-${row.time_bin_id}`}><TableCell className="mono">{row.cell_id}</TableCell><TableCell className="mono">{row.time_bin_id}</TableCell><TableCell align="right">{row.value}</TableCell><TableCell>{matrix?.unit}</TableCell></TableRow>)}</TableBody></Table></TableContainer>;
}
