import { useState } from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { DemandEditor, InputDescriptor } from "../api/generated";
import { DemandSpatialEditor } from "./DemandSpatialEditor";

const odInput = (patch: Partial<InputDescriptor> = {}): InputDescriptor => ({
  input_id: "input_weekday_od", name: "Empirical weekday OD", role: "demand", format: "csv",
  original_filename: "demand_joint_od_weekday.csv", file: "source.csv", columns: ["origin_cell_id", "destination_cell_id", "weight"],
  byte_hash: "a".repeat(64), content_hash: "b".repeat(64), crs_required: false,
  source_crs: "EPSG:32610", layer: null, layers: [], preview: [], size_bytes: 100,
  ...patch,
});
const weekday = odInput();
const weekend = odInput({ input_id: "input_weekend_od", name: "Empirical weekend OD", original_filename: "demand_joint_od_weekend.csv" });
const initial: DemandEditor = {
  source: "generator", task_type: "od", od_distribution_input: weekday.input_id,
  spatial_feature: "population", destination_feature: "shops",
  spatial_weights: [{ feature: "population", weight: 2 }],
  destination_spatial_weights: [{ feature: "shops", weight: 3 }],
};
const features = ["uniform", "population", "shops"];

describe("DemandSpatialEditor", () => {
  it("displays a saved demand-role OD file and hides inactive feature controls", () => {
    const change = vi.fn();
    render(<DemandSpatialEditor demand={initial} inputs={[weekday]} features={features} prepared onChange={change} />);
    expect(screen.getByRole("combobox", { name: "Demand spatial source" })).toHaveTextContent("Empirical weekday OD");
    expect(screen.getByText("demand_joint_od_weekday.csv")).toBeInTheDocument();
    expect(screen.getByText(/Origin and destination cells are sampled together/)).toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Spatial feature" })).not.toBeInTheDocument();
    expect(screen.queryByRole("spinbutton", { name: "Mixture coefficient" })).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(change).not.toHaveBeenCalled();
  });

  it("persists only the selected input reference and restores independent feature weights when explicitly cleared", async () => {
    const user = userEvent.setup();
    const change = vi.fn();
    function Probe() {
      const [demand, setDemand] = useState<DemandEditor>({ ...initial, od_distribution_input: null });
      return <DemandSpatialEditor demand={demand} inputs={[weekday, weekend]} features={features} prepared onChange={(patch) => {
        change(patch); setDemand((value) => ({ ...value, ...patch }));
      }} />;
    }
    render(<Probe />);
    expect(screen.getByRole("combobox", { name: "Demand spatial source" })).toHaveTextContent("Independent origin and destination distributions");
    expect(screen.getAllByRole("combobox", { name: "Spatial feature" })).toHaveLength(2);
    await user.click(screen.getByRole("combobox", { name: "Demand spatial source" }));
    await user.click(screen.getByRole("option", { name: /Empirical weekday OD/ }));
    expect(change).toHaveBeenLastCalledWith({ od_distribution_input: weekday.input_id });
    expect(screen.queryByRole("combobox", { name: "Spatial feature" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("combobox", { name: "Demand spatial source" }));
    await user.click(screen.getByRole("option", { name: /Empirical weekend OD/ }));
    expect(change).toHaveBeenLastCalledWith({ od_distribution_input: weekend.input_id });
    await user.click(screen.getByRole("combobox", { name: "Demand spatial source" }));
    await user.click(screen.getByRole("option", { name: "Independent origin and destination distributions" }));
    expect(change).toHaveBeenLastCalledWith({ od_distribution_input: null });
    expect(screen.getByRole("combobox", { name: "Demand spatial source" })).toHaveTextContent("Independent origin and destination distributions");
    expect(screen.getAllByRole("combobox", { name: "Spatial feature" }).map((field) => field.textContent)).toEqual(["population", "shops"]);
    expect(screen.getAllByRole("spinbutton", { name: "Mixture coefficient" }).map((field) => (field as HTMLInputElement).value)).toEqual(["2", "3"]);
  });

  it("offers compatible demand and legacy feature-role tables, excluding unrelated or incomplete schemas", async () => {
    const user = userEvent.setup();
    const inputs = [
      weekday,
      ...(["feature", "population", "weight"] as const).map((role) => odInput({ input_id: role, name: `Legacy ${role}`, role })),
      odInput({ input_id: "extra", name: "OD with provenance", columns: [...weekday.columns, "source_year"] }),
      odInput({ input_id: "parquet", name: "Parquet OD table", format: "parquet" }),
      odInput({ input_id: "wrong_role", name: "Unrelated vehicle table", role: "supply" }),
      odInput({ input_id: "wrong_format", name: "Geometry file", format: "geojson" }),
      odInput({ input_id: "wrong_columns", name: "Individual trip instances", columns: ["origin_cell_id", "destination_cell_id", "release_time"] }),
      odInput({ input_id: "wrong_case", name: "Wrong column names", columns: ["Origin_cell_id", "destination_cell_id", "weight"] }),
    ];
    render(<DemandSpatialEditor demand={{ ...initial, od_distribution_input: null }} inputs={inputs} features={features} prepared onChange={vi.fn()} />);
    await user.click(screen.getByRole("combobox", { name: "Demand spatial source" }));
    for (const name of ["Empirical weekday OD", "Legacy feature", "Legacy population", "Legacy weight", "OD with provenance", "Parquet OD table"]) {
      expect(screen.getByRole("option", { name: new RegExp(name) })).toBeInTheDocument();
    }
    expect(screen.queryByRole("option", { name: /Unrelated vehicle table|Individual trip instances|Wrong column names|Geometry file/ })).not.toBeInTheDocument();
  });

  it.each([
    ["missing", []],
    ["incompatible columns", [odInput({ columns: ["cell_id", "weight"] })]],
    ["incompatible role", [odInput({ role: "supply" })]],
    ["incompatible format", [odInput({ format: "geojson" })]],
  ] as const)("retains a %s saved reference visibly and reports an error instead of enabling uniform fallback", (_, inputs) => {
    const change = vi.fn();
    render(<DemandSpatialEditor demand={initial} inputs={inputs} features={features} prepared onChange={change} />);
    expect(screen.getByRole("combobox", { name: "Demand spatial source" })).toHaveTextContent(/Unavailable input: input_weekday_od|Empirical weekday OD/);
    expect(screen.getByRole("alert")).toHaveTextContent("The saved input reference is retained.");
    expect(screen.queryByRole("combobox", { name: "Spatial feature" })).not.toBeInTheDocument();
    expect(change).not.toHaveBeenCalled();
  });

  it("keeps the saved OD selection while inputs load and after the descriptors arrive", () => {
    const change = vi.fn();
    const view = render(<DemandSpatialEditor demand={initial} features={features} prepared onChange={change} />);
    expect(screen.getByRole("status")).toHaveTextContent("Loading registered OD inputs");
    expect(screen.getByRole("combobox", { name: "Demand spatial source" })).toHaveTextContent(weekday.input_id);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    view.rerender(<DemandSpatialEditor demand={initial} inputs={[weekday]} features={features} prepared onChange={change} />);
    expect(screen.getByRole("combobox", { name: "Demand spatial source" })).toHaveTextContent("Empirical weekday OD");
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(change).not.toHaveBeenCalled();
  });

  it("reports descriptor loading failures without changing the saved source", () => {
    const change = vi.fn();
    render(<DemandSpatialEditor demand={initial} inputsError="Connection unavailable" features={features} prepared onChange={change} />);
    expect(screen.getByRole("alert")).toHaveTextContent("Could not load registered inputs: Connection unavailable");
    expect(screen.getByRole("combobox", { name: "Demand spatial source" })).toHaveTextContent(weekday.input_id);
    expect(screen.queryByRole("combobox", { name: "Spatial feature" })).not.toBeInTheDocument();
    expect(change).not.toHaveBeenCalled();
  });

  it.each([weekday.input_id, null])("honors read-only projects for source %s", async (selected) => {
    const user = userEvent.setup();
    const change = vi.fn();
    render(<DemandSpatialEditor demand={{ ...initial, od_distribution_input: selected }} inputs={[weekday]} features={features} prepared readOnly onChange={change} />);
    expect(screen.getByRole("combobox", { name: "Demand spatial source" })).toHaveAttribute("aria-disabled", "true");
    await user.click(screen.getByRole("combobox", { name: "Demand spatial source" }));
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    for (const field of screen.queryAllByRole("combobox", { name: "Spatial feature" })) expect(field).toHaveAttribute("aria-disabled", "true");
    for (const field of screen.queryAllByRole("spinbutton", { name: "Mixture coefficient" })) expect(field).toBeDisabled();
    expect(change).not.toHaveBeenCalled();
  });

  it("preserves location-demand controls without showing an OD selector", async () => {
    const user = userEvent.setup();
    const change = vi.fn();
    render(<DemandSpatialEditor demand={{ task_type: "location", spatial_feature: "uniform" }} inputs={[weekday]} features={features} prepared onChange={change} />);
    expect(screen.queryByRole("combobox", { name: "Demand spatial source" })).not.toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Spatial feature" })).toHaveTextContent("Uniform");
    await user.click(screen.getByRole("combobox", { name: "Location reachability" }));
    await user.click(screen.getByRole("option", { name: "Require a route from and back to the depot" }));
    expect(change).toHaveBeenLastCalledWith({ location_condition: "depot_roundtrip" });
  });
});
