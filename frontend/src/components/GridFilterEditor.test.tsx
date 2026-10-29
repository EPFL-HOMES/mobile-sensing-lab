import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { GridFilterEditor, gridFilterLabel } from "./GridFilterEditor";
import type { EnvironmentResult, GridFilter, GridFilterRule } from "../api/generated";
import { useState } from "react";

const prepared: EnvironmentResult = { artifact: { artifact_id: "env", artifact_kind: "environment", content_hash: "a".repeat(64) }, features: { artifact_id: "features", artifact_kind: "dataset", content_hash: "b".repeat(64) }, feature_names: ["uniform", "population", "shops"], timezone: "UTC", working_crs: "EPSG:2056" };
const reply = (retained: number) => new Response(JSON.stringify({ total_cell_count: 10, retained_cell_count: retained, excluded_cell_count: 10 - retained }), { headers: { "Content-Type": "application/json" } });
const spatial = (feature: string, logic: "any" | "must" = "any"): GridFilterRule => ({ kind: "spatial_feature", feature, logic });
const road = (logic: "any" | "must" = "any"): GridFilterRule => ({ kind: "road_network", feature: null, logic });
const canonical = (rules: GridFilterRule[]): GridFilter => ({ rules, features: [], match: "any", require_road_intersection: false });
const payload = () => JSON.parse(String(vi.mocked(fetch).mock.calls.at(-1)?.[1]?.body)).grid_filter;

function renderEditor(value?: GridFilter, environment: EnvironmentResult | undefined = prepared, disabled = false, change = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  function Probe() {
    const [selection, setSelection] = useState(value);
    return <GridFilterEditor value={selection} prepared={environment} disabled={disabled} onChange={next => { change(next); setSelection(next); }} />;
  }
  return render(<QueryClientProvider client={client}><Probe /></QueryClientProvider>);
}

beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(globalThis, "fetch").mockImplementation(async () => reply(10));
});

it("adds feature rows, puts Road network in the any union, and combines that union with must rows in the preview payload", async () => {
  vi.mocked(fetch).mockImplementation(async (_, init) => {
    const rules = JSON.parse(String(init?.body)).grid_filter.rules ?? [];
    return reply(!rules.length ? 10 : rules.some((rule: GridFilterRule) => rule.logic === "must") ? 3 : rules.some((rule: GridFilterRule) => rule.kind === "road_network") ? 8 : 4);
  });
  renderEditor();
  const user = userEvent.setup();
  await screen.findByText(/10 \/ 10 cells retained/);
  await user.click(screen.getByRole("button", { name: "Add filter row" }));
  expect(screen.getByRole("combobox", { name: "Filter feature 1" })).toHaveTextContent("population");
  await screen.findByText(/4 \/ 10 cells retained/);
  await user.click(screen.getByRole("button", { name: "Add filter row" }));
  await user.click(screen.getByRole("combobox", { name: "Filter feature 2" }));
  expect(screen.queryByRole("option", { name: "population" })).not.toBeInTheDocument();
  expect(screen.queryByRole("option", { name: "uniform" })).not.toBeInTheDocument();
  await user.click(screen.getByRole("option", { name: "Road network" }));
  await screen.findByText(/8 \/ 10 cells retained/);
  expect(payload()).toEqual(canonical([spatial("population"), road()]));
  expect(screen.getByText(/Sensing and coverage further require/)).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Add filter row" }));
  expect(screen.getByRole("combobox", { name: "Filter feature 3" })).toHaveTextContent("shops");
  await user.click(screen.getByRole("combobox", { name: "Filter logic 3" }));
  await user.click(screen.getByRole("option", { name: "Must include" }));
  await screen.findByText(/3 \/ 10 cells retained/);
  expect(payload()).toEqual(canonical([spatial("population"), road(), spatial("shops", "must")]));
  expect(gridFilterLabel(payload())).toBe("Must include (shops > 0) AND Any of (population > 0 OR Road network)");
  expect(screen.getByRole("button", { name: "Add filter row" })).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "Remove filter row 1" }));
  await waitFor(() => expect(payload()).toEqual(canonical([road(), spatial("shops", "must")])));
  expect(screen.queryByText(/Sensing and coverage further require/)).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Add filter row" })).toBeEnabled();
  await user.click(screen.getByRole("button", { name: "Remove filter row 2" }));
  await user.click(screen.getByRole("button", { name: "Remove filter row 1" }));
  await screen.findByText(/10 \/ 10 cells retained/);
  expect(payload()).toEqual(canonical([]));
  expect(gridFilterLabel(payload())).toBe("All prepared cells");
});

it("supports road-only any and must rows without geographical features", async () => {
  vi.mocked(fetch).mockImplementation(async (_, init) => reply(JSON.parse(String(init?.body)).grid_filter.rules?.length ? 6 : 10));
  renderEditor(undefined, { ...prepared, feature_names: ["uniform"] });
  const user = userEvent.setup();
  await screen.findByText(/10 \/ 10 cells retained/);
  await user.click(screen.getByRole("button", { name: "Add filter row" }));
  await screen.findByText("6 / 10 cells retained.");
  expect(screen.getByRole("combobox", { name: "Filter feature 1" })).toHaveTextContent("Road network");
  expect(screen.getByRole("combobox", { name: "Filter logic 1" })).toHaveTextContent("Any of (OR)");
  expect(payload()).toEqual(canonical([road()]));
  await user.click(screen.getByRole("combobox", { name: "Filter logic 1" }));
  await user.click(screen.getByRole("option", { name: "Must include" }));
  await waitFor(() => expect(payload()).toEqual(canonical([road("must")])));
  expect(screen.getByRole("button", { name: "Add filter row" })).toBeDisabled();
});

it.each(["any", "all"] as const)("renders legacy %s plus mandatory road without rewriting it until an edit", async match => {
  const legacy: GridFilter = { features: ["population", "shops"], match, require_road_intersection: true };
  const change = vi.fn();
  renderEditor(legacy, prepared, false, change);
  await screen.findByText("10 / 10 cells retained.");
  expect(payload()).toEqual(legacy);
  expect(change).not.toHaveBeenCalled();
  expect(screen.getByRole("combobox", { name: "Filter logic 1" })).toHaveTextContent(match === "all" ? "Must include" : "Any of (OR)");
  expect(screen.getByRole("combobox", { name: "Filter feature 3" })).toHaveTextContent("Road network");
  expect(screen.getByRole("combobox", { name: "Filter logic 3" })).toHaveTextContent("Must include");
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Remove filter row 2" }));
  const expected = canonical([spatial("population", match === "all" ? "must" : "any"), road("must")]);
  await waitFor(() => expect(payload()).toEqual(expected));
  expect(change).toHaveBeenLastCalledWith(expected);
});

it("retains a missing saved feature visibly and lets the user replace it without losing row logic", async () => {
  renderEditor(canonical([spatial("residents_2020", "must"), road()]));
  await screen.findByText("10 / 10 cells retained.");
  const missing = screen.getByRole("combobox", { name: "Filter feature 1" });
  expect(missing).toHaveTextContent("residents_2020 (not in prepared environment)");
  expect(screen.getByText(/This feature is absent from the prepared environment/)).toBeInTheDocument();
  const user = userEvent.setup();
  await user.click(missing);
  expect(screen.queryByRole("option", { name: "Road network" })).not.toBeInTheDocument();
  await user.click(screen.getByRole("option", { name: "population" }));
  await waitFor(() => expect(payload()).toEqual(canonical([spatial("population", "must"), road()])));
  expect(screen.queryByText(/This feature is absent/)).not.toBeInTheDocument();
});

it("refreshes previews when either environment or feature artifact identity changes", async () => {
  vi.mocked(fetch).mockImplementation(async (_, init) => {
    const environment = JSON.parse(String(init?.body)).prepared_environment;
    return reply(environment.features.content_hash !== prepared.features.content_hash ? 2 : environment.artifact.content_hash !== prepared.artifact.content_hash ? 3 : 6);
  });
  const client = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity } } });
  const editor = (environment: EnvironmentResult) => <QueryClientProvider client={client}><GridFilterEditor prepared={environment} value={canonical([road("must")])} disabled={false} onChange={vi.fn()} /></QueryClientProvider>;
  const view = render(editor(prepared));
  await screen.findByText("6 / 10 cells retained.");
  const changedRoad = { ...prepared, artifact: { ...prepared.artifact, content_hash: "c".repeat(64) } };
  view.rerender(editor(changedRoad));
  await screen.findByText("3 / 10 cells retained.");
  view.rerender(editor({ ...changedRoad, features: { ...prepared.features, content_hash: "d".repeat(64) } }));
  await screen.findByText("2 / 10 cells retained.");
  expect(fetch).toHaveBeenCalledTimes(3);
});

it("shows empty-result errors from the preview", async () => {
  vi.mocked(fetch).mockResolvedValue(new Response(JSON.stringify({ code: "INVALID_GRID_FILTER", message: "Grid filter retains no cells; choose different spatial features", issues: [], request_id: "preview" }), { status: 422, headers: { "Content-Type": "application/json" } }));
  renderEditor(canonical([spatial("population", "must"), road("must")]));
  expect(await screen.findByRole("alert")).toHaveTextContent("Grid filter retains no cells");
  expect(screen.queryByText(/cells retained/)).not.toBeInTheDocument();
});

it("disables all row editing for reference projects while preserving preview", async () => {
  const change = vi.fn();
  renderEditor(canonical([spatial("population"), road("must")]), prepared, true, change);
  await screen.findByText("10 / 10 cells retained.");
  for (const input of screen.getAllByRole("combobox")) expect(input).toHaveAttribute("aria-disabled", "true");
  for (const button of screen.getAllByRole("button")) expect(button).toBeDisabled();
  expect(change).not.toHaveBeenCalled();
});

it("disables filtering until the environment is prepared", () => {
  render(<QueryClientProvider client={new QueryClient()}><GridFilterEditor value={{ features: ["population"] }} disabled={false} onChange={vi.fn()} /></QueryClientProvider>);
  expect(screen.getByRole("combobox", { name: "Filter feature 1" })).toHaveAttribute("aria-disabled", "true");
  expect(screen.getByRole("combobox", { name: "Filter logic 1" })).toHaveAttribute("aria-disabled", "true");
  expect(screen.getByRole("button", { name: "Add filter row" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Remove filter row 1" })).toBeDisabled();
  expect(fetch).not.toHaveBeenCalled();
});

it("describes mixed and legacy filters faithfully in saved-run summaries", () => {
  expect(gridFilterLabel()).toBe("All prepared cells");
  expect(gridFilterLabel(canonical([]))).toBe("All prepared cells");
  expect(gridFilterLabel({ features: ["population", "shops"], match: "any", require_road_intersection: true })).toBe("Must include (Road network) AND Any of (population > 0 OR shops > 0)");
  expect(gridFilterLabel({ features: ["population", "shops"], match: "all" })).toBe("Must include (population > 0 AND shops > 0)");
  expect(gridFilterLabel({ require_road_intersection: true })).toBe("Must include (Road network)");
  expect(gridFilterLabel(canonical([spatial("population", "must"), spatial("shops"), road()]))).toBe("Must include (population > 0) AND Any of (shops > 0 OR Road network)");
});
