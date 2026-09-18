import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { JobSnapshot } from "../../api/client";
import { WorkspaceProvider } from "../../app/workspace";
import { App } from "../../app/App";
import { JobMonitor, jobPhaseLabel } from "../../components/JobMonitor";
import fixture from "../../test/studioFixture.json";

const project = { project_id: "project-1", name: "M11 project", description: "results", current_revision_id: "revision-1", created_at_utc: "2026-09-09T00:00:00Z", updated_at_utc: "2026-09-09T00:00:00Z" };
const revision = { project_id: "project-1", revision_id: "revision-1", base_revision_id: null, payload: { ...fixture.config, schema_version: "3.4" }, created_at_utc: "2026-09-09T00:00:00Z" };

function response(value: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } }));
}

function job(id: string, artifactId: string, kind: "simulation" | "exposure" | "portfolio", stage?: "samples" | "analysis"): JobSnapshot {
  return { job_id: id, kind, project_id: project.project_id, resource_id: artifactId, request_hash: "a".repeat(64), status: "completed", phase: "completed", attempt: 1, cancel_requested: false, counters: {}, result: { artifact: { artifact_id: artifactId, artifact_kind: kind, content_hash: "b".repeat(64) }, ...(stage ? { portfolio_stage: stage } : {}) }, error_code: null, error_message: null, cache_source_job_id: null, created_at_utc: project.created_at_utc, updated_at_utc: project.updated_at_utc };
}

function manifest(id: string, kind: "simulation" | "exposure" | "portfolio", dependencies: Array<{ role: string; artifact_id: string }> = []) {
  return { artifact_id: id, artifact_kind: kind, content_fingerprint: "c".repeat(64), dependencies: dependencies.map((item) => ({ ...item, content_hash: "d".repeat(64) })), expected_table_names: [], complete: true, scientific_identity: { resolved_config: {}, algorithm_versions: {} } };
}

function page(items: unknown[]) { return { items, returned_count: items.length, total_matching_count: items.length, next_cursor: null, is_complete: true }; }

function installBase(fetcher: (url: string, init?: RequestInit) => Promise<Response>) {
  localStorage.setItem("mobile-sensing-workbench@1", JSON.stringify({ activeProjectId: project.project_id, trackedJobIds: [], jobRevisionIds: { "job-exposure": "revision-1", "job-analysis": "revision-1" } }));
  return vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
    const url = String(input);
    if (url.startsWith("/api/v1/workbench/runs?") || url.startsWith("/api/v1/workbench/analyses?")) return response([]);
    if (url === "/api/v1/projects") return response([project]);
    if (url === `/api/v1/projects/${project.project_id}`) return response(project);
    if (url.endsWith("/revisions/revision-1")) return response(revision);
    return fetcher(url, init);
  });
}

function renderApp(route: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[route]}><WorkspaceProvider><App /></WorkspaceProvider></MemoryRouter></QueryClientProvider>);
}

describe("M11 result workspaces", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.restoreAllMocks();
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(null);
  });

  it.each([
    ["environment.network.osm.tile_3_of_15.server_1_of_3.overpass-api_de", "Step 4 of 8 · Download road tile 3 of 15 (overpass-api.de)"],
    ["environment.network.osm", "Step 4 of 8 · Download road network from OpenStreetMap"],
    ["environment.network.osm.server_1_of_3.overpass-api_de", "Step 4 of 8 · Download road network · server 1 of 3 (overpass-api.de)"],
    ["environment.prepare.prepare_network", "Step 6 of 8 · Build the directed routing network"],
    ["environment.features.osm.public_services", "Step 7 of 8 · Download and aggregate OSM public services"],
    ["environment.features.osm.batch.server_1_of_3.overpass-api_de", "Step 7 of 8 · Download OSM selected spatial features · server 1 of 3 (overpass-api.de)"],
    ["environment.features.osm.batch.tile_2_of_5.server_1_of_3.overpass-api_de", "Step 7 of 8 · Download OSM feature tile 2 of 5 (overpass-api.de)"],
    ["environment.features.osm.batch.subdivide", "Step 7 of 8 · Feature query timed out; retry smaller tiles"],
    ["environment.publish_features", "Step 8 of 8 · Publish spatial features and provenance"],
  ])("describes environment progress phase %s", (phase, label) => {
    expect(jobPhaseLabel("studio_environment", phase)).toBe(label);
  });

  it("shows mean-only fleet results including inactive vehicles and falls back without WebGL", async () => {
    const exposureJob = job("job-exposure", "exposure-1", "exposure");
    installBase((url, init) => {
      if (url.startsWith("/api/v1/jobs?")) return response([exposureJob]);
      if (url === "/api/v1/exposures/exposure-1") return response(manifest("exposure-1", "exposure", [{ role: "environment", artifact_id: "environment-1" }]));
      if (url.startsWith("/api/v1/results/exposure-1/replication_status")) return response(page([{ replication_id: "r1", complete: true }]));
      if (url.startsWith("/api/v1/results/exposure-1/vehicle_catalog")) return response(page([{ fleet_id: "fleet", vehicle_id: "active", catalog_index: 0, catalog_hash: "hash" }, { fleet_id: "fleet", vehicle_id: "inactive", catalog_index: 1, catalog_hash: "hash" }]));
      if (url.startsWith("/api/v1/results/exposure-1/time_bins")) return response(page([{ time_bin_id: "bin-2", canonical_index: 1, start_s: 60, end_s: 120 },{ time_bin_id: "bin-1", canonical_index: 0, start_s: 0, end_s: 60 }]));
      if (url === "/api/v1/maps/environment-1/grid" || url === "/api/v1/maps/environment-1/boundary") return response({ type: "FeatureCollection", features: [{ type: "Feature", geometry: { type: "Polygon", coordinates: [[[6, 46], [6.1, 46], [6.1, 46.1], [6, 46.1], [6, 46]]] }, properties: { cell_id: "cell-1" } }], returned_count: 1, total_matching_count: 1, is_complete: true, crs: "EPSG:4326", aggregation: null });
      if (url === "/api/v1/matrix-queries" && init?.method === "POST") {
        const body = JSON.parse(String(init.body)) as { vehicle_keys: Array<Record<string, unknown>>; statistic: string; replication_ids?: string[] };
        expect(body.vehicle_keys).toEqual([{ fleet_id: "fleet", vehicle_id: "inactive" }]);
        expect(body.statistic).toBe("mean");
        expect(body.replication_ids).toBeUndefined();
        return response({ resource_id: "exposure-1", kind: "vehicle_exposure", statistic: "mean", mean_coverage_fraction: 0, coverage_denominator_cell_count: 1, coverage_semantics: "mean_within_observation_any_time_spatial_coverage_road_intersecting_cells", replication_ids: [], vehicle_keys: body.vehicle_keys, cell_ids: ["cell-1"], time_bin_ids: ["bin-1"], expected_shape: [1, 1], values: [], unit: "s", zero_fill: "absent_sparse_rows_are_zero", complete: true, replications_R: 1, sampling_rounds_J: null, time_summary: [{ replication_or_round_id: "r1", time_bin_id: "bin-1", value: 0 }], overall_value: 0, positive_cell_count: 0, summary_semantics: "statistic_of_within_observation_cell_sum" });
      }
      throw new Error(`Unexpected request ${init?.method ?? "GET"} ${url}`);
    });
    renderApp("/results?view=sensing&resource=exposure-1&scope=vehicle&fleet=fleet&vehicle=inactive&replication=r1&time_bin=bin-1");
    // The first results route loads lazily and resolves several dependent queries.
    // Allow cold CI workers to finish that work without relaxing the assertion.
    expect(await screen.findByText("Mean of 1 runs", {}, { timeout: 4000 })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Fleet results" })).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "Operations" })).not.toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "Sensing" })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Simulation replication")).not.toBeInTheDocument();
    expect(screen.getByText("Selected interval: 00:00–00:02")).toBeInTheDocument();
    expect(screen.getByText("0.00%")).toBeInTheDocument();
    expect(screen.getByText(/1 road-intersecting grids/)).toBeInTheDocument();
    expect(screen.getByText(/Only positive-length road intersections enter the denominator/)).toBeInTheDocument();
    expect(await screen.findByText(/WebGL is unavailable/)).toBeInTheDocument();
  });

  it.each(["std", "p05"] as const)("selects coincident portfolios for %s with five-decimal statistics and only the mean map", async (metric) => {
    const analysisJob = job("job-analysis", "analysis-1", "portfolio", "analysis");
    const budget = { budget_id: "budget-1", budget_minor: 10, cost_unit: "CHF_minor", minor_unit_scale: 100, replications_R: 2, sampling_rounds_J: 4, feasible_portfolio_count: 2, frontier_portfolio_count: 2, frontier_enabled: true, disabled_reason: null };
    const points = ["p-tie-1", "p-tie-2"].map((portfolio_id, index) => ({ portfolio_id, count_by_fleet: { fleet: index + 1 }, cost_by_fleet: { fleet: (index + 1) / 100 }, total_cost: (index + 1) / 100, total_cost_minor: index + 1, unspent_minor: 0, nondominated: true, tie_group_id: "tie-1", utility_mean: 0.5123456789, utility_sample_variance: 0, utility_sample_std: 0, conditional_mean_se: 0, utility_min: 0.5, utility_max: 0.5, utility_p05: 0.5, utility_p50: 0.5, utility_p95: 0.5, sample_count: 4 }));
    const fetchMock = installBase((url, init) => {
      if (url.startsWith("/api/v1/jobs?")) return response([analysisJob]);
      if (url === "/api/v1/portfolio-analyses/analysis-1") return response(manifest("analysis-1", "portfolio", [{ role: "portfolio_samples", artifact_id: "samples-1" }]));
      if (url.startsWith("/api/v1/results/analysis-1/budget_levels")) return response(page([budget]));
      if (url === "/api/v1/portfolio-samples/samples-1") return response(manifest("samples-1", "portfolio", [{ role: "exposure", artifact_id: "exposure-1" }]));
      if (url === "/api/v1/exposures/exposure-1") return response(manifest("exposure-1", "exposure", [{ role: "environment", artifact_id: "environment-1" }]));
      if (url.startsWith("/api/v1/results/exposure-1/time_bins")) return response(page([{ time_bin_id: "bin-1", canonical_index: 0, start_s: 0, end_s: 60 }]));
      if (url === "/api/v1/maps/environment-1/grid" || url === "/api/v1/maps/environment-1/boundary") return response({ type: "FeatureCollection", features: [{ type: "Feature", geometry: { type: "Polygon", coordinates: [[[6, 46], [6.1, 46], [6.1, 46.1], [6, 46.1], [6, 46]]] }, properties: { cell_id: "cell-1" } }], returned_count: 1, total_matching_count: 1, is_complete: true, crs: "EPSG:4326", aggregation: null });
      if (url.startsWith("/api/v1/portfolio-frontiers/analysis-1")) return response({ analysis_id: "analysis-1", risk_metric: metric, budget, replications_R: 2, sampling_rounds_J: 4, variability_interpretation: "combined_empirical_operational_and_allocation_variability", inference_scope: "conditional_empirical", points, returned_count: 2, is_complete: true });
      if (url === "/api/v1/matrix-queries" && init?.method === "POST") {
        const body = JSON.parse(String(init.body)) as { kind: string; statistic: string };
        expect(body.kind).toBe("portfolio_summary");
        expect(body.statistic).toBe("mean");
        return response({ resource_id: body.kind === "portfolio_sample" ? "samples-1" : "analysis-1", kind: body.kind, statistic: body.kind === "portfolio_sample" ? "realization" : "mean", cell_ids: ["cell-1"], time_bin_ids: ["bin-1"], expected_shape: [1, 1], values: [{ cell_id: "cell-1", time_bin_id: "bin-1", value: 300 }], unit: "s", zero_fill: "absent_sparse_rows_are_zero", complete: true, replications_R: 2, sampling_rounds_J: 4, portfolio_id: "p-tie-1", round_id: body.kind === "portfolio_sample" ? 0 : null, time_summary: [{ time_bin_id: "bin-1", value: 300 }], overall_value: 300, positive_cell_count: 1, mean_coverage_fraction: 0.25, summary_semantics: "statistic_of_within_observation_cell_sum" });
      }
      throw new Error(`Unexpected request ${init?.method ?? "GET"} ${url}`);
    });
    renderApp("/results?view=portfolio&resource=analysis-1&portfolio=p-tie-1&round=0&time_bin=bin-1");
    expect(await screen.findByText("Simulation replications: 2")).toBeInTheDocument();
    expect(screen.getByText("Fleet sampling runs: 4")).toBeInTheDocument();
    expect(screen.getAllByText(metric === "p05" ? /Mean and P05 are both maximized/ : /standard deviation is minimized/).length).toBeGreaterThan(0);
    expect(screen.getByRole("checkbox", { name: "Show nondominated points only" })).toBeChecked();
    expect(screen.getAllByText("0.1 CHF_minor").length).toBeGreaterThan(0);
    expect(screen.getAllByText("0.51235").length).toBeGreaterThan(0);
    expect(screen.getByLabelText("Fleet cost 0.01000 CHF_minor")).toBeInTheDocument();
    fireEvent.mouseDown(screen.getByRole("combobox", { name: "Equipped vehicles by fleet" }));
    fireEvent.click(await screen.findByRole("option", { name: "Fleet 2" }));
    expect(await screen.findByLabelText("Fleet cost 0.02000 CHF_minor")).toBeInTheDocument();
    expect(screen.getByText("Portfolio mean sensing duration")).toBeInTheDocument();
    expect(screen.getByText("100.00%")).toBeInTheDocument();
    expect(screen.getByText("Mean spatial grid coverage")).toBeInTheDocument();
    expect(screen.getByText("25.00%")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "Only cells above 5 min saturation" })).not.toBeChecked();
    expect(screen.getByText(/at least 5 minutes/)).toBeInTheDocument();
    for (const removed of ["Retained round utilities", "Selected sample matrix", "Sampled vehicles", "p-tie-1", "p-tie-2"])
      expect(screen.queryByText(removed, { exact: true })).not.toBeInTheDocument();
    await waitFor(() => expect(fetchMock.mock.calls.some(([url, init]) => String(url) === "/api/v1/matrix-queries" && JSON.parse(String(init?.body)).portfolio_id === "p-tie-2")).toBe(true));
    expect(fetchMock.mock.calls.some(([url]) => /results\/samples-1\/(portfolio_samples|sample_selection)/.test(String(url)))).toBe(false);
  });

  it.each(["cancelled", "completed", "failed"] as const)("shows terminal %s after a cancellation request", async (status) => {
    const terminalJob = { ...job("job-terminal", "simulation-1", "simulation"), status, phase: status, cancel_requested: true };
    vi.spyOn(globalThis, "fetch").mockImplementation(() => response(terminalJob));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><JobMonitor jobId="job-terminal" sourceRevisionId={null} /></QueryClientProvider>);
    expect(await screen.findByText(status, { exact: true })).toBeInTheDocument();
    expect(screen.queryByText("Cancelling")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Request cancellation" })).not.toBeInTheDocument();
  });

  it("shows polling recovery after an SSE disconnect", async () => {
    const running = { ...job("job-running", "simulation-1", "simulation"), status: "running" as const, phase: "simulation", counters: { completed: 1, total: 2 } };
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => String(input) === "/api/v1/jobs/job-running" ? response(running) : Promise.reject(new Error(`Unexpected ${input}`)));
    class BrokenEventSource {
      onopen: (() => void) | null = null;
      onmessage: (() => void) | null = null;
      onerror: (() => void) | null = null;
      constructor() { setTimeout(() => this.onerror?.(), 0); }
      addEventListener() { return undefined; }
      close() { return undefined; }
    }
    vi.stubGlobal("EventSource", BrokenEventSource);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><JobMonitor jobId="job-running" sourceRevisionId="revision-1" /></QueryClientProvider>);
    expect(await screen.findByText("Reconnecting · polling")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText(/1\/2/)).toBeInTheDocument());
    vi.unstubAllGlobals();
  });
  it("provides a managed download after an export completes", async () => {
    const exported = { ...job("export-job", "export-resource", "simulation"), kind: "export" };
    vi.spyOn(globalThis, "fetch").mockImplementation(() => response(exported));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><JobMonitor jobId="export-job" sourceRevisionId={null} /></QueryClientProvider>);
    expect(await screen.findByRole("link", { name: "Download exported file" })).toHaveAttribute("href", "/api/v1/artifacts/export-resource/download");
  });

});
