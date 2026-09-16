import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { DemandEditor } from "../api/generated";
import { DemandTimeEditor } from "./TemporalEditors";

describe("Daily distribution authoring", () => {
  it.each(["window", "rates"] as const)("offers only windows and shares while preserving saved %s input until edited", async (temporal_mode) => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    const value: DemandEditor = { temporal_mode, volume_mode: "expected", task_volume: 1200, time_profile: [{ start_time: "08:00", end_time: "10:00", value: 25 }] };
    render(<DemandTimeEditor value={value} onChange={onChange} input={<div>Registered profile</div>} />);
    expect(onChange).not.toHaveBeenCalled();
    if (temporal_mode === "rates") expect(screen.getByText(/retains a saved rate profile/)).toBeInTheDocument();
    await user.click(screen.getByRole("combobox", { name: "Daily demand distribution" }));
    expect(screen.getAllByRole("option")).toHaveLength(2);
    expect(screen.queryByRole("option", { name: /tasks per hour/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole("option", { name: "Time intervals with relative shares" }));
    expect(onChange).toHaveBeenCalledWith({ temporal_mode: "shares", release_mode: "uniform", time_profile_input: null, time_profile: [{ start_time: "00:00", end_time: "24:00", value: 1 }] });
  });
});
