import { DemandTimeEditor, ShiftGroupsEditor } from "../components/TemporalEditors";
import { useCallback, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Accordion, AccordionDetails, AccordionSummary, Alert, Button, Checkbox, FormControlLabel, MenuItem, Paper, Stack, Tab, Tabs, TextField, Typography } from "@mui/material";
import { requestJson } from "../api/client";
import type { JobSnapshot } from "../api/client";
import type { DemandEditor, DispatchEditor, FleetEditor, InputDescriptor, ProjectConfig, ProjectResolutionResult, SupplyEditor } from "../api/generated";
import { useWorkspace } from "../app/workspace";
import { Page } from "../components/Page";
import { JobMonitor } from "../components/JobMonitor";
import { ApiIssues } from "../components/ApiIssues";
import { FormSection, OptionHint } from "../components/FormSection";
import { SpatialWeightEditor } from "../components/SpatialWeightEditor";
import { featureRoles, InputLibrary } from "./DataPage";

const fleetRoles = ["demand", "supply", "assignment", "gtfs", "service_area", "area_assignment", "feature"] as const;
const movementLabels = { service_pickup: "Travel to first service stop", service_inter_step: "Travel between service stops", cruise: "Idle cruising", depot_return: "Depot return", reposition: "Scheduled repositioning" } as const;

export function FleetPage() {
  const workspace = useWorkspace();
  const projectDefaults = useQuery({ queryKey: ["project-defaults"], queryFn: () => requestJson<ProjectConfig>("/api/v1/workbench/project/defaults") });
  const fleetDefaults = useQuery({ queryKey: ["fleet-defaults"], queryFn: () => requestJson<FleetEditor>("/api/v1/workbench/fleet/defaults") });
  const inputs = useQuery({ queryKey: ["inputs", workspace.project?.project_id], queryFn: () => requestJson<InputDescriptor[]>("/api/v1/inputs") });
  const config = workspace.draft.authoring ?? projectDefaults.data;
  const [index, setIndex] = useState(0); const [tab, setTab] = useState("demand");
  const [name, setName] = useState(""); const [id, setId] = useState(""); const [error, setError] = useState("");
  const fleet = config?.fleets?.[index];
  const demand = fleet?.demand; const supply = fleet?.supply; const dispatch = fleet?.dispatch;
  const automaticDepot = Boolean(supply?.synthetic_depot && supply.depot_longitude == null && supply.depot_latitude == null);
  const source = inputs.data?.find((input) => input.input_id === demand?.input_id);
  const routes = useQuery({ queryKey: ["gtfs-routes", demand?.input_id], enabled: Boolean(demand?.template === "gtfs" && demand?.input_id),
    queryFn: () => requestJson<{ route_id: string; name: string }[]>(`/api/v1/workbench/inputs/${demand!.input_id}/gtfs-routes`) });
  const preparedEnvironment = workspace.draft.environment.result ?? config?.prepared_environment;
  const features = preparedEnvironment ? [...new Set([...preparedEnvironment.feature_names, "uniform"])] : [];
  const readOnly = config?.read_only ?? false;
  const mutate = (callback: (draft: ProjectConfig) => void) => {
    if (readOnly) return;
    workspace.updateDraft((draft) => {
    const value = structuredClone(draft.authoring ?? projectDefaults.data ?? {});
    value.environment = draft.environment.editor ?? value.environment;
    value.prepared_environment = draft.environment.result ?? value.prepared_environment;
    callback(value); draft.authoring = value; draft.resolution = undefined; draft.resolutionJobId = undefined; return draft;
    });
  };
  const update = (patch: Partial<FleetEditor>) => mutate((draft) => { draft.fleets = draft.fleets?.map((item, i) => i === index ? { ...item, ...patch } : item); });
  const updateDemand = (patch: Partial<DemandEditor>) => mutate((draft) => { const current = draft.fleets?.[index]; if (current) current.demand = { ...current.demand, ...patch }; });
  const updateSupply = (patch: Partial<SupplyEditor>) => mutate((draft) => { const current = draft.fleets?.[index]; if (current) current.supply = { ...current.supply, ...patch }; });
  const updateDispatch = (patch: Partial<DispatchEditor>) => mutate((draft) => { const current = draft.fleets?.[index]; if (current) current.dispatch = { ...current.dispatch, ...patch }; });
  const namedInput = (label: string, role: string, value: string | null | undefined, change: (value: string | null) => void) =>
    <TextField select label={label} value={value ?? ""} onChange={(e) => change(e.target.value || null)}><MenuItem value="">Select input</MenuItem>{inputs.data?.filter((input) => role === "feature" ? featureRoles.includes(input.role) : input.role === role).map((input) => <MenuItem key={input.input_id} value={input.input_id}>{input.name}</MenuItem>)}</TextField>;
  const featureWeights = (label: string, legacy: string, value: readonly { feature: string; weight?: number }[] | undefined, change: (value: { feature: string; weight: number }[]) => void) => <SpatialWeightEditor label={label} features={features} legacyFeature={legacy} value={value} disabled={readOnly || !preparedEnvironment} onChange={change} />;
  const completed = useCallback((job: JobSnapshot) => {
    workspace.updateDraft((draft) => { draft.resolution = job.result as unknown as ProjectResolutionResult; return draft; });
  }, [workspace]);
  const resolve = async () => {
    setError("");
    try {
      const resolvedConfig = { ...config, environment: workspace.draft.environment.editor ?? config?.environment, prepared_environment: workspace.draft.environment.result ?? config?.prepared_environment };
      const job = await workspace.submitJob("/api/v1/workbench/resolve", "studio_resolve", { config: resolvedConfig });
      workspace.updateDraft((draft) => { draft.resolutionJobId = job.job_id; return draft; });
    } catch (caught) { setError(caught instanceof Error ? caught.message : String(caught)); }
  };
  const mappedFields = demand?.content === "instances" ? ["task_id", "release_time", ...(demand?.task_type === "ordered" ? ["step_index", "scheduled_time"] : []), "service_seconds", "quantity", "vehicle_id"] : ["value", "interval_start", "interval_end"];
  const locationFields = demand?.coordinate_kind === "coordinates" ? ["x", "y"] : [demand?.coordinate_kind === "location_id" ? "location_id" : "cell_id"];
  const mappings = [...mappedFields, ...(demand?.task_type === "od" ? locationFields.flatMap((field) => [`origin_${field}`, `destination_${field}`]) : locationFields)];
  return <Page title="Fleet Configuration" description="Demand describes tasks; Supply freezes the physical vehicle catalog. Dispatch assigns tasks and Routing executes the selected travel-time model."
    actions={<Button component={Link} to="/simulation" disabled={!config?.fleets?.length}>Next: Simulation</Button>}>
    <ApiIssues error={workspace.error} onClear={workspace.clearError} />
    {error && <Alert severity="error">{error}</Alert>}
    {!workspace.draft.environment.prepared && <Alert severity="info">Prepare the shared environment before validating fleet inputs. <Link to="/environment">Open Environment</Link></Alert>}
    <Paper variant="outlined" sx={{ p: 2 }}><Stack direction="row" gap={2} flexWrap="wrap" alignItems="center">
      <TextField label="New fleet name" value={name} onChange={(e) => setName(e.target.value)} />
      <TextField label="New fleet ID" value={id} onChange={(e) => setId(e.target.value)} />
      <Button variant="contained" disabled={readOnly || !fleetDefaults.data || !name.trim() || !id.trim()} onClick={() => {
        if (config?.fleets?.some((item) => item.fleet_id === id.trim())) { setError("Fleet ID already exists"); return; }
        const next = config?.fleets?.length ?? 0;
        mutate((draft) => { draft.fleets = [...draft.fleets ?? [], { ...structuredClone(fleetDefaults.data!), name: name.trim(), fleet_id: id.trim() }]; });
        setIndex(next); setName(""); setId("");
      }}>Add fleet</Button>
    </Stack></Paper>
    <Accordion><AccordionSummary>Register fleet data: tasks, vehicles, assignments and GTFS</AccordionSummary><AccordionDetails><InputLibrary roles={fleetRoles} /></AccordionDetails></Accordion>
    {(config?.fleets?.length ?? 0) > 0 && <Tabs value={Math.min(index, (config?.fleets?.length ?? 1)-1)} onChange={(_, value: number) => setIndex(value)} variant="scrollable" scrollButtons="auto">{config?.fleets?.map((item) => <Tab label={item.name} key={item.fleet_id} />)}</Tabs>}
    {fleet && demand && supply && dispatch ? <Paper variant="outlined" sx={{ p: 3 }}><Stack gap={2.5}>
      <Stack direction="row" justifyContent="space-between"><Typography variant="h6">{fleet.name}</Typography><Button color="error" onClick={() => { mutate((draft) => { draft.fleets = draft.fleets?.filter((_, i) => i !== index); }); setIndex(Math.max(0, index-1)); }}>Remove fleet</Button></Stack>
      <Tabs value={tab} onChange={(_, value: string) => setTab(value)} variant="scrollable">{["demand", "supply", "dispatch", "routing", "sensing"].map((value) => <Tab label={value} value={value} key={value} />)}</Tabs>
      {tab === "demand" && <Stack gap={2}>
        <FormSection title="Task definition" description="Choose how tasks enter this fleet and what each task represents.">
        <div className="form-grid">
          <TextField select label="Demand source" value={demand.source} onChange={(e) => updateDemand({ source: e.target.value as DemandEditor["source"], temporal_mode: "window", time_profile: [], time_profile_input: null, content: "instances", count_semantics: null, generation_timing: "offline", template: "table", task_type: "location" })}><MenuItem value="import">Import tasks or demand table</MenuItem><MenuItem value="generator">Generate tasks</MenuItem></TextField>
          <TextField select label="Task type" value={demand.task_type} onChange={(e) => updateDemand({ task_type: e.target.value as DemandEditor["task_type"] })}><MenuItem value="location">Location</MenuItem><MenuItem value="od">Origin–destination</MenuItem>{demand.source === "import" && <MenuItem value="ordered">Timetable / ordered stops</MenuItem>}</TextField>
        </div>
        </FormSection>
        {demand.source === "import" ? <>
          {demand.task_type === "ordered" && <FormControlLabel label="This is a GTFS feed" control={<Checkbox checked={demand.template === "gtfs"} onChange={(_, checked) => { update({ demand: { ...demand, template: checked ? "gtfs" : "table", input_id: null }, ...(checked ? { supply: { ...supply, source: "timetable", fleet_size: null, operating_start: null, operating_end: null, capacity_mode: "none" }, dispatch: { ...dispatch, mode: "scheduled" } } : {}) }); }} />} />}
          {namedInput("Demand input", demand.template === "gtfs" ? "gtfs" : "demand", demand.input_id, (value) => updateDemand({ input_id: value }))}
          {demand.template === "gtfs" ? <>
            <Alert severity="info">GTFS uses Scheduled dispatch and an inferred duty catalog by default. Neighboring service dates are resolved on one absolute timeline; results use the Simulation observation window.</Alert>
            {routes.error && <Alert severity="error">{routes.error.message}</Alert>}
            <TextField select label="Bus routes" SelectProps={{ multiple: true }} value={demand.route_ids ?? []} onChange={(e) => updateDemand({ route_ids: typeof e.target.value === "string" ? e.target.value.split(",") : e.target.value })}>
              {routes.data?.map((route) => <MenuItem key={route.route_id} value={route.route_id}>{route.name} ({route.route_id})</MenuItem>)}
            </TextField>
          </> : <>
            <TextField select label="Table contents" value={demand.content} onChange={(e) => updateDemand({ content: e.target.value as DemandEditor["content"], count_semantics: null })}><MenuItem value="instances">Task instances</MenuItem>{demand.task_type !== "ordered" && <MenuItem value="counts">Grid / OD quantity table</MenuItem>}{demand.task_type !== "ordered" && <MenuItem value="rates">Grid / OD demand-rate table</MenuItem>}</TextField>
            {demand.content === "counts" && <TextField select label="Meaning of quantity column" value={demand.count_semantics ?? ""} onChange={(e) => updateDemand({ count_semantics: e.target.value as DemandEditor["count_semantics"] })}><MenuItem value="task_count">Number of tasks (integer)</MenuItem><MenuItem value="service_quantity">Service quantity (one task per positive row)</MenuItem></TextField>}
            {demand.content === "rates" && <TextField select label="Rate unit" value={demand.rate_unit} onChange={(e) => updateDemand({ rate_unit: e.target.value as DemandEditor["rate_unit"] })}><MenuItem value="per_hour">Tasks per hour</MenuItem><MenuItem value="per_day">Tasks per civil day</MenuItem></TextField>}
            <div className="form-grid"><TextField select label="Location representation" value={demand.coordinate_kind} onChange={(e) => updateDemand({ coordinate_kind: e.target.value as DemandEditor["coordinate_kind"] })}><MenuItem value="cell_id">Grid cell ID</MenuItem><MenuItem value="coordinates">Coordinates in the input CRS</MenuItem><MenuItem value="location_id">Prepared location ID</MenuItem></TextField>
              <TextField select label="Time representation" value={demand.time_unit} onChange={(e) => updateDemand({ time_unit: e.target.value as DemandEditor["time_unit"] })}>{["clock", "seconds", "minutes", "hours", "datetime"].map((unit) => <MenuItem key={unit} value={unit}>{unit === "datetime" ? "Datetime with explicit offset" : unit === "clock" ? "Local clock HH:MM:SS" : `${unit} since local midnight`}</MenuItem>)}</TextField></div>
            {source && <><Typography>Map source columns. Optional absent fields use the defaults below; errors retain source row numbers.</Typography><div className="form-grid">{mappings.map((field) => <TextField select key={field} label={field.replaceAll("_", " ")} value={demand.columns?.[field] ?? (source.columns.includes(field) ? field : "")} onChange={(e) => updateDemand({ columns: { ...demand.columns, [field]: e.target.value } })}><MenuItem value="">Not supplied / default</MenuItem>{source.columns.map((column) => <MenuItem key={column} value={column}>{column}</MenuItem>)}</TextField>)}</div></>}
            {source?.columns.includes("vehicle_id") && <Alert severity="info">Vehicle references were detected. Select Scheduled to execute these assignments, or retain them only as history when rescheduling.</Alert>}
            <TextField select label="Vehicle assignment information" value={demand.imported_assignment} onChange={(e) => { const value = e.target.value as DemandEditor["imported_assignment"]; update({ demand: { ...demand, imported_assignment: value }, ...(value === "execute" && source?.columns.includes("vehicle_id") ? { dispatch: { ...dispatch, mode: "scheduled" } } : {}) }); }}><MenuItem value="execute">Execute input assignments when present</MenuItem><MenuItem value="history_only">Keep as history and reschedule</MenuItem></TextField>
          </>}
        </> : <>
          <FormSection title="Task volume" description="Fixed totals preserve the count in each replication; expected totals retain random count variation.">
          <div className="form-grid"><TextField disabled={readOnly || demand.temporal_mode === "rates"} select label="Task volume mode" value={demand.volume_mode} onChange={(e) => updateDemand({ volume_mode: e.target.value as DemandEditor["volume_mode"] })}><MenuItem value="fixed">Fixed total tasks</MenuItem><MenuItem value="expected">Expected total tasks (Poisson)</MenuItem></TextField><TextField disabled={readOnly || demand.temporal_mode === "rates"} label={demand.volume_mode === "fixed" ? "Fixed task total" : "Expected task total"} type="number" value={demand.task_volume} onChange={(e) => updateDemand({ task_volume: Number(e.target.value) })} />
            <TextField select label="Generation timing" value={demand.generation_timing} onChange={(e) => updateDemand({ generation_timing: e.target.value as DemandEditor["generation_timing"] })}><MenuItem value="offline">Offline</MenuItem><MenuItem value="online">Online arrivals</MenuItem></TextField>
          </div></FormSection>
          <FormSection title="Spatial distribution" description="Use one or more prepared grid features for sampling across the shared study area."><Stack gap={2}>
            {featureWeights(demand.task_type === "od" ? "Origin spatial distribution" : "Task spatial distribution", demand.spatial_feature ?? "uniform", demand.spatial_weights, (value) => updateDemand({ spatial_weights: value }))}
            {demand.task_type === "location" && <TextField select label="Location reachability" value={demand.location_condition ?? "all_resolved"} onChange={(e) => updateDemand({ location_condition: e.target.value as DemandEditor["location_condition"] })}><MenuItem value="all_resolved">All resolved locations</MenuItem><MenuItem value="depot_roundtrip">Require a route from and back to the depot</MenuItem></TextField>}
            {demand.task_type === "od" && featureWeights("Destination spatial distribution", demand.destination_feature ?? "uniform", demand.destination_spatial_weights, (value) => updateDemand({ destination_spatial_weights: value }))}
          </Stack>
          {demand.task_type === "od" && <>{namedInput("Optional sparse OD distribution", "feature", demand.od_distribution_input, (value) => updateDemand({ od_distribution_input: value }))}<Typography variant="body2" color="text.secondary">Sparse columns: origin_cell_id, destination_cell_id, weight. Otherwise origins and destinations are drawn separately. Same-node and unreachable pairs are rejected and recorded.</Typography></>}
          </FormSection>
        </>}
        {demand.template !== "gtfs" && <>
        <FormSection title="Daily timing" description="Times use the local clock. Relative shares distribute the daily total across non-overlapping intervals.">
          {demand.source === "generator" && <DemandTimeEditor value={demand} onChange={updateDemand} input={namedInput("Temporal profile input", "demand", demand.time_profile_input, (value) => updateDemand({ time_profile_input: value, time_profile: [] }))} />}
          {(demand.source !== "generator" || !demand.temporal_mode || demand.temporal_mode === "window") && <div className="form-grid">
          <TextField disabled={readOnly || (demand.source === "generator" && demand.temporal_mode !== "window" && !!demand.temporal_mode)} label="Demand window starts" value={demand.start_time} onChange={(e) => updateDemand({ start_time: e.target.value })} />
          <TextField disabled={readOnly || (demand.source === "generator" && demand.temporal_mode !== "window" && !!demand.temporal_mode)} label="Demand window ends" value={demand.end_time} onChange={(e) => updateDemand({ end_time: e.target.value })} />
          {(demand.source === "generator" || demand.content !== "instances") && <TextField disabled={readOnly || (demand.source === "generator" && demand.temporal_mode !== "window" && !!demand.temporal_mode)} select label="Task release pattern" value={demand.release_mode} onChange={(e) => updateDemand({ release_mode: e.target.value as DemandEditor["release_mode"] })}><MenuItem value="uniform">Uniform over window</MenuItem><MenuItem value="at_start">All known at window start</MenuItem></TextField>}
          </div>}
        </FormSection>
        <FormSection title="Service requirements" description="Durations apply at stops; task quantity uses the same unit as vehicle capacity."><div className="form-grid">
          <TextField label={demand.task_type === "od" ? "Drop-off service duration (seconds)" : "Service duration (seconds)"} type="number" value={demand.service_seconds} onChange={(e) => updateDemand({ service_seconds: Number(e.target.value) })} />
          {demand.task_type === "od" && <TextField label="Pickup service duration (seconds)" type="number" value={demand.pickup_seconds} onChange={(e) => updateDemand({ pickup_seconds: Number(e.target.value) })} />}
          <TextField label="Service quantity per task" type="number" value={demand.quantity} onChange={(e) => updateDemand({ quantity: Number(e.target.value) })} helperText="Consumes capacity only when finite capacity is enabled." />
        </div></FormSection></>}
      </Stack>}
      {tab === "supply" && <Stack gap={2}>
        <FormSection title="Vehicle catalog" description="Defines physical vehicles, independently of how many carry sensors.">
        <TextField select label="Physical vehicle catalog" value={supply.source} onChange={(e) => updateSupply({ source: e.target.value as SupplyEditor["source"], shift_groups: [], ...(e.target.value === "timetable" ? { fleet_size: null, operating_start: null, operating_end: null } : { fleet_size: supply.fleet_size ?? 10, operating_start: supply.operating_start ?? "08:00", operating_end: supply.operating_end ?? "18:00" }) })}><MenuItem value="generated">Generate vehicles</MenuItem><MenuItem value="import">Import complete vehicle catalog</MenuItem>{demand.task_type === "ordered" && <MenuItem value="timetable">Infer duties from timetable</MenuItem>}</TextField>
        {supply.source === "import" && <>{namedInput("Vehicle input", "supply", supply.input_id, (value) => updateSupply({ input_id: value }))}<Typography>Map vehicle_id, initial_cell_id (or initial_x / initial_y), start_time, end_time, and optional capacity.</Typography><div className="form-grid">{["vehicle_id", "initial_cell_id", "initial_x", "initial_y", "start_time", "end_time", "capacity"].map((field) => <TextField key={field} label={field.replaceAll("_", " ") + " column"} value={supply.columns?.[field] ?? field} onChange={(e) => updateSupply({ columns: { ...supply.columns, [field]: e.target.value } })} />)}</div></>}
        {supply.source === "timetable" && <><FormControlLabel label="Auto fleet size: deterministic feasible duty inference" control={<Checkbox checked={supply.fleet_size == null} onChange={(_, checked) => updateSupply({ fleet_size: checked ? null : 10 })} />} /><Alert severity="info">Auto reports its inferred vehicle count after validation. It does not guarantee a minimum fleet. A fixed-size failure means the current rules did not find a plan.</Alert><FormControlLabel label="Vehicle IDs in this input explicitly describe the complete operating catalog" control={<Checkbox checked={supply.timetable_catalog_complete ?? false} onChange={(_, checked) => updateSupply({ timetable_catalog_complete: checked })} />} /></>}
        {supply.source !== "import" && supply.fleet_size != null && <TextField label="Fleet size" type="number" value={supply.fleet_size} onChange={(e) => updateSupply({ fleet_size: Number(e.target.value) })} />}
        </FormSection>
        <FormSection title="Operating hours and activation" description="Each vehicle works within the operating window. Activation controls when it starts, not the number of physical vehicles.">
        {supply.source === "timetable" && <FormControlLabel label="Auto operating window from duties" control={<Checkbox checked={supply.operating_start == null} onChange={(_, checked) => updateSupply({ operating_start: checked ? null : "00:00", operating_end: checked ? null : "24:00" })} />} />}
        {supply.operating_start != null && <div className="form-grid"><TextField label="Operating window starts" value={supply.operating_start} onChange={(e) => updateSupply({ operating_start: e.target.value })} /><TextField label="Operating window ends" value={supply.operating_end} onChange={(e) => updateSupply({ operating_end: e.target.value })} /></div>}
        {supply.source === "generated" && <><ShiftGroupsEditor value={supply} onChange={updateSupply} /><div className="form-grid">
          <TextField disabled={readOnly || !!supply.shift_groups?.length} select label="Activation" value={supply.activation} onChange={(e) => updateSupply({ activation: e.target.value as SupplyEditor["activation"] })}><MenuItem value="simultaneous">All start together</MenuItem><MenuItem value="uniform_bounded">Uniform bounded random starts</MenuItem></TextField>
          {supply.activation === "uniform_bounded" && <TextField disabled={readOnly || !!supply.shift_groups?.length} label="Latest vehicle start" value={supply.latest_start} onChange={(e) => updateSupply({ latest_start: e.target.value })} />}
          <TextField disabled={readOnly || !!supply.shift_groups?.length} label="Fixed work duration per vehicle (hours)" type="number" value={supply.work_hours} onChange={(e) => updateSupply({ work_hours: Number(e.target.value) })} />
        </div></>}
        </FormSection>
        <FormSection title="Locations and idle movement" description="Choose the starting location and movement while waiting for the next task.">
        {supply.source === "generated" && <div className="form-grid">
          <TextField select label="Initial locations" value={supply.initial_location} onChange={(e) => updateSupply({ initial_location: e.target.value as SupplyEditor["initial_location"] })}><MenuItem value="spatial_feature">Draw from spatial feature</MenuItem><MenuItem value="depot">Depot</MenuItem></TextField>
          {(supply.initial_location === "spatial_feature" || automaticDepot) && featureWeights(supply.initial_location === "spatial_feature" ? "Initial-location spatial distribution" : "Synthetic-depot spatial distribution", supply.spatial_feature ?? "uniform", supply.spatial_weights, (value) => updateSupply({ spatial_weights: value }))}
        </div>}
        {supply.source === "timetable" && <TextField label="Initial location" value="Each inferred duty's first stop" disabled />}
        <TextField select label="Post-service behavior" value={supply.post_service === "return_after_plan" ? "stationary" : supply.post_service} onChange={(e) => updateSupply({ post_service: e.target.value as SupplyEditor["post_service"] })} helperText={dispatch.mode === "one_shot" ? "One-shot vehicles return to the depot after completing their delivery plan." : "Applies only while the vehicle is active and has no assigned task."}><MenuItem value="stationary"><OptionHint label="Idling" hint="wait at the last location" /></MenuItem><MenuItem value="random_cruise"><OptionHint label="Cruising" hint="move randomly on the road network" /></MenuItem></TextField>
        </FormSection>
        <Accordion><AccordionSummary>Depot, capacity and service areas</AccordionSummary><AccordionDetails><Stack gap={2}>
          <FormControlLabel label="Create a synthetic depot near the supply feature's weighted centre" control={<Checkbox checked={automaticDepot} onChange={(_, checked) => updateSupply({ synthetic_depot: checked, ...(checked ? { depot_longitude: null, depot_latitude: null } : {}) })} />} />
          {!automaticDepot && <div className="form-grid"><TextField label="Depot longitude" type="number" value={supply.depot_longitude ?? ""} onChange={(e) => updateSupply({ depot_longitude: e.target.value ? Number(e.target.value) : null })} /><TextField label="Depot latitude" type="number" value={supply.depot_latitude ?? ""} onChange={(e) => updateSupply({ depot_latitude: e.target.value ? Number(e.target.value) : null })} /></div>}
          <TextField select label="Capacity mode" value={supply.capacity_mode} onChange={(e) => updateSupply({ capacity_mode: e.target.value as SupplyEditor["capacity_mode"] })}><MenuItem value="none">No capacity limit</MenuItem><MenuItem value="consumable">Consumable delivery quantity</MenuItem><MenuItem value="occupancy">Occupancy during OD task</MenuItem></TextField>
          {supply.capacity_mode !== "none" && <div className="form-grid"><TextField label="Capacity per vehicle" type="number" value={supply.capacity} onChange={(e) => updateSupply({ capacity: Number(e.target.value) })} /><TextField label="Capacity unit" value={supply.capacity_unit} onChange={(e) => updateSupply({ capacity_unit: e.target.value })} /></div>}
          {(supply.initial_location === "depot" || dispatch.mode === "one_shot") && <TextField label="Depot minimum stay (minutes)" type="number" value={supply.depot_min_stay_minutes ?? 10} onChange={(e) => updateSupply({ depot_min_stay_minutes: Number(e.target.value) })} helperText="Minimum replenishment time. Stock is restored only after this stay; stationary time at the depot contributes no sensing." />}
          <TextField select label="Service areas" value={supply.service_area_mode ?? "none"} onChange={(e) => {
            const mode = e.target.value as SupplyEditor["service_area_mode"];
            updateSupply({ service_area_mode: mode, service_area_input: null, area_assignment_input: null, auto_service_area_count: mode === "auto" ? (supply.auto_service_area_count ?? 4) : null });
          }}>
            <MenuItem value="none">No service-area restrictions</MenuItem>
            <MenuItem value="uploaded">Upload areas and vehicle assignments</MenuItem>
            <MenuItem value="auto">Auto · balance expected demand</MenuItem>
          </TextField>
          {supply.service_area_mode === "uploaded" && <>
            {namedInput("Service-area geometry", "service_area", supply.service_area_input, (value) => updateSupply({ service_area_input: value }))}
            {namedInput("Vehicle-area assignments", "area_assignment", supply.area_assignment_input, (value) => updateSupply({ area_assignment_input: value }))}
            <Typography color="text.secondary">Geometry uses area_id; assignments use vehicle_id and area_id. Every physical vehicle must be assigned explicitly.</Typography>
          </>}
          {supply.service_area_mode === "auto" && <>
            <TextField label="Number of auto service areas" type="number" value={supply.auto_service_area_count ?? 4} onChange={(e) => updateSupply({ auto_service_area_count: Number(e.target.value) })} slotProps={{ htmlInput: { min: 1, max: supply.fleet_size ?? 1000, step: 1 } }} />
            <Typography color="text.secondary">Before replications, expected origin demand is divided into deterministic spatially compact areas. Every area receives one vehicle; remaining physical vehicles are allocated by largest remainder of expected demand. Areas and assignments stay frozen across replications.</Typography>
          </>}
          <Typography color="text.secondary">Depot location never determines service-area membership. For OD demand, the origin selects the service area and the destination may cross its boundary.</Typography>
        </Stack></AccordionDetails></Accordion>
      </Stack>}
      {tab === "dispatch" && <Stack gap={2}>
        <TextField select label="Dispatch mode" value={dispatch.mode} onChange={(e) => update({ dispatch: { ...dispatch, mode: e.target.value as DispatchEditor["mode"], ...(e.target.value === "one_shot" ? { allow_replenishment: true } : {}) }, ...(e.target.value === "one_shot" ? { supply: { ...supply, initial_location: "depot", post_service: "stationary" } } : supply.post_service === "return_after_plan" ? { supply: { ...supply, post_service: "stationary" } } : {}) })}><MenuItem value="scheduled">Scheduled</MenuItem><MenuItem value="sequential">Sequential</MenuItem><MenuItem value="batch">Batch</MenuItem><MenuItem value="one_shot">One-shot</MenuItem></TextField>
        {dispatch.mode === "scheduled" && <>{namedInput("Assignment plan (optional when input or duties provide one)", "assignment", dispatch.assignment_input, (value) => updateDispatch({ assignment_input: value }))}<Alert severity="info">A complete input or inferred plan binds automatically. Each task must have one vehicle and one position in its sequence.</Alert></>}
        {["sequential", "batch"].includes(dispatch.mode ?? "") && <TextField label="Maximum pickup travel time (minutes)" type="number" value={dispatch.max_pickup_minutes} onChange={(e) => updateDispatch({ max_pickup_minutes: Number(e.target.value) })} helperText="Routed travel from the vehicle to the task's first stop; excludes waiting for a batch." />}
        {dispatch.mode === "batch" && <TextField label="Batch interval (minutes)" type="number" value={dispatch.batch_minutes} onChange={(e) => updateDispatch({ batch_minutes: Number(e.target.value) })} />}
        {dispatch.mode === "one_shot" && <><FormControlLabel label="Return to depot to replenish when capacity is insufficient" control={<Checkbox checked={dispatch.allow_replenishment ?? true} onChange={(_,checked) => updateDispatch({ allow_replenishment: checked })} />} /><Alert severity="info">Single depot and fixed vehicles. All offline delivery requests are mandatory. Planned replenishment, travel and service must fit each vehicle's working window; final return ends the route. Same-location requests are preferentially consecutive. Search is heuristic, without a global optimum guarantee.</Alert><Accordion><AccordionSummary>Planning limits</AccordionSummary><AccordionDetails><div className="form-grid"><TextField label="Maximum replenishments per vehicle" type="number" value={dispatch.max_replenishments_per_vehicle ?? 4} onChange={e => updateDispatch({ max_replenishments_per_vehicle:Number(e.target.value) })} helperText="A bounded search limit. If no plan is found, inspect this limit and the working windows." />
          <TextField label="Maximum directed location pairs" type="number" value={dispatch.max_cost_pairs} onChange={e => updateDispatch({ max_cost_pairs: Number(e.target.value) })} helperText="Planning fails explicitly if its routing-cost matrix exceeds this resource bound." />
          <TextField label="Search solution limit" type="number" value={dispatch.solution_limit} onChange={e => updateDispatch({ solution_limit: Number(e.target.value) })} helperText="Deterministic search stopping rule; a larger value does not guarantee an optimum." />
          <TextField label="Planning timeout (seconds)" type="number" value={dispatch.planning_timeout_seconds} onChange={e => updateDispatch({ planning_timeout_seconds: Number(e.target.value) })} helperText="A failure guard. Reaching it reports a timeout rather than accepting a timing-dependent result." />
        </div></AccordionDetails></Accordion></>}
      </Stack>}
      {tab === "routing" && <Stack gap={2}><TextField label="Travel-time scheme" value="Prepared environment · default" disabled /><Typography>Shortest travel time on the shared directed road network. Planning and execution use the same routing profile.</Typography><Button component={Link} to="/environment">Edit road speed assumptions in Environment</Button></Stack>}
      {tab === "sensing" && <Stack gap={2}><TextField select label="Sensing period" value={fleet.sensing_mode ?? "movement_duration"} onChange={e => update({sensing_mode:e.target.value as FleetEditor["sensing_mode"]})}><MenuItem value="operating_duration">Operating hours, excluding depot stays</MenuItem><MenuItem value="movement_duration">Movement only (historical mode)</MenuItem></TextField>{fleet.sensing_mode === "operating_duration" ? <><Alert severity="info">Includes driving, delivery service, pickup/drop-off, waiting and idling while in service. Stationary depot stays and off-duty intervals are excluded. Other locations in the same grid cell remain eligible.</Alert>{supply.source === "timetable" && <TextField label="Timetable off-duty idle threshold (minutes)" type="number" value={supply.timetable_idle_break_minutes ?? ""} onChange={e => updateSupply({timetable_idle_break_minutes:e.target.value ? Number(e.target.value):null})} helperText="An unassigned idle gap at least this long is treated as entirely off duty. This is an explicit inferred power schedule; service and scheduled waiting remain included. Blank retains every on-duty idle gap." />}</> : Object.entries(movementLabels).map(([kind, label]) => <FormControlLabel key={kind} label={label} control={<Checkbox checked={(fleet.sensing_movements ?? []).includes(kind as keyof typeof movementLabels)} onChange={(_, checked) => update({ sensing_movements: checked ? [...fleet.sensing_movements ?? [], kind as keyof typeof movementLabels] : fleet.sensing_movements?.filter((value) => value !== kind) })} />} />)}<Typography color="text.secondary">Operational supply stays fixed when sensor counts change in Portfolio.</Typography></Stack>}
      <div className="form-actions"><Typography variant="body2" color="text.secondary">Check configuration and input references. Task generation and route planning run with Simulation.</Typography><Button variant="contained" disabled={readOnly || !workspace.project || !workspace.draft.environment.prepared} onClick={() => void resolve()}>Validate fleet inputs</Button></div>
    </Stack></Paper> : <Alert severity="info">Add a fleet to configure Demand, Supply, Dispatch, Routing and sensing.</Alert>}
    {workspace.draft.resolution && <Paper variant="outlined" sx={{ p: 2 }}><Typography variant="h6">Fleet configuration check</Typography>{Object.entries(workspace.draft.resolution.vehicle_counts).map(([fleetId, count]) => <Typography key={fleetId}>{config?.fleets?.find((item) => item.fleet_id === fleetId)?.name ?? fleetId}: {count} physical vehicles{workspace.draft.resolution!.validation_level === "configuration" ? " (configured)" : ` · service tasks per replication: ${workspace.draft.resolution!.task_counts[fleetId]?.join(", ") ?? ""}`}</Typography>)}{workspace.draft.resolution.validation_level === "configuration" && <Alert severity="info">Configuration checks passed. Route feasibility, inferred duties and One-shot plans will be checked during Run simulation.</Alert>}{workspace.draft.resolution.assumptions.map((value) => <Typography color="text.secondary" key={value}>{value}</Typography>)}</Paper>}
    {workspace.draft.resolutionJobId && <JobMonitor jobId={workspace.draft.resolutionJobId} sourceRevisionId={workspace.jobRevisionIds[workspace.draft.resolutionJobId] ?? null} onCompleted={completed} />}
  </Page>;
}
