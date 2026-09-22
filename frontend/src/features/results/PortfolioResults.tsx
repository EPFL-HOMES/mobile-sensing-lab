import {
  Accordion, AccordionDetails, AccordionSummary, Alert, Box, Checkbox, Chip,
  FormControlLabel, LinearProgress, MenuItem, Paper, Stack, Table, TableBody, TableCell, TableContainer, TableHead,
  TableRow, TextField, Typography,
} from "@mui/material";
import { useQueries, useQuery } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo } from "react";
import { Link, useSearchParams } from "react-router-dom";
import type { JobSnapshot } from "../../api/client";
import type { PortfolioBudgetSeriesView, PortfolioBudgetView, PortfolioFrontierPoint, PortfolioFrontierView } from "../../api/generated";
import { requestJson } from "../../api/client";
import { ScientificChart } from "../../components/ScientificChart";
import { ScientificMap } from "../../components/ScientificMap";
import { ExportActions } from "./ExportActions";
import { portfolioBudgetTrace, portfolioColors as colors, portfolioTraces } from "./portfolioPresentation";
import { matrixQuery, resultTable } from "./api";
import type { ArtifactManifestValue, GeoJsonValue, MatrixSliceValue } from "./types";
import { artifactFromJob, dependency } from "./types";

interface TimeBinRow { time_bin_id: string; canonical_index: number; start_s: number; end_s: number; }
const formatUtility = (value: number | null) => value == null ? "—" : value.toFixed(5);
const fleetLabel = (value: string) => value.replaceAll("_", " ").replace(/^./, char => char.toUpperCase());
const countLabel = (point: PortfolioFrontierPoint) => Object.entries(point.count_by_fleet).map(([fleet, count]) => `${fleetLabel(fleet)} ${count}`).join(" · ");


function attachValues(grid: GeoJsonValue | undefined, matrix: MatrixSliceValue | undefined): GeoJsonValue | null {
  if (!grid || !matrix) return null;
  const values = new Map(matrix.values.map(row => [row.cell_id, row.value]));
  return { ...grid, features: grid.features.map(feature => ({ ...feature,
    properties: { ...feature.properties, value: values.get(String(feature.properties.cell_id)) ?? 0 },
  })) };
}

function PointTable({ points, onPoint, unit, p05Risk }: { points: PortfolioFrontierPoint[]; onPoint: (id: string) => void; unit: string; p05Risk: boolean }) {
  return <TableContainer sx={{ maxHeight: 360 }}><Table size="small" stickyHeader>
    <TableHead><TableRow><TableCell>Equipped vehicles</TableCell><TableCell align="right">Mean utility</TableCell><TableCell align="right">{p05Risk ? "P05 utility" : "Std utility"}</TableCell><TableCell align="right">Cost ({unit})</TableCell></TableRow></TableHead>
    <TableBody>{points.map(point => <TableRow hover tabIndex={0} role="button" aria-label={`Inspect ${countLabel(point)}`} key={point.portfolio_id} onClick={() => onPoint(point.portfolio_id)} onKeyDown={event => { if (event.key === "Enter") onPoint(point.portfolio_id); }} sx={{ cursor: "pointer" }}>
      <TableCell>{countLabel(point)}</TableCell><TableCell align="right">{formatUtility(point.utility_mean)}</TableCell><TableCell align="right">{formatUtility(p05Risk ? point.utility_p05 : point.utility_sample_std)}</TableCell><TableCell align="right">{point.total_cost.toFixed(5)}</TableCell>
    </TableRow>)}</TableBody></Table></TableContainer>;
}

function MatrixTable({ matrix }: { matrix: MatrixSliceValue | undefined }) {
  return <TableContainer sx={{ maxHeight: 330 }}><Table size="small" stickyHeader><TableHead><TableRow><TableCell>Cell</TableCell><TableCell align="right">Mean duration ({matrix?.unit ?? "s"})</TableCell></TableRow></TableHead><TableBody>{(matrix?.values ?? []).map(row => <TableRow key={`${row.cell_id}-${row.time_bin_id}`}><TableCell className="mono">{row.cell_id}</TableCell><TableCell align="right">{row.value.toFixed(5)}</TableCell></TableRow>)}</TableBody></Table></TableContainer>;
}

function PortfolioInspector({ point, unit, p05Risk, saturatedPercentage, meanCoverageFraction }: { point: PortfolioFrontierPoint; unit: string; p05Risk: boolean; saturatedPercentage: number | null; meanCoverageFraction: number | null }) {
  return <Stack gap={2.5}>
    <Box className="portfolio-metrics">
      <Box><Typography variant="caption" color="text.secondary">Mean utility</Typography><Typography className="metric-value">{formatUtility(point.utility_mean)}</Typography></Box>
      <Box><Typography variant="caption" color="text.secondary">{p05Risk ? "P05 utility" : "Std utility"}</Typography><Typography className="metric-value">{formatUtility(p05Risk ? point.utility_p05 : point.utility_sample_std)}</Typography></Box>
      <Box><Typography variant="caption" color="text.secondary">Mean spatial grid coverage</Typography><Typography className="metric-value">{meanCoverageFraction == null ? "—" : `${(100 * meanCoverageFraction).toFixed(2)}%`}</Typography></Box>
      <Box><Typography variant="caption" color="text.secondary">Saturated space–time units</Typography><Typography className="metric-value">{saturatedPercentage == null ? "—" : `${saturatedPercentage.toFixed(2)}%`}</Typography></Box>
    </Box>
    <Typography variant="caption" color="text.secondary">Mean fraction of grids visited at least once per sampling round within the selected reporting window. Includes zero-coverage grids in the saved exposure domain; the saturation map filter does not affect this metric.</Typography>
    <Box><Typography variant="overline" color="text.secondary">Utility quantiles</Typography><Box className="quantile-row">{[["P05", point.utility_p05], ["Median", point.utility_p50], ["P95", point.utility_p95]].map(([label, value]) => <Box key={String(label)}><Typography variant="caption" color="text.secondary">{label}</Typography><Typography className="numeric">{formatUtility(Number(value))}</Typography></Box>)}</Box></Box>
    <Box><Stack direction="row" justifyContent="space-between" alignItems="baseline" gap={1}><Typography variant="overline" color="text.secondary">Cost by fleet</Typography><Typography variant="caption" color="text.secondary">{unit}</Typography></Stack>
      <Stack gap={1.75} sx={{ mt: 1 }}>{Object.entries(point.cost_by_fleet).map(([fleet, cost], index) => <Box key={fleet}>
        <Stack direction="row" justifyContent="space-between" gap={1}><Typography variant="body2">{fleetLabel(fleet)} <Typography component="span" variant="caption" color="text.secondary">· {point.count_by_fleet[fleet]} sensors</Typography></Typography><Typography variant="body2" className="numeric">{cost.toFixed(5)}</Typography></Stack>
        <Box className="cost-track" aria-label={`${fleetLabel(fleet)} cost ${cost.toFixed(5)} ${unit}`}><Box sx={{ height: "100%", width: `${point.total_cost > 0 ? cost / point.total_cost * 100 : 0}%`, backgroundColor: colors[index % colors.length], borderRadius: 1 }} /></Box>
      </Box>)}</Stack>
      <Stack direction="row" justifyContent="space-between" sx={{ pt: 1.5, mt: 1.5, borderTop: "1px solid", borderColor: "divider" }}><Typography variant="body2" fontWeight={700}>Total cost</Typography><Typography variant="body2" fontWeight={700} className="numeric">{point.total_cost.toFixed(5)}</Typography></Stack>
    </Box>
    <Typography variant="caption" color="text.secondary">Display rounded to five decimals. Analysis and exports retain full precision. {p05Risk ? "P05 is an empirical quantile, not a guaranteed lower bound." : "Std describes empirical variability, not a confidence interval."}</Typography>
  </Stack>;
}

export function PortfolioResults({ jobs, revisionForJob, preferredView }: { jobs: JobSnapshot[]; revisionForJob: (jobId: string) => string | null; preferredView?: string }) {
  const [params, setParams] = useSearchParams();
  const sources = jobs.filter(job => artifactFromJob(job)?.artifact_kind === "portfolio" && job.result?.portfolio_stage === "analysis");
  const available = sources.map(artifactFromJob).filter(Boolean);
  const preferred = new URLSearchParams(preferredView?.split("?")[1]);
  const resourceId = available.find(value => value?.artifact_id === (params.get("analysis_resource") ?? params.get("resource")))?.artifact_id ?? available.find(value => value?.artifact_id === preferred.get("resource"))?.artifact_id ?? available[0]?.artifact_id ?? "";
  const sourceJob = sources.find(job => artifactFromJob(job)?.artifact_id === resourceId);
  const set = useCallback((key: string, value: string) => setParams(previous => { const next = new URLSearchParams(previous); value ? next.set(key, value) : next.delete(key); return next; }, { replace: true }), [setParams]);
  const selectPoint = useCallback((value: unknown) => { if (value != null) set("portfolio", String(value)); }, [set]);
  const analysis = useQuery({ queryKey: ["portfolio-analysis", resourceId], enabled: Boolean(resourceId), queryFn: () => requestJson<ArtifactManifestValue>(`/api/v1/portfolio-analyses/${resourceId}`) });
  const resolvedPortfolio = (analysis.data?.scientific_identity.resolved_config as { portfolio?: { costs?: { by_fleet_minor?: Record<string, number> }; utility?: { saturation_s?: number } } } | undefined)?.portfolio;
  const configuredFleetIds = useMemo(() => Object.keys(resolvedPortfolio?.costs?.by_fleet_minor ?? {}).sort(), [resolvedPortfolio]);
  const requestedFleetIds = useMemo(() => [...new Set((params.get("fleet_scope") ?? "").split(",").filter(Boolean))].sort(), [params]);
  const validRequestedFleetIds = configuredFleetIds.length ? requestedFleetIds.filter(fleet => configuredFleetIds.includes(fleet)) : requestedFleetIds;
  const fleetQuery = validRequestedFleetIds.map(fleet => `fleet_id=${encodeURIComponent(fleet)}`).join("&");
  const budgets = useQuery({ queryKey: ["portfolio-budgets", resourceId], enabled: Boolean(resourceId), queryFn: () => resultTable<PortfolioBudgetView>(resourceId, "budget_levels") });
  const sampleId = dependency(analysis.data, "portfolio_samples")?.artifact_id ?? "";
  const samplesManifest = useQuery({ queryKey: ["portfolio-samples", sampleId], enabled: Boolean(sampleId), queryFn: () => requestJson<ArtifactManifestValue>(`/api/v1/portfolio-samples/${sampleId}`) });
  const exposureId = dependency(samplesManifest.data, "exposure")?.artifact_id ?? "";
  const exposure = useQuery({ queryKey: ["portfolio-exposure", exposureId], enabled: Boolean(exposureId), queryFn: () => requestJson<ArtifactManifestValue>(`/api/v1/exposures/${exposureId}`) });
  const environmentId = dependency(exposure.data, "environment")?.artifact_id ?? "";
  const grid = useQuery({ queryKey: ["portfolio-grid", environmentId], enabled: Boolean(environmentId), queryFn: () => requestJson<GeoJsonValue>(`/api/v1/maps/${environmentId}/grid`) });
  const bins = useQuery({ queryKey: ["portfolio-bins", exposureId], enabled: Boolean(exposureId), queryFn: () => resultTable<TimeBinRow>(exposureId, "time_bins") });
  const budgetRows = useMemo(() => [...(budgets.data?.items ?? [])].sort((a, b) => a.budget_minor - b.budget_minor), [budgets.data]);
  const frontiers = useQueries({ queries: budgetRows.map(budget => ({ queryKey: ["portfolio-frontier", resourceId, budget.budget_id, fleetQuery], queryFn: () => requestJson<PortfolioFrontierView>(`/api/v1/portfolio-frontiers/${resourceId}?budget_id=${encodeURIComponent(budget.budget_id)}${fleetQuery ? `&${fleetQuery}` : ""}`), enabled: Boolean(resourceId) })) });
  const zeroBudgetIds = new Set(frontiers.flatMap(query => query.data?.budget.budget_minor === 0 ? query.data.points.map(point => point.portfolio_id) : []));
  const allPoints = frontiers.flatMap(query => query.data?.points ?? []).filter(point => !zeroBudgetIds.has(point.portfolio_id));
  const displayedBudgets = budgetRows.filter(row => row.budget_minor !== 0);
  const displayedFrontiers = frontiers.flatMap(query => query.data && query.data.budget.budget_minor !== 0 ? [query.data] : []);
  const firstFrontier = frontiers.find(query => query.data)?.data;
  const availableFleetIds = firstFrontier?.available_fleet_ids ?? configuredFleetIds;
  const selectedFleetIds = firstFrontier?.selected_fleet_ids ?? (validRequestedFleetIds.length ? validRequestedFleetIds : availableFleetIds);
  const uniquePoints = [...new Map(allPoints.map(point => [point.portfolio_id, point])).values()];
  const nondominatedIds = new Set(allPoints.filter(point => point.nondominated).map(point => point.portfolio_id));
  const nondominatedOnly = params.get("nondominated_only") !== "0";
  const visiblePoints = nondominatedOnly ? uniquePoints.filter(point => nondominatedIds.has(point.portfolio_id)) : uniquePoints;
  const requestedPortfolio = params.get("portfolio") ?? (resourceId === preferred.get("resource") ? preferred.get("portfolio") : null) ?? "";
  const frontiersSettled = frontiers.every(query => !query.isPending);
  const defaultPortfolio = [...displayedFrontiers].sort((a, b) => b.budget.budget_minor - a.budget.budget_minor)[0]?.best_mean_portfolio_id;
  const portfolioId = requestedPortfolio && (!frontiersSettled || uniquePoints.some(point => point.portfolio_id === requestedPortfolio)) ? requestedPortfolio : defaultPortfolio ?? uniquePoints[0]?.portfolio_id ?? "";
  const selectedPoint = uniquePoints.find(point => point.portfolio_id === portfolioId);
  const timeBins = [...(bins.data?.items ?? [])].sort((a, b) => a.canonical_index - b.canonical_index);
  const timeBin = timeBins.some(row => row.time_bin_id === params.get("time_bin")) ? params.get("time_bin")! : "all";
  const summaryMatrix = useQuery({ queryKey: ["portfolio-summary-matrix", resourceId, portfolioId, "mean", timeBin], enabled: Boolean(resourceId && portfolioId), queryFn: () => matrixQuery({ resource_id: resourceId, kind: "portfolio_summary", portfolio_id: portfolioId, time_bin_ids: timeBin === "all" ? [] : [timeBin], temporal_aggregation: "sum", statistic: "mean" }) });
  const jobConfig = sourceJob?.result?.config as { saturation_minutes?: number } | undefined;
  const saturationSeconds = Number(jobConfig?.saturation_minutes ?? ((resolvedPortfolio?.utility?.saturation_s ?? 300) / 60)) * 60;
  const budgetMetric = params.get("budget_metric") === "coverage" ? "coverage" : "utility";
  const coverageSeries = useQuery({
    queryKey: ["portfolio-budget-series", resourceId, fleetQuery],
    queryFn: () => requestJson<PortfolioBudgetSeriesView>(`/api/v1/portfolio-budget-series/${resourceId}?${fleetQuery}`),
    enabled: Boolean(resourceId && budgetMetric === "coverage"),
  });
  const coverageByPortfolio = new Map((coverageSeries.data?.rows ?? []).map(row => [row.portfolio.portfolio_id, {
    mean: row.coverage_mean_fraction,
    p05: row.coverage_p05_fraction,
    p50: row.coverage_p50_fraction,
    p95: row.coverage_p95_fraction,
  }]));
  const saturatedOnly = params.get("saturated_only") === "1";
  const saturatedCount = summaryMatrix.data?.values.filter(row => row.value >= saturationSeconds).length ?? 0;
  const spaceTimeUnitCount = summaryMatrix.data?.expected_shape.reduce((product, size) => product * size, 1) ?? 0;
  const saturatedPercentage = spaceTimeUnitCount > 0 ? 100 * saturatedCount / spaceTimeUnitCount : null;
  const mappedSummary = useMemo(() => {
    const mapped = attachValues(grid.data, summaryMatrix.data);
    return mapped && saturatedOnly ? { ...mapped, features: mapped.features.filter(feature => Number(feature.properties.value ?? 0) > saturationSeconds) } : mapped;
  }, [grid.data, summaryMatrix.data, saturatedOnly, saturationSeconds]);
  const exportOptions = [
    ["analysis", "portfolio_statistics", "Portfolio utility statistics"], ["analysis", "budget_frontiers", "Budget frontiers"], ["analysis", "portfolio_sensing_statistics", "Portfolio sensing statistics"], ["analysis", "budget_levels", "Budget settings"],
    ["samples", "portfolio_counts", "Sensor counts"], ["samples", "portfolio_samples", "Retained sample utilities"], ["samples", "sample_selection", "Retained vehicle selections"], ["samples", "sampling_rounds", "Sampling identities"], ["exposure", "exposure", "Physical vehicle exposure"],
  ] as const;
  const requestedExport = params.get("export_table") ?? "analysis:portfolio_statistics";
  const exportChoice = exportOptions.some(([source, table]) => `${source}:${table}` === requestedExport) ? requestedExport : "analysis:portfolio_statistics";
  const [exportSource, exportTable] = exportChoice.split(":");
  const exportResourceId = exportSource === "analysis" ? resourceId : exportSource === "samples" ? sampleId : exposureId;
  useEffect(() => { if (resourceId && params.get("analysis_resource") !== resourceId) set("analysis_resource", resourceId); }, [resourceId, params, set]);
  useEffect(() => { if (portfolioId && params.get("portfolio") !== portfolioId) set("portfolio", portfolioId); }, [portfolioId, params, set]);
  if (sources.length === 0) return <Alert severity="info">Run portfolio analysis from a completed simulation to view results.</Alert>;
  const p05Risk = firstFrontier?.risk_metric === "p05";
  const error = analysis.error ?? budgets.error ?? samplesManifest.error ?? exposure.error ?? grid.error ?? bins.error ?? frontiers.find(query => query.error)?.error ?? summaryMatrix.error ?? coverageSeries.error;
  const traces = portfolioTraces(frontiers.flatMap(query => query.data ? [query.data] : []), portfolioId, nondominatedOnly);
  const budgetTraces = portfolioBudgetTrace(displayedFrontiers, budgetMetric, coverageByPortfolio);
  const unit = budgetRows[0]?.cost_unit ?? "cost units";
  const updateFleetScope = (fleet: string, checked: boolean) => {
    const next = new Set(selectedFleetIds);
    if (checked) next.add(fleet); else next.delete(fleet);
    if (!next.size) return;
    const ordered = [...next].sort();
    setParams(previous => {
      const value = new URLSearchParams(previous);
      if (ordered.length === availableFleetIds.length) value.delete("fleet_scope"); else value.set("fleet_scope", ordered.join(","));
      value.delete("portfolio");
      return value;
    }, { replace: true });
  };
  return <Stack gap={2.5}>
    <Paper variant="outlined" className="result-context"><Stack gap={1.25}>
      <TextField disabled={false} select label="Portfolio analysis" value={resourceId} onChange={event => {setParams(previous => {const next=new URLSearchParams(previous);next.set("analysis_resource",event.target.value);next.delete("portfolio");next.delete("time_bin");return next;},{replace:true});}}>{[...new Map(sources.map(job => [artifactFromJob(job)!.artifact_id,job])).entries()].map(([id,job]) => <MenuItem key={id} value={id}>{String(job.result?.name ?? "Saved portfolio analysis")}</MenuItem>)}</TextField>
      <Stack direction="row" justifyContent="space-between" gap={2} flexWrap="wrap"><Typography variant="subtitle2">{String(sourceJob?.result?.name ?? "Completed portfolio analysis")}</Typography><Link to="/portfolio">Analysis settings</Link></Stack>
      <Stack direction="row" gap={2} alignItems="center" flexWrap="wrap"><Typography variant="caption" color="text.secondary">Fleet scope</Typography>{availableFleetIds.map(fleet => <FormControlLabel key={fleet} control={<Checkbox checked={selectedFleetIds.includes(fleet)} disabled={selectedFleetIds.length === 1 && selectedFleetIds.includes(fleet)} onChange={event => updateFleetScope(fleet, event.target.checked)} />} label={fleetLabel(fleet)} />)}</Stack>
      <Stack direction="row" gap={1} alignItems="center" flexWrap="wrap"><Typography variant="caption" color="text.secondary">Budgets</Typography>{displayedBudgets.map(row => <Chip key={row.budget_id} size="small" variant="outlined" label={`${row.budget_minor / row.minor_unit_scale} ${row.cost_unit}`} />)}{firstFrontier && <><Chip size="small" label={`Simulation replications: ${firstFrontier.replications_R}`} /><Chip size="small" label={`Fleet sampling runs: ${firstFrontier.sampling_rounds_J}`} /></>}</Stack>
    </Stack></Paper>
    {(analysis.isFetching || budgets.isFetching || frontiers.some(query => query.isFetching) || coverageSeries.isFetching) && <LinearProgress aria-label="Loading portfolio results" />}
    {error && <Alert severity="error">{String(error)}</Alert>}
    {displayedBudgets.some(row => !row.frontier_enabled) && <Alert severity="warning">Frontiers require at least two sampling runs. Select a count portfolio to inspect its mean and quantiles.</Alert>}
    <Paper variant="outlined" className="analysis-panel"><Typography variant="h6">Budget comparison</Typography><Table size="small"><TableHead><TableRow><TableCell>Budget ({unit})</TableCell><TableCell align="right">Best mean utility</TableCell><TableCell align="right">Best P05 utility</TableCell><TableCell align="right">Frontier portfolios</TableCell></TableRow></TableHead><TableBody>{displayedFrontiers.map(response => <TableRow key={response.budget.budget_id}><TableCell>{response.budget.budget_minor / response.budget.minor_unit_scale}</TableCell><TableCell align="right">{formatUtility(response.max_mean_utility ?? null)}</TableCell><TableCell align="right">{formatUtility(response.max_p05_utility ?? null)}</TableCell><TableCell align="right">{response.frontier_portfolio_count ?? "—"}</TableCell></TableRow>)}</TableBody></Table><Typography variant="caption" color="text.secondary">Best mean and best P05 may belong to different portfolios. Values use the retained samples.</Typography></Paper>
    <Box className="portfolio-overview">
      <Box className="portfolio-overview-charts">
      <Paper variant="outlined" className="analysis-panel"><Stack direction="row" justifyContent="space-between" alignItems="flex-start" gap={2} flexWrap="wrap"><Box><Typography variant="h6">Utility frontier</Typography><Typography variant="body2" color="text.secondary">Compare all configured budgets. Select a point to inspect its sensor counts, costs and mean sensing coverage.</Typography></Box><FormControlLabel control={<Checkbox checked={nondominatedOnly} onChange={event => set("nondominated_only", event.target.checked ? "" : "0")} />} label="Show nondominated points only" /></Stack>
        <ScientificChart traces={traces} xTitle={p05Risk ? "Worst-case utility (5th percentile)" : "Std utility"} yTitle="Mean utility" onPoint={selectPoint} precision={5} horizontalLegend height={390} fallback={<PointTable p05Risk={p05Risk} unit={unit} points={visiblePoints} onPoint={selectPoint} />} />
        <Typography variant="caption" color="text.secondary">{p05Risk ? "Mean and P05 are both maximized. P05 is an empirical quantile, not a guaranteed minimum. " : "Mean is maximized; standard deviation is minimized. "}Lines connect nondominated points in the same color and budget category. Click a legend entry to hide or show its points and line together. Single-point categories have no line. Each combination is drawn once; color identifies its lowest feasible budget. Muted circles are dominated, solid diamonds are nondominated, and the outlined point is selected. Lines are visual guides, not interpolated portfolios.</Typography>
      </Paper>
      <Paper variant="outlined" className="analysis-panel"><Stack direction="row" justifyContent="space-between" alignItems="flex-start" gap={2} flexWrap="wrap"><Box><Typography variant="h6">Best mean portfolio by sensor count</Typography><Typography variant="body2" color="text.secondary">Maximum mean utility at each budget within the selected fleet scope.</Typography></Box><TextField select size="small" label="Metric" value={budgetMetric} onChange={event => set("budget_metric", event.target.value === "coverage" ? "coverage" : "")} sx={{ minWidth: 170 }}><MenuItem value="utility">Utility</MenuItem><MenuItem value="coverage">Spatial coverage</MenuItem></TextField></Stack>
        <ScientificChart traces={budgetTraces} xTitle="Equipped sensors" yTitle={budgetMetric === "utility" ? "Mean utility" : "Mean spatial coverage (%)"} onPoint={selectPoint} height={310} fallback={<Typography color="text.secondary">No budget-series values are available for this fleet scope.</Typography>} />
        <Typography variant="caption" color="text.secondary">Bars show means; diamonds mark medians, with empirical 5th–95th percentile whiskers. Hover to inspect composition; click to select.</Typography>
      </Paper>
      </Box>
      <Paper variant="outlined" className="analysis-panel"><Typography variant="h6" sx={{ mb: 1.5 }}>Selected portfolio</Typography>
        <TextField fullWidth select label="Equipped vehicles by fleet" value={selectedPoint ? portfolioId : ""} onChange={event => selectPoint(event.target.value)} sx={{ mb: 2.5 }} helperText="Select by counts, including portfolios whose frontier points overlap.">{uniquePoints.map(point => <MenuItem key={point.portfolio_id} value={point.portfolio_id}>{countLabel(point)}</MenuItem>)}</TextField>
        {selectedPoint ? <PortfolioInspector point={selectedPoint} unit={unit} p05Risk={p05Risk} saturatedPercentage={saturatedPercentage} meanCoverageFraction={summaryMatrix.data?.mean_coverage_fraction ?? null} /> : <Typography color="text.secondary">Select a feasible point.</Typography>}
      </Paper>
    </Box>
    <Paper variant="outlined" className="analysis-panel portfolio-mean-map"><Stack direction="row" justifyContent="space-between" alignItems="flex-start" gap={2} flexWrap="wrap" sx={{ mb: 2 }}><Box><Typography variant="h6">Portfolio mean sensing duration</Typography><Typography variant="body2" color="text.secondary">Mean coverage of the selected sensor-count portfolio across sampling runs.</Typography></Box>
      <Stack gap={1} alignItems="flex-end"><TextField select label="Reporting bin" value={timeBin} onChange={event => set("time_bin", event.target.value)} sx={{ minWidth: 230 }}><MenuItem value="all">Whole observation window</MenuItem>{timeBins.map(row => <MenuItem key={row.time_bin_id} value={row.time_bin_id}>{(row.start_s / 3600).toFixed(2)}–{(row.end_s / 3600).toFixed(2)} elapsed h</MenuItem>)}</TextField><FormControlLabel control={<Checkbox checked={saturatedOnly} onChange={event => set("saturated_only", event.target.checked ? "1" : "")} />} label={`Only cells above ${saturationSeconds / 60} min saturation`} /></Stack></Stack>
      <ScientificMap loading={summaryMatrix.isFetching || grid.isFetching} data={mappedSummary} mode="duration" unit={summaryMatrix.data?.unit} fallback={<MatrixTable matrix={summaryMatrix.data} />} />
      <Typography variant="caption" color="text.secondary">{saturatedPercentage == null ? "Saturation coverage is unavailable." : `${saturatedCount} of ${spaceTimeUnitCount} displayed space–time units (${saturatedPercentage.toFixed(2)}%) have mean sensing duration at least ${saturationSeconds / 60} minutes.`}</Typography>
    </Paper>
    <Accordion><AccordionSummary>Export analysis data</AccordionSummary><AccordionDetails><Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>Full-precision results and retained sample provenance remain available for reproducibility.</Typography><Stack direction="row" alignItems="flex-start" gap={2} flexWrap="wrap"><TextField select label="Table to export" value={exportChoice} onChange={event => set("export_table", event.target.value)} sx={{ minWidth: 280 }}>{exportOptions.map(([source, table, label]) => <MenuItem key={`${source}:${table}`} value={`${source}:${table}`}>{label}</MenuItem>)}</TextField>{exportResourceId && sourceJob && <ExportActions resourceId={exportResourceId} table={exportTable} sourceRevisionId={revisionForJob(sourceJob.job_id)} />}</Stack></AccordionDetails></Accordion>
  </Stack>;
}
