import { describe, expect, it } from "vitest";
import { portfolioTraces } from "./portfolioPresentation";
import type { PresentationView, PresentationPoint } from "./portfolioPresentation";

const point = (id: string, mean: number, risk: number, frontier = false) => ({ portfolio_id: id, utility_mean: mean, utility_p05: risk, nondominated: frontier, count_by_fleet: { postal: 5 }, utility_sample_std: .1 } as PresentationPoint);
const response = (budget: number, points: PresentationPoint[]) => ({ budget: { budget_minor: budget, minor_unit_scale: 1, cost_unit: "units", frontier_enabled: true }, risk_metric: "p05", points } as PresentationView);

describe("Portfolio presentation", () => {
  it("draws nested-budget portfolios once with strong colors and no coordinate jitter", () => {
    const a = point("a", .7, .5, true), b = point("b", .8, .4, true), c = point("c", .5, .3);
    const traces = portfolioTraces([response(10,[a,c]), response(20,[a,b,c])], "b");
    const markers = traces.filter(t => t.mode === "markers");
    expect(markers.flatMap(t => t.customdata)).toEqual(["a","c","b"]);
    expect(markers.map(t => t.marker?.opacity)).toEqual([[1,.18],[1]]);
    expect(markers[0].marker?.color).not.toBe(markers[1].marker?.color);
    expect(markers[1].x).toEqual([.4]); expect(markers[1].y).toEqual([.8]);
    expect(markers[1].marker?.size).toEqual([15]);
    expect(traces.filter(t => t.mode === "lines")).toHaveLength(0);
    const filtered = portfolioTraces([response(10,[a,c]), response(20,[a,b,c])], "b", true);
    expect(filtered.flatMap(t => t.customdata)).toEqual(["a","b"]);
    expect(filtered.every(t => t.mode === "markers")).toBe(true);
  });
});
