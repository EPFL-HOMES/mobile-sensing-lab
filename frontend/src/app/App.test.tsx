import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import fixture from "../test/studioFixture.json";
import { WorkspaceProvider } from "./workspace";
import { App } from "./App";

vi.mock("../components/ScientificMap", () => ({ ScientificMap: ({ data, fitKey }: { data: unknown; fitKey?: string }) => <div data-testid="scientific-map" data-fit-key={fitKey}>{JSON.stringify(data)}</div> }));

const project = { project_id: "project-1", name: "Acceptance project", description: "MR05", current_revision_id: "revision-1", created_at_utc: "2026-09-13T00:00:00Z", updated_at_utc: "2026-09-13T00:00:00Z" };
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
  it("keeps global registration free of task and vehicle categories", async () => {
    installFetch(); const user = userEvent.setup(); renderRoute("/data");
    await user.click(await screen.findByLabelText("Data category"));
    expect(screen.getByRole("option", { name: "feature" })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "demand" })).not.toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "supply" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("option", { name: "feature" }));
    expect(screen.queryByLabelText("Feature contents")).not.toBeInTheDocument();
    expect(screen.getByText(/Population, activity and custom weights are all features/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Register input" })).toBeDisabled();
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
