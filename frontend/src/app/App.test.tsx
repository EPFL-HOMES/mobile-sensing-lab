import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import fixture from "../test/studioFixture.json";
import type { InputDescriptor, ProjectConfig } from "../api/generated";
import { WorkspaceProvider } from "./workspace";
import { App } from "./App";

vi.mock("../components/ScientificMap", () => ({ ScientificMap: ({ data, fitKey }: { data: unknown; fitKey?: string }) => <div data-testid="scientific-map" data-fit-key={fitKey}>{JSON.stringify(data)}</div> }));

const project = { project_id: "project-1", name: "Acceptance project", description: "MR05", current_revision_id: "revision-1", created_at_utc: "2026-09-13T00:00:00Z", updated_at_utc: "2026-09-13T00:00:00Z" };
const empiricalOd: InputDescriptor = {
  input_id: "input_8110b5d05f4d21c2ebde33f2f80a1bf6", name: "Empirical joint OD — weekday", role: "demand",
  format: "csv", original_filename: "demand_joint_od_weekday.csv", file: "source.csv",
  columns: ["origin_cell_id", "destination_cell_id", "weight"], preview: [],
  source_crs: null, crs_required: false, layer: null, layers: [], size_bytes: 100,
  byte_hash: "a".repeat(64), content_hash: "b".repeat(64),
};
const weekendOd: InputDescriptor = {
  ...empiricalOd, input_id: "input_29ad4fe9229229f33d18f709f904d336", name: "Empirical joint OD — weekend",
  original_filename: "demand_joint_od_weekend.csv",
};
function odConfiguration(inputId: string | null = null): ProjectConfig {
  const configuration = structuredClone(fixture.config) as unknown as ProjectConfig;
  return { ...configuration, schema_version: "3.4", fleets: configuration.fleets!.map((fleet, index) => index ? fleet : {
    ...fleet, demand: { ...fleet.demand, source: "generator", task_type: "od", od_distribution_input: inputId },
  }) };
}
const storedDemand = () => JSON.parse(localStorage.getItem(`mobile-sensing-draft:${project.project_id}`) ?? "null")?.draft.authoring?.fleets?.[0]?.demand;
const response = (value: unknown, status = 200) => Promise.resolve(new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } }));
function installFetch(saved = true, overrides: Record<string, unknown> = {}) {
  const selected = { ...project, current_revision_id: saved ? project.current_revision_id : null };
  localStorage.setItem("mobile-sensing-workbench@1", JSON.stringify({ activeProjectId: project.project_id, trackedJobIds: [], jobRevisionIds: {} }));
  return vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
    const url = String(input);
    if (url in overrides) return response(overrides[url]);
    if (url === "/api/v1/projects") return response([selected]);
    if (url === `/api/v1/projects/${project.project_id}`) return response(selected);
    if (url.endsWith("/revisions/revision-1")) return response({ project_id: project.project_id, revision_id: "revision-1", base_revision_id: null, payload: fixture.config, created_at_utc: project.created_at_utc });
    if (url.endsWith("/migration")) return response({ config: { ...fixture.config, schema_version: "3.4" }, notices: ["Migrated into a new revision"] });
    if (url === "/api/v1/workbench/workspace/initialize") return response({ directory: "project" });
    if (url === "/api/v1/workbench/project/defaults") return response(fixture.config);
    if (url === "/api/v1/workbench/environment/defaults") return response(fixture.config.environment);
    if (url === "/api/v1/workbench/fleet/defaults") return response(fixture.fleet);
    if (url === "/api/v1/workbench/run-options") return response({ workers: 2, memory_limit_bytes: 4294967296, job_timeout_s: 7200 });
    if (url.startsWith("/api/v1/workbench/runs?")) return response([fixture.run]);
    if (url.startsWith("/api/v1/workbench/deleted-runs?") || url.startsWith("/api/v1/workbench/analyses?") || url.startsWith("/api/v1/jobs?") || url.endsWith("/revisions") || url === "/api/v1/inputs") return response([]);
    if (url === "/api/v1/workbench/utility-curve") return response(fixture.curve);
    if (url === "/api/v1/workbench/runs") return response({ job_id: "run-job", resource_id: "run-resource", status_url: "/api/v1/jobs/run-job", events_url: "/api/v1/jobs/run-job/events", cache_hit: false }, 202);
    throw new Error(`Unexpected request ${init?.method ?? "GET"} ${url}`);
  });
}
function renderRoute(route: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={queryClient}><MemoryRouter initialEntries={[route]}><WorkspaceProvider><App /></WorkspaceProvider></MemoryRouter></QueryClientProvider>);
}

describe("MR05 public workbench", () => {
  beforeEach(() => { localStorage.clear(); vi.restoreAllMocks(); });
  it("requires named confirmation and keeps cancellation free of delete requests", async () => {
    const fetcher = installFetch(); const user = userEvent.setup(); renderRoute("/project");
    await user.click(await screen.findByRole("button", { name: /^Delete$/ }));
    const dialog = screen.getByRole("dialog", { name: "Delete project?" });
    expect(within(dialog).getByText(project.name)).toBeInTheDocument();
    expect(within(dialog).getByText(/workspace trash/)).toBeInTheDocument();
    expect(fetcher.mock.calls.some(([, init]) => init?.method === "DELETE")).toBe(false);
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(fetcher.mock.calls.some(([, init]) => init?.method === "DELETE")).toBe(false);
    await user.click(screen.getByRole("button", { name: /^Delete$/ }));
    await user.click(screen.getByRole("button", { name: /^Delete project$/ }));
    await waitFor(() => expect(fetcher.mock.calls.filter(([, init]) => init?.method === "DELETE")).toHaveLength(1));
  });
  it("labels every help step with its navigation name", async () => {
    installFetch(); const user = userEvent.setup(); renderRoute("/project");
    await user.click(await screen.findByRole("button", { name: "Help" }));
    const dialog = screen.getByRole("dialog", { name: "Scientific workflow" });
    const steps = within(dialog).getAllByRole("listitem");
    expect(steps).toHaveLength(7);
    ["Project", "Data", "Environment", "Fleet Configuration", "Simulation", "Portfolio", "Results"].forEach((name, index) => expect(steps[index].textContent).toMatch(new RegExp(`^${name}:`)));
    expect(within(dialog).getByText(/Fleet results/)).toBeInTheDocument();
  });
  it("rejects duplicate project names before submission and opens the file inventory", async () => {
    const fetcher=installFetch(true,{"/api/v1/projects/project-1/files":{project_id:"project-1",name:project.name,directory:"/workspace/Acceptance project",portable:false,files:[{file_id:"file1",name:"Population source.csv",category:"Inputs / Global",path:"../inputs/input1/source.csv",size_bytes:1024,shared:true,source_id:"input1"}]}});
    const user=userEvent.setup();renderRoute("/project");
    await screen.findByText(project.name,{selector:"h6"});
    await user.click(screen.getByRole("button",{name:"New project"}));
    await user.type(screen.getByLabelText(/Project name/),"acceptance project");
    expect(screen.getByRole("button",{name:"Create"})).toBeDisabled();
    expect(screen.getByText("A project with this name already exists.")).toBeInTheDocument();
    await user.click(screen.getByRole("button",{name:"Cancel"}));
    await user.click(await screen.findByRole("button",{name:"Files"}));
    expect(await screen.findByRole("link",{name:"Population source.csv"})).toHaveAttribute("href","/api/v1/projects/project-1/files/file1");
    expect(screen.getByRole("button",{name:"Export complete project"})).toBeInTheDocument();
    expect(fetcher.mock.calls.some(([url,init])=>String(url)==="/api/v1/projects" && init?.method==="POST")).toBe(false);
  });
  it("separates simulation replications from fleet sampling and never runs on edits", async () => {
    const fetchMock = installFetch(); const user = userEvent.setup(); renderRoute("/simulation");
    const replications = await screen.findByLabelText("Simulation replications");
    await user.clear(replications); await user.type(replications, "3");
    expect(screen.getByLabelText("Simulation reporting interval (minutes)")).toBeInTheDocument();
    expect(screen.getByLabelText("Simulation worker processes")).toHaveValue(2);
    expect(fetchMock.mock.calls.some(([url, init]) => String(url) === "/api/v1/workbench/runs" && init?.method === "POST")).toBe(false);
    await user.click(screen.getByRole("link", { name: /^Portfolio$/ }));
    expect(await screen.findByLabelText("Fleet sampling runs")).toBeInTheDocument();
    expect(screen.queryByLabelText("Simulation replications")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Budgets (abstract cost units)")).toBeInTheDocument();
    expect(screen.queryByText(/minor-unit scale/i)).not.toBeInTheDocument();
  });
  it("submits the complete edited version-three configuration in one action", async () => {
    const fetchMock = installFetch(); const user = userEvent.setup(); renderRoute("/simulation");
    const interval = await screen.findByLabelText("Simulation reporting interval (minutes)");
    await user.clear(interval); await user.type(interval, "30");
    await user.click(screen.getByRole("button", { name: "Run simulation" }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url) === "/api/v1/workbench/runs")).toBe(true));
    const call = fetchMock.mock.calls.find(([url]) => String(url) === "/api/v1/workbench/runs")!;
    const body = JSON.parse(String(call[1]?.body));
    expect(body.config.schema_version).toBe("3.4");
    expect(body.config.simulation.temporal_resolution_minutes).toBe(30);
    expect(body.options.workers).toBe(2);
    expect(body.config.fleets).toEqual(fixture.config.fleets);
    expect(screen.queryByRole("button", { name: "Validate scenario" })).not.toBeInTheDocument();
  });
  it("supports demand/OD registration on Data while preserving feature and fleet-only vehicle categories", async () => {
    installFetch(); const user = userEvent.setup(); renderRoute("/data");
    await user.click(await screen.findByLabelText("Data category"));
    expect(screen.getByRole("option", { name: "feature" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Demand / OD distribution" })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "supply" })).not.toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "assignment" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("option", { name: "Demand / OD distribution" }));
    expect(screen.getByText(/origin_cell_id, destination_cell_id and weight/)).toBeInTheDocument();
    expect(screen.getByText(/registering a file does not activate it/)).toBeInTheDocument();
    await user.click(screen.getByRole("combobox", { name: "Data category" }));
    await user.click(screen.getByRole("option", { name: "feature" }));
    expect(screen.queryByLabelText("Feature contents")).not.toBeInTheDocument();
    expect(screen.getByText(/Population, activity and custom weights are all features/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Register input" })).toBeDisabled();
  });
  it("shows a saved demand-role OD in Data and Fleet, retains explicit selection after reload, and submits it only on Run", async () => {
    const configuration = odConfiguration(empiricalOd.input_id);
    const fetchMock = installFetch(true, {
      [`/api/v1/projects/${project.project_id}/revisions/revision-1`]: { revision_id: "revision-1", payload: configuration },
      "/api/v1/inputs": [empiricalOd, weekendOd],
    });
    const user = userEvent.setup();
    const view = renderRoute("/data");
    const inputRow = (await screen.findByRole("button", { name: empiricalOd.name })).closest("tr")!;
    expect(within(inputRow).getByText("OD distribution")).toBeInTheDocument();
    await user.click(within(screen.getByRole("list", { name: "Main navigation" })).getByRole("link", { name: "Fleet Configuration" }));
    expect(await screen.findByRole("combobox", { name: "Demand spatial source" })).toHaveTextContent(empiricalOd.name);
    expect(screen.getByText(empiricalOd.original_filename)).toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Spatial feature" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("combobox", { name: "Demand spatial source" }));
    await user.click(screen.getByRole("option", { name: "Independent origin and destination distributions" }));
    await waitFor(() => expect(storedDemand().od_distribution_input).toBeNull());
    await user.click(screen.getByRole("combobox", { name: "Demand spatial source" }));
    await user.click(screen.getByRole("option", { name: new RegExp(weekendOd.name) }));
    await waitFor(() => expect(storedDemand().od_distribution_input).toBe(weekendOd.input_id));
    expect(fetchMock.mock.calls.some(([url, init]) => String(url) === "/api/v1/workbench/runs" && init?.method === "POST")).toBe(false);
    view.unmount();
    renderRoute("/fleets");
    expect(await screen.findByRole("combobox", { name: "Demand spatial source" })).toHaveTextContent(weekendOd.name);
    expect(screen.queryByRole("combobox", { name: "Spatial feature" })).not.toBeInTheDocument();
    await user.click(within(screen.getByRole("list", { name: "Main navigation" })).getByRole("link", { name: "Simulation" }));
    const runButton = await screen.findByRole("button", { name: "Run simulation" });
    expect(fetchMock.mock.calls.some(([url, init]) => String(url) === "/api/v1/workbench/runs" && init?.method === "POST")).toBe(false);
    await user.click(runButton);
    await waitFor(() => expect(fetchMock.mock.calls.filter(([url, init]) => String(url) === "/api/v1/workbench/runs" && init?.method === "POST")).toHaveLength(1));
    const request = fetchMock.mock.calls.find(([url, init]) => String(url) === "/api/v1/workbench/runs" && init?.method === "POST")!;
    const payload = JSON.parse(String(request[1]?.body));
    expect(payload.config.fleets[0].demand.od_distribution_input).toBe(weekendOd.input_id);
    expect(payload.config.fleets[0].demand.task_type).toBe("od");
    expect(payload.config.fleets[0].demand.source).toBe("generator");
  });
  it("uploads an OD table as demand without CRS and does not bind registration to a fleet", async () => {
    const overrides: Record<string, unknown> = {
      [`/api/v1/projects/${project.project_id}/revisions/revision-1`]: { revision_id: "revision-1", payload: odConfiguration() },
      "/api/v1/inputs": [], "/api/v1/inputs/upload": empiricalOd,
    };
    const fetchMock = installFetch(true, overrides);
    const user = userEvent.setup();
    const view = renderRoute("/data");
    await user.click(await screen.findByRole("combobox", { name: "Data category" }));
    await user.click(screen.getByRole("option", { name: "Demand / OD distribution" }));
    const file = new File(["origin_cell_id,destination_cell_id,weight\ncell-a,cell-b,3\n"], empiricalOd.original_filename, { type: "text/csv" });
    await user.upload(view.container.querySelector<HTMLInputElement>('input[type="file"]')!, file);
    overrides["/api/v1/inputs"] = [empiricalOd];
    await user.click(screen.getByRole("button", { name: "Register input" }));
    await screen.findByText(`Input preview: ${empiricalOd.name}`);
    const upload = fetchMock.mock.calls.find(([url]) => String(url) === "/api/v1/inputs/upload")!;
    const form = upload[1]?.body as FormData;
    expect(form.get("role")).toBe("demand");
    expect(form.get("source_crs")).toBe("");
    expect(form.get("file")).toBe(file);
    expect(screen.queryByRole("button", { name: "Confirm and register coordinate interpretation" })).not.toBeInTheDocument();
    await user.click(within(screen.getByRole("list", { name: "Main navigation" })).getByRole("link", { name: "Fleet Configuration" }));
    expect(await screen.findByRole("combobox", { name: "Demand spatial source" })).toHaveTextContent("Independent origin and destination distributions");
    expect(screen.getAllByRole("combobox", { name: "Spatial feature" })).toHaveLength(2);
    await user.click(screen.getByRole("combobox", { name: "Demand spatial source" }));
    expect(screen.getByRole("option", { name: new RegExp(empiricalOd.name) })).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(storedDemand()?.od_distribution_input ?? null).toBeNull();
    expect(fetchMock.mock.calls.some(([url, init]) => String(url) === "/api/v1/workbench/runs" && init?.method === "POST")).toBe(false);
  });
  it.each([
    ["Task type", "Location"],
    ["Demand source", "Import tasks or demand table"],
  ])("clears a stale OD reference when %s changes away from generated OD demand", async (field, option) => {
    const fetchMock = installFetch(true, {
      [`/api/v1/projects/${project.project_id}/revisions/revision-1`]: { revision_id: "revision-1", payload: odConfiguration(empiricalOd.input_id) },
      "/api/v1/inputs": [empiricalOd],
    });
    const user = userEvent.setup(); renderRoute("/fleets");
    expect(await screen.findByRole("combobox", { name: "Demand spatial source" })).toHaveTextContent(empiricalOd.name);
    await user.click(screen.getByRole("combobox", { name: field }));
    await user.click(screen.getByRole("option", { name: option }));
    await waitFor(() => expect(storedDemand().od_distribution_input).toBeNull());
    expect(screen.queryByRole("combobox", { name: "Demand spatial source" })).not.toBeInTheDocument();
    if (field === "Demand source") {
      expect(storedDemand().source).toBe("import");
      await user.click(screen.getByRole("combobox", { name: "Demand source" }));
      await user.click(screen.getByRole("option", { name: "Generate tasks" }));
    }
    await user.click(screen.getByRole("combobox", { name: "Task type" }));
    await user.click(screen.getByRole("option", { name: "Origin–destination" }));
    expect(screen.getByRole("combobox", { name: "Demand spatial source" })).toHaveTextContent("Independent origin and destination distributions");
    expect(storedDemand().od_distribution_input).toBeNull();
    expect(fetchMock.mock.calls.some(([url, init]) => String(url) === "/api/v1/workbench/runs" && init?.method === "POST")).toBe(false);
  });
  it("does not label an unsaved project as saved", async () => {
    installFetch(false); renderRoute("/project");
    expect(await screen.findByText("Not saved")).toBeInTheDocument();
    expect(screen.queryByText("Configuration saved")).not.toBeInTheDocument();
  });
  it("binds the typed environment result without worker envelope metadata", async () => {
    const prepared = fixture.config.prepared_environment;
    const empty = { type: "FeatureCollection", crs: "EPSG:4326", features: [] };
    const fetchMock = installFetch(true, {
      "/api/v1/workbench/environments": { job_id: "env-job", resource_id: "env-resource", status_url: "/api/v1/jobs/env-job", events_url: "/api/v1/jobs/env-job/events", cache_hit: false },
      "/api/v1/jobs/env-job": { job_id: "env-job", resource_id: "env-resource", status: "completed", kind: "studio_environment", phase: "completed", counters: {}, attempt: 1, result: { ...prepared, dependencies: [], resource_id: "env-resource" } },
      "/api/v1/workbench/environments/env-resource/result": prepared,
      [`/api/v1/workbench/environments/${prepared.artifact.artifact_id}/map`]: { roads: empty, boundary: empty, road_count: 0, preview_road_count: 0, cell_count: 0, working_crs: "EPSG:2056" },
    });
    const user = userEvent.setup(); renderRoute("/environment");
    await user.click(await screen.findByRole("button", { name: "Prepare environment" }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("env-resource/result"))).toBe(true));
    await user.click(within(screen.getByRole("list", { name: "Main navigation" })).getByRole("link", { name: "Simulation" }));
    await user.click(await screen.findByRole("button", { name: "Run simulation" }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url) === "/api/v1/workbench/runs")).toBe(true));
    const call = fetchMock.mock.calls.find(([url]) => String(url) === "/api/v1/workbench/runs")!;
    expect(JSON.parse(String(call[1]?.body)).config.prepared_environment).toEqual(prepared);
  });
  it("persists mixed grid-filter rows into Run while Portfolio describes its saved source filter", async () => {
    const configuration = structuredClone(fixture.config) as unknown as ProjectConfig;
    const prepared = { ...configuration.prepared_environment!, feature_names: ["uniform", "population", "shops"] };
    configuration.prepared_environment = prepared;
    configuration.grid_filter = { features: ["population"], match: "any" };
    const empty = { type: "FeatureCollection", crs: "EPSG:4326", features: [] };
    const source = { ...fixture.run, config: { ...fixture.run.config, prepared_environment: prepared,
      grid_filter: { features: ["shops"], match: "all", require_road_intersection: true } } };
    const fetchMock = installFetch(true, {
      [`/api/v1/projects/${project.project_id}/revisions/revision-1`]: { revision_id: "revision-1", payload: configuration },
      [`/api/v1/workbench/environments/${prepared.artifact.artifact_id}/map`]: { roads: empty, boundary: empty, road_count: 0, preview_road_count: 0, cell_count: 10, working_crs: "EPSG:2056" },
      [`/api/v1/workbench/features/${prepared.features.artifact_id}/map?feature=population`]: { ...empty, unit: "people", zero_cell_count: 0, returned_count: 0, sources: [] },
      "/api/v1/workbench/grid-filter/preview": { total_cell_count: 10, retained_cell_count: 7, excluded_cell_count: 3 },
      [`/api/v1/workbench/runs?project_id=${project.project_id}`]: [source],
    });
    const user = userEvent.setup();
    const view = renderRoute("/environment");
    expect(await screen.findByRole("combobox", { name: "Filter feature 1" })).toHaveTextContent("population");
    await screen.findByText(/7 \/ 10 cells retained/);
    const initialPreview = fetchMock.mock.calls.find(([url]) => String(url) === "/api/v1/workbench/grid-filter/preview")!;
    expect(JSON.parse(String(initialPreview[1]?.body)).grid_filter).toEqual(configuration.grid_filter);
    await user.click(screen.getByRole("button", { name: "Add filter row" }));
    await user.click(screen.getByRole("button", { name: "Add filter row" }));
    expect(screen.getByRole("combobox", { name: "Filter feature 3" })).toHaveTextContent("Road network");
    await user.click(screen.getByRole("combobox", { name: "Filter logic 1" }));
    await user.click(screen.getByRole("option", { name: "Must include" }));
    const expected = { features: [], match: "any", require_road_intersection: false, rules: [
      { kind: "spatial_feature", feature: "population", logic: "must" },
      { kind: "spatial_feature", feature: "shops", logic: "any" },
      { kind: "road_network", feature: null, logic: "any" },
    ] };
    await waitFor(() => expect(JSON.parse(localStorage.getItem(`mobile-sensing-draft:${project.project_id}`)!).draft.authoring.grid_filter).toEqual(expected));
    expect(fetchMock.mock.calls.some(([url, init]) => ["/api/v1/workbench/environments", "/api/v1/workbench/runs"].includes(String(url)) && init?.method === "POST")).toBe(false);
    view.unmount();
    renderRoute("/simulation");
    expect(await screen.findByText(/Grid filter: Must include \(population > 0\) AND Any of \(shops > 0 OR Road network\)/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Run simulation" }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([url, init]) => String(url) === "/api/v1/workbench/runs" && init?.method === "POST")).toBe(true));
    const runRequest = fetchMock.mock.calls.find(([url, init]) => String(url) === "/api/v1/workbench/runs" && init?.method === "POST")!;
    expect(JSON.parse(String(runRequest[1]?.body)).config.grid_filter).toEqual(expected);
    await user.click(within(screen.getByRole("list", { name: "Main navigation" })).getByRole("link", { name: "Portfolio" }));
    expect(await screen.findByText(/Source grid filter: Must include \(shops > 0 AND Road network\)/)).toBeInTheDocument();
    expect(screen.queryByText(/Source grid filter:.*population/)).not.toBeInTheDocument();
  });
  it("persists Supply feature selection separately from Demand and limits idle choices", async () => {
    const configuration = structuredClone(fixture.config);
    configuration.schema_version = "3.4";
    configuration.fleets[0].supply.initial_location = "spatial_feature";
    configuration.fleets[0].supply.source = "generated";
    configuration.prepared_environment.feature_names = ["uniform", "population"];
    const overrides = { [`/api/v1/projects/${project.project_id}/revisions/revision-1`]: { revision_id: "revision-1", payload: configuration } };
    installFetch(true, overrides);
    const user = userEvent.setup();
    const view = renderRoute("/fleets");
    await user.click(await screen.findByRole("tab", { name: "supply" }));
    const feature = screen.getByRole("combobox", { name: "Spatial feature" });
    expect(feature).not.toHaveAttribute("aria-disabled", "true");
    await user.click(feature);
    await user.click(screen.getByRole("option", { name: "population" }));
    const stored = () => JSON.parse(localStorage.getItem(`mobile-sensing-draft:${project.project_id}`)!);
    await waitFor(() => expect(stored().draft.authoring.fleets[0].supply.spatial_weights).toEqual([{ feature: "population", weight: 1 }]));
    expect(stored().draft.authoring.fleets[0].demand).toEqual(configuration.fleets[0].demand);
    await user.click(screen.getByRole("combobox", { name: "Post-service behavior" }));
    expect(screen.getAllByRole("option")).toHaveLength(2);
    expect(screen.getByRole("option", { name: /Idling.*wait at the last location/ })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /Cruising.*move randomly on the road network/ })).toBeInTheDocument();
    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("button", { name: "Depot, capacity and service areas" }));
    await user.click(screen.getByRole("combobox", { name: "Service areas" }));
    await user.click(screen.getByRole("option", { name: "Auto · balance expected demand" }));
    const areaCount = screen.getByRole("spinbutton", { name: "Number of auto service areas" });
    await user.clear(areaCount); await user.type(areaCount, "3");
    await waitFor(() => expect(stored().draft.authoring.fleets[0].supply.service_area_mode).toBe("auto"));
    expect(stored().draft.authoring.fleets[0].supply.auto_service_area_count).toBe(3);
    view.unmount();
    renderRoute("/fleets");
    await user.click(await screen.findByRole("tab", { name: "supply" }));
    expect(screen.getByRole("combobox", { name: "Spatial feature" })).toHaveTextContent("population");
  });

  it("keeps Supply activation and feature inputs consistently read-only on reference projects", async () => {
    const configuration = structuredClone(fixture.config);
    installFetch(true, { [`/api/v1/projects/${project.project_id}/revisions/revision-1`]: { revision_id: "revision-1", payload: { ...configuration, schema_version: "3.4", read_only: true } } });
    const user = userEvent.setup(); renderRoute("/fleets");
    await user.click(await screen.findByRole("tab", { name: "supply" }));
    expect(screen.getByRole("combobox", { name: "Activation" })).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByRole("combobox", { name: "Spatial feature" })).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByRole("spinbutton", { name: "Fixed work duration per vehicle (hours)" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Create editable copy" })).toBeEnabled();
  });

  it("replaces a searched region in the single map on the right", async () => {
    const empty = { type: "FeatureCollection", features: [], crs: "EPSG:4326" };
    const receipt = (query: string) => ({ query, name: `${query} region`, source: "OSM test fixture", boundary: { ...empty, features: [{ properties: { name: query } }] } });
    const overrides: Record<string, unknown> = {
      [`/api/v1/workbench/environments/${fixture.config.prepared_environment.artifact.artifact_id}/map`]: { roads: empty, boundary: empty, road_count: 0, preview_road_count: 0, cell_count: 0, working_crs: "EPSG:2056" },
      "/api/v1/workbench/region-search": { job_id: "search-a", resource_id: "region-a" },
      "/api/v1/jobs/search-a": { job_id: "search-a", resource_id: "region-a", kind: "region_search", status: "completed", phase: "completed", counters: {}, attempt: 1 },
      "/api/v1/workbench/region-search/region-a": receipt("Lausanne"),
      "/api/v1/jobs/search-b": { job_id: "search-b", resource_id: "region-b", kind: "region_search", status: "completed", phase: "completed", counters: {}, attempt: 1 },
      "/api/v1/workbench/region-search/region-b": receipt("Morges"),
    };
    installFetch(true, overrides);
    const user = userEvent.setup(); renderRoute("/environment");
    await user.click(await screen.findByRole("combobox", { name: "Boundary source" }));
    expect(screen.getAllByRole("option")).toHaveLength(2);
    expect(screen.queryByRole("option", { name: /Draw/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole("option", { name: "Search region (OSM)" }));
    fireEvent.change(screen.getByLabelText("Region search"), { target: { value: "Lausanne" } });
    await user.click(screen.getByRole("button", { name: "Search" }));
    await screen.findByText("Lausanne region");
    expect(screen.getAllByTestId("scientific-map")).toHaveLength(2);
    const regionMap = screen.getAllByTestId("scientific-map").find((element) => element.getAttribute("data-fit-key") === "search:Lausanne");
    expect(regionMap?.closest(".environment-preview")).not.toBeNull();
    overrides["/api/v1/workbench/region-search"] = { job_id: "search-b", resource_id: "region-b" };
    fireEvent.change(screen.getByLabelText("Region search"), { target: { value: "Morges" } });
    expect(screen.queryByText("Lausanne region")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Search" }));
    await screen.findByText("Morges region");
    expect(screen.getAllByTestId("scientific-map")).toHaveLength(2);
    const replacedRegionMap = screen.getAllByTestId("scientific-map").find((element) => element.getAttribute("data-fit-key") === "search:Morges");
    expect(replacedRegionMap).toHaveTextContent("Morges");
  });

});
