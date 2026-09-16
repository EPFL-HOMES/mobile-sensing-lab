import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { SpatialWeightEditor } from "./SpatialWeightEditor";

describe("SpatialWeightEditor", () => {
  it("adds independently weighted prepared features", async () => {
    const user = userEvent.setup();
    const change = vi.fn();
    render(<SpatialWeightEditor label="Task spatial distribution" features={["uniform", "population", "shops"]} legacyFeature="population" onChange={change} />);
    expect(screen.getByText(/normalized over the full prepared grid first/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Add feature" }));
    expect(change).toHaveBeenLastCalledWith([
      { feature: "population", weight: 1 },
      { feature: "uniform", weight: 1 },
    ]);
  });
});
