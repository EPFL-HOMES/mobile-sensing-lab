import { Accordion, AccordionDetails, AccordionSummary, Alert, Button, Checkbox, FormControlLabel, MenuItem, Paper, Stack, TextField, Typography } from "@mui/material";
import { useQuery } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import type { EnvironmentEditor, EnvironmentResult, FeatureSelection, InputDescriptor, RegionSearchResult, JobSubmission } from "../api/generated";
import { requestJson } from "../api/client";
import type { JobSnapshot } from "../api/client";
import { ApiIssues } from "../components/ApiIssues";
import { JobMonitor } from "../components/JobMonitor";
import { ScientificMap } from "../components/ScientificMap";
import { Page } from "../components/Page";
import { useWorkspace } from "../app/workspace";
import type { GeoJsonValue } from "./results/types";

interface Preview { roads: GeoJsonValue; boundary: GeoJsonValue; road_count: number; preview_road_count: number; display_line_count?: number; simplification_m?: number; cell_count: number; working_crs: string }
const categories = ["transportation", "residential", "commercial", "industrial", "public_services", "leisure"] as const;

export function EnvironmentPage() {
  const workspace = useWorkspace();
  const environment = workspace.draft.environment;
  const defaults = useQuery({ queryKey: ["environment-defaults"], queryFn: () => requestJson<EnvironmentEditor>("/api/v1/workbench/environment/defaults") });
  const inputs = useQuery({ queryKey: ["inputs", workspace.project?.project_id], queryFn: () => requestJson<InputDescriptor[]>("/api/v1/inputs") });
  const config = environment.editor ?? defaults.data;
  const [boundaryMode, setBoundaryMode] = useState<"file" | "search">(environment.editor?.region_query ? "search" : "file");
  const [searchJob, setSearchJob] = useState<string | null>(null);
  const [searched, setSearched] = useState<RegionSearchResult | null>(null);
  const [searching, setSearching] = useState(false);
  const activeSearch = useRef<string | null>(null);
  const queryRef = useRef(config?.region_query?.trim());
  queryRef.current = config?.region_query?.trim();
  useEffect(() => { activeSearch.current = null; setSearched(null); setSearchJob(null); setSearching(false); setBoundaryMode(environment.editor?.region_query ? "search" : "file"); }, [workspace.project?.project_id]);
  const regionReady = useCallback((job: JobSnapshot) => {
    if (activeSearch.current !== job.job_id) return;
    void requestJson<RegionSearchResult>(`/api/v1/workbench/region-search/${job.resource_id}`).then((value) => {
      if (activeSearch.current === job.job_id && value.query === queryRef.current) { setSearched(value); setSearching(false); }
    }).catch((error) => { if (activeSearch.current === job.job_id) { setError(String(error)); setSearching(false); } });
  }, []);
  const searchRegion = async () => {
    setSearching(true); setError(null); setSearched(null);
    const query = config?.region_query?.trim();
    try { const job = await requestJson<JobSubmission>("/api/v1/workbench/region-search", { method: "POST", body: JSON.stringify({ query }) }); if (query === queryRef.current) { activeSearch.current = job.job_id; setSearchJob(job.job_id); } }
    catch (error) { setError(String(error)); setSearching(false); }
  };
  const [error, setError] = useState<string | null>(null);
  const [featureChoice, setFeatureChoice] = useState("");
  const preparedFeatures = environment.result ?? workspace.draft.authoring?.prepared_environment;
  const featureNames = preparedFeatures?.feature_names ?? [];
  const featureName = featureNames.includes(featureChoice) ? featureChoice : featureNames.find(name => name !== "uniform") ?? featureNames[0] ?? "";
  const [featureView, setFeatureView] = useState<"grid" | "source">("grid");
  const [sourceChoice, setSourceChoice] = useState("");
  const sourceInputs = (inputs.data ?? []).filter(input => ["feature", "population", "weight"].includes(input.role) && (Boolean(input.source_crs) || input.columns.some(column => ["geometry", "geom", "x", "longitude", "lon"].includes(column))));
  const sourceId = sourceInputs.some(input => input.input_id === sourceChoice) ? sourceChoice : sourceInputs[0]?.input_id ?? "";
  const sourceColumns = sourceInputs.find(input => input.input_id === sourceId)?.columns ?? [];
  const sourceSelection = config?.features?.find(item => item.input_id === sourceId) ?? { input_id: sourceId, name: "Source feature", value_column: "", unit: "feature", x_column: sourceColumns.includes("x") ? "x" : sourceColumns.includes("longitude") ? "longitude" : "lon", y_column: sourceColumns.includes("y") ? "y" : sourceColumns.includes("latitude") ? "latitude" : "lat" };
  const displayedView = environment.prepared ? featureView : "source";
  const featureMap = useQuery({ queryKey: ["prepared-feature-map", workspace.project?.project_id, preparedFeatures?.features.artifact_id, featureName, displayedView, sourceSelection],
    enabled: displayedView === "source" ? Boolean(sourceId) : Boolean(environment.prepared && preparedFeatures && featureName),
    queryFn: () => displayedView === "source"
      ? requestJson<GeoJsonValue & {unit: string; zero_cell_count: number; sources: Array<Record<string,unknown>>}>("/api/v1/workbench/feature-source/map", {method:"POST", body:JSON.stringify(sourceSelection)})
      : requestJson<GeoJsonValue & {unit: string; zero_cell_count: number; sources: Array<Record<string,unknown>>}>(`/api/v1/workbench/features/${preparedFeatures!.features.artifact_id}/map?feature=${encodeURIComponent(featureName)}`) });
  const preview = useQuery({ queryKey: ["environment-preview", environment.prepared?.artifact_id], enabled: Boolean(environment.prepared),
    queryFn: () => requestJson<Preview>(`/api/v1/workbench/environments/${environment.prepared!.artifact_id}/map`) });
  const matchingSearch = boundaryMode === "search" && searched?.query === config?.region_query?.trim() ? searched : null;
  const mapContext = useMemo(() => !matchingSearch && preview.data ? [preview.data.boundary] : undefined, [preview.data, matchingSearch]);
  const update = (patch: Partial<EnvironmentEditor>) => workspace.updateDraft((draft) => {
    draft.environment.editor = { ...config, ...patch };
    draft.environment.prepared = null; draft.environment.result = undefined; draft.environment.jobId = undefined;
    if (draft.authoring) { draft.authoring.environment = draft.environment.editor; draft.authoring.prepared_environment = null; }
    return draft;
  });
  const updateFeature = (index: number, patch: Partial<FeatureSelection>) => update({ features: (config?.features ?? []).map((f, i) => i === index ? { ...f, ...patch } : f) });
  const prepared = useCallback((job: JobSnapshot) => {
    // Job envelopes also contain resource/dependency metadata; only the public
    // EnvironmentResult belongs in the closed authoring configuration.
    void requestJson<EnvironmentResult>(`/api/v1/workbench/environments/${job.resource_id}/result`).then(result => {
      workspace.updateDraft((draft) => {
        draft.environment.prepared = result.artifact; draft.environment.result = result;
        if (draft.authoring) { draft.authoring.prepared_environment = result; draft.authoring.environment = draft.environment.editor; }
        return draft;
      });
    }).catch(caught => setError(caught instanceof Error ? caught.message : String(caught)));
  }, [workspace]);
  const prepare = async () => {
    setError(null);
    try {
      const job = await workspace.submitJob("/api/v1/workbench/environments", "studio_environment", { config });
      workspace.updateDraft((draft) => { draft.environment.jobId = job.job_id; return draft; });
    } catch (caught) { setError(caught instanceof Error ? caught.message : String(caught)); }
  };
  const selectInput = (label: string, key: "boundary_input" | "network_input" | "grid_input" | "speed_input", role: string, empty: string) =>
    <TextField select label={label} value={config?.[key] ?? ""} onChange={(e) => update({ [key]: e.target.value || null })}>
      <MenuItem value="">{empty}</MenuItem>{(inputs.data ?? []).filter((i) => i.role === role).map((i) => <MenuItem value={i.input_id} key={i.input_id}>{i.name}</MenuItem>)}
    </TextField>;
  if (!config) return <Page title="Environment" description="Loading environment settings…"><Typography>Loading…</Typography></Page>;
  return <Page title="Environment" description="Prepare the shared study area, routing network and sensing grid. Online acquisition runs as a cancellable background task."
    actions={<Button component={Link} to="/fleets" disabled={!environment.prepared}>Next: Fleet Configuration</Button>}>
    <ApiIssues error={workspace.error} onClear={workspace.clearError} />
    {error && <Alert severity="error">{error}</Alert>}
    {preview.error && <Alert severity="error">{preview.error.message}</Alert>}
    {!workspace.project && <Alert severity="info">Create or open a project first. <Link to="/project">Open Project</Link></Alert>}
    <div className="split-grid environment-workspace"><Stack gap={2}>
      <Paper variant="outlined" sx={{ p: 3 }}><Stack gap={2}>
        <Typography variant="h6">1. Study area</Typography>
        <TextField select label="Boundary source" value={boundaryMode} onChange={(e) => { activeSearch.current = null; setSearched(null); setSearchJob(null); setSearching(false); setBoundaryMode(e.target.value as typeof boundaryMode); update({ boundary_input: null, region_query: "", drawn_boundary: null }); }}>
          <MenuItem value="file">Registered boundary</MenuItem><MenuItem value="search">Search region (OSM)</MenuItem>
        </TextField>
        {boundaryMode === "file" && selectInput("Study boundary", "boundary_input", "boundary", "Select boundary from Data")}
        {boundaryMode === "search" && <><Stack direction="row" gap={1} alignItems="flex-start"><TextField fullWidth label="Region search" placeholder="Lausanne, Switzerland" value={config.region_query} onChange={(e) => { activeSearch.current = null; setSearchJob(null); setSearching(false); update({ region_query: e.target.value }); setSearched(null); }} helperText="Search updates the map on the right. Check the boundary before preparation." /><Button variant="outlined" sx={{ minHeight: 40 }} disabled={searching || !config.region_query?.trim()} onClick={() => void searchRegion()}>{searching ? "Searching…" : "Search"}</Button></Stack>
          {searchJob && <JobMonitor jobId={searchJob} sourceRevisionId={null} onCompleted={regionReady} onTerminal={(job) => { if (job.status !== "completed" && activeSearch.current === job.job_id) setSearching(false); }} />}
        </>}
        {config.drawn_boundary && <Alert severity="info">This saved project contains an earlier custom boundary. Its geometry is preserved; select a boundary file or search to replace it.</Alert>}
        <Accordion><AccordionSummary>Coordinate projection</AccordionSummary><AccordionDetails><TextField fullWidth label="Working CRS" value={config.working_crs} onChange={(e) => update({ working_crs: e.target.value })} helperText="auto suggests a local UTM projection for city extents; EPSG:2056 is supported for Lausanne." /></AccordionDetails></Accordion>
        <Accordion><AccordionSummary>Advanced time settings · {config.timezone}</AccordionSummary><AccordionDetails><TextField label="Local time zone" value={config.timezone} onChange={(e) => update({ timezone: e.target.value })} helperText="IANA time zone for civil-day inputs and daylight-saving transitions." /></AccordionDetails></Accordion>
      </Stack></Paper>
      <Paper variant="outlined" sx={{ p: 3 }}><Stack gap={2}>
        <Typography variant="h6">2. Grid</Typography>
        {selectInput("Grid source", "grid_input", "grid", "Generate regular grid")}
        {!config.grid_input && <TextField label="Grid side length (m)" type="number" value={config.grid_size_m} onChange={(e) => update({ grid_size_m: Number(e.target.value) })} helperText="The backend fixes and records the grid origin." />}
      </Stack></Paper>
      <Paper variant="outlined" sx={{ p: 3 }}><Stack gap={2}>
        <Typography variant="h6">3. Road network</Typography>
        {selectInput("Road source", "network_input", "network", "Acquire drivable roads from OSM")}
        {!config.network_input && <TextField label="Routing buffer beyond study area (m)" type="number" value={config.routing_buffer_m} onChange={(e) => update({ routing_buffer_m: Number(e.target.value) })} />}
        <Typography color="text.secondary">Supplied roads retain complete coverage. Routing may leave the sensing area.</Typography>
      </Stack></Paper>
      <Paper variant="outlined" sx={{ p: 3 }}><Stack gap={2}>
        <Typography variant="h6">4. Road speed</Typography>
        <TextField select label="Travel-time assumptions" value={config.speed_source} onChange={(e) => update({ speed_source: e.target.value as EnvironmentEditor["speed_source"] })}>
          <MenuItem value="constant">Uniform assumed speed</MenuItem><MenuItem value="file">Uploaded directed-edge speeds</MenuItem><MenuItem value="osm">OSM speed-limit tags</MenuItem>
        </TextField>
        {config.speed_source === "file" && <>{selectInput("Road speeds", "speed_input", "speed", "Select speed table")}
          <TextField label="Speed column (km/h)" value={config.speed_column} onChange={(e) => update({ speed_column: e.target.value })} helperText="Rows join to roads by u, v, key." /></>}
        {config.speed_source === "osm" && <Alert severity="info">OSM limits are not measured traffic speeds. Ambiguous tags use the explicit fallback below.</Alert>}
        <TextField label={config.speed_source === "constant" ? "Assumed speed (km/h)" : "Fallback speed (km/h)"} type="number" value={config.speed_kph} onChange={(e) => update({ speed_kph: Number(e.target.value) })} />
      </Stack></Paper>
      <Paper variant="outlined" sx={{ p: 3 }}><Stack gap={2}>
        <Typography variant="h6">5. Spatial features <Typography component="span" variant="body2" color="text.secondary">· optional</Typography></Typography>
        <Typography color="text.secondary">Population, activity measures and custom values use the same feature workflow. Values are aggregated to the grid and normalized when used for demand, vehicle locations or utility.</Typography>
        <Accordion><AccordionSummary>Acquire features from OpenStreetMap</AccordionSummary><AccordionDetails><Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>Each category produces one dimensionless grid indicator. Residential and industrial use normalized area; transportation and public services use normalized object count; commercial and leisure combine normalized count and area equally. These are spatial proxies, not measured population.</Typography>{categories.map((category) => <FormControlLabel key={category} label={category.replaceAll("_", " ")} control={<Checkbox checked={(config.osm_features ?? []).includes(category)} onChange={(_, checked) => update({ osm_features: checked ? [...config.osm_features ?? [], category] : config.osm_features?.filter((c) => c !== category) })} />} />)}</AccordionDetails></Accordion>
        {(config.features ?? []).map((feature, index) => <Paper key={index} variant="outlined" sx={{ p: 2 }}><Stack gap={1.5}>
          <TextField select label="Registered feature" value={feature.input_id} onChange={(e) => updateFeature(index, { input_id: e.target.value })}>
            <MenuItem value="">Select from Data</MenuItem>
            {(inputs.data ?? []).filter((i) => ["population", "feature", "weight"].includes(i.role)).map((i) => <MenuItem value={i.input_id} key={i.input_id}>{i.name}</MenuItem>)}
          </TextField>
          <div className="form-grid">
            <TextField label="Feature name" value={feature.name} onChange={(e) => updateFeature(index, { name: e.target.value })} />
            <TextField label="Value column" value={feature.value_column ?? ""} onChange={(e) => updateFeature(index, { value_column: e.target.value })} helperText="Blank uses geometry counts, lengths and areas." />
            <TextField label="Value unit" value={feature.unit ?? "count"} onChange={(e) => updateFeature(index, { unit: e.target.value })} />
            <TextField label="Year filter (optional)" value={feature.year ?? ""} type="number" onChange={(e) => updateFeature(index, { year: e.target.value ? Number(e.target.value) : null })} />
            <TextField label="X column" value={feature.x_column ?? "easting"} onChange={(e) => updateFeature(index, { x_column: e.target.value })} />
            <TextField label="Y column" value={feature.y_column ?? "northing"} onChange={(e) => updateFeature(index, { y_column: e.target.value })} />
            <TextField select label="Coordinate meaning" value={feature.coordinate_anchor ?? "point"} onChange={(e) => updateFeature(index, { coordinate_anchor: e.target.value as FeatureSelection["coordinate_anchor"] })}>
              <MenuItem value="point">Point / cell centre</MenuItem><MenuItem value="lower_left">Grid lower-left corner</MenuItem>
            </TextField>
            {feature.coordinate_anchor === "lower_left" && <TextField label="Source cell side length (m)" type="number" value={feature.source_cell_size_m ?? 100} onChange={(e) => updateFeature(index, { source_cell_size_m: Number(e.target.value) })} />}
          </div>
          <Button onClick={() => update({ features: config.features?.filter((_, i) => i !== index) })}>Remove feature</Button>
        </Stack></Paper>)}
        <Button sx={{ alignSelf: "flex-start" }} variant="outlined" onClick={() => update({ features: [...config.features ?? [], { input_id: "", name: "", value_column: "", unit: "count" }] })}>Add uploaded feature</Button>
      </Stack></Paper>
      <Paper variant="outlined" className="form-actions"><Typography variant="body2" color="text.secondary">Prepare once to make the shared grid, roads and features available to every fleet.</Typography><Button variant="contained" disabled={!workspace.project || workspace.draft.authoring?.read_only} onClick={() => void prepare()}>Prepare environment</Button></Paper>
    </Stack><Stack gap={2} className="environment-preview">
      <Paper variant="outlined" className="analysis-panel"><Typography variant="h6">{matchingSearch ? matchingSearch.name : environment.prepared ? "Prepared environment" : "Study area preview"}</Typography><Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>{matchingSearch ? "Search boundary · ready to prepare" : environment.prepared ? "Shared road network and sensing extent" : "Search for a region or prepare a registered boundary to view its extent."}</Typography>
      <ScientificMap network={!matchingSearch} label={matchingSearch ? "Study region map" : "Prepared road network map"} fitKey={matchingSearch ? `search:${matchingSearch.query}` : environment.prepared?.artifact_id} loading={searching || preview.isFetching} data={matchingSearch ? matchingSearch.boundary as unknown as GeoJsonValue : preview.data?.roads ?? null} contextData={mapContext} mode={matchingSearch ? "region" : "movement"} fallback={<Alert severity="info">Geographical inputs remain available in Data; prepared tables can be exported.</Alert>} />
      {matchingSearch && <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1.5 }}>{matchingSearch.source}</Typography>}
      {!matchingSearch && preview.data && <Typography variant="body2" color="text.secondary" sx={{ mt: 1.5 }}>{preview.data.cell_count.toLocaleString()} cells · {preview.data.road_count.toLocaleString()} directed edges · {preview.data.working_crs}. Complete network geometry; coincident directions share one display line. {preview.data.simplification_m ?? 2} m display simplification.</Typography>}
      </Paper>
      <Paper variant="outlined" className="analysis-panel"><Typography variant="h6">Spatial feature</Typography><Typography variant="body2" color="text.secondary" sx={{mb:2}}>Inspect source points, lines and polygons, or their prepared grid values. Displaying a layer does not change simulation weights.</Typography>
        <Stack gap={2}>
          <TextField disabled={false} select label="Feature representation" value={displayedView} onChange={e => setFeatureView(e.target.value as "grid" | "source")}>
            <MenuItem value="source">Original geographical features</MenuItem><MenuItem value="grid" disabled={!environment.prepared}>Prepared grid values</MenuItem>
          </TextField>
          {displayedView === "source" ? <TextField disabled={!sourceInputs.length} select label="Map source feature" value={sourceId} onChange={e => setSourceChoice(e.target.value)}>{sourceInputs.map(input => <MenuItem key={input.input_id} value={input.input_id}>{input.name}</MenuItem>)}</TextField> : <TextField disabled={false} select label="Map spatial feature" value={featureName} onChange={e => setFeatureChoice(e.target.value)}>{featureNames.map(name => <MenuItem key={name} value={name}>{name}</MenuItem>)}</TextField>}
          {displayedView === "source" && !sourceInputs.length && <Alert severity="info">Register point, line or polygon feature data in Data to inspect its original geometry. Cell-ID-only tables use the prepared grid view.</Alert>}
          {featureMap.error && <Alert severity="error">{featureMap.error.message}</Alert>}
          <ScientificMap label="Spatial feature map" data={featureMap.data ?? null} mode={displayedView === "source" ? "feature" : "duration"} unit={featureMap.data?.unit} fitKey={`${displayedView}:${displayedView === "source" ? sourceId : featureName}:${environment.prepared?.artifact_id ?? ""}`} contextData={mapContext} loading={featureMap.isFetching} fallback={<Typography>Feature values remain available through the project files.</Typography>} />
          {featureMap.data && <Typography variant="caption" color="text.secondary">{featureMap.data.returned_count.toLocaleString()} {displayedView === "source" ? "geometry parts · original values, not normalized" : `grid cells · ${featureMap.data.zero_cell_count.toLocaleString()} zero cells`} · {featureMap.data.unit}. {featureMap.data.sources.map(row => String(row.source ?? row.input_id ?? row.aggregation ?? "")).filter(Boolean).join(" · ")}</Typography>}
        </Stack>
      </Paper>
      {environment.jobId && <JobMonitor jobId={environment.jobId} sourceRevisionId={workspace.jobRevisionIds[environment.jobId] ?? null} onCompleted={prepared} />}
    </Stack></div>
  </Page>;
}
