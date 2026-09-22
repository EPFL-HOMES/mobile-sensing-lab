import { describe, expect, it } from "vitest";
import { portfolioBudgetTrace, portfolioTraces } from "./portfolioPresentation";
import type { PresentationView, PresentationPoint } from "./portfolioPresentation";

const point = (id: string, mean: number, risk: number, frontier = false) => ({ portfolio_id: id, utility_mean: mean, utility_p05: risk, utility_p50: mean, utility_p95: Math.min(1, mean + .1), nondominated: frontier, count_by_fleet: { postal: 5 }, utility_sample_std: .1 } as PresentationPoint);
const response = (budget: number, points: PresentationPoint[]) => ({ budget: { budget_minor: budget, minor_unit_scale: 1, cost_unit: "units", frontier_enabled: true }, risk_metric: "p05", points } as PresentationView);

describe("Portfolio presentation", () => {
  it("hides the zero-budget category without moving its portfolios into positive categories", () => {
    const zero = point("zero", 0, 0, true), paid = point("paid", .7, .6, true);
    for (const only of [true, false]) {
      const traces = portfolioTraces([response(0, [zero]), response(20, [zero, paid])], "zero", only);
      expect(traces.flatMap(trace => trace.customdata ?? [])).toEqual(["paid"]);
      expect(traces.some(trace => trace.name === "First feasible at 0 units")).toBe(false);
      expect(traces.some(trace => trace.mode === "lines")).toBe(false);
    }
    expect(portfolioTraces([response(0, [zero])], "zero")).toEqual([]);
  });
  it("leaves singleton budget categories unconnected", () => {
    const a = point("a", .4, .3, true), b = point("b", .8, .7, true);
    const low = response(10, [a]);
    const high = response(20, [{ ...a, nondominated: false }, b]);
    for (const only of [true, false]) {
      const traces = portfolioTraces([high, low], "a", only);
      expect(traces.some(trace => trace.mode === "lines")).toBe(false);
      expect(traces.filter(trace => trace.mode === "markers").flatMap(trace => trace.customdata)).toEqual(["a", "b"]);
    }
    expect(portfolioTraces([low], "").some(trace => trace.mode === "lines")).toBe(false);
  });
  it("draws nested-budget portfolios once and connects each budget frontier", () => {
    const a = point("a", .7, .5, true), b = point("b", .8, .4, true), c = point("c", .5, .3);
    const traces = portfolioTraces([response(10,[a,c]), response(20,[a,b,c])], "b");
    const markers = traces.filter(t => t.mode === "markers");
    expect(markers.flatMap(t => t.customdata)).toEqual(["a","c","b"]);
    expect(markers.map(t => t.marker?.opacity)).toEqual([[1,.18],[1]]);
    expect(markers[0].marker?.color).not.toBe(markers[1].marker?.color);
    expect(markers[1].x).toEqual([.4]); expect(markers[1].y).toEqual([.8]);
    expect(markers[1].marker?.size).toEqual([15]);
    const lines = traces.filter(t => t.mode === "lines");
    expect(lines).toHaveLength(0);
    const filtered = portfolioTraces([response(10,[a,c]), response(20,[a,b,c])], "b", true);
    expect(filtered.filter(t => t.mode === "markers").flatMap(t => t.customdata)).toEqual(["a","b"]);
    expect(filtered.filter(t => t.mode === "lines")).toHaveLength(0);
  });

  it("connects only same-color category points and groups their legend visibility", () => {
    const a = point("a", .4, .3, true), b = point("b", .8, .5, true), c = point("c", .7, .6, true);
    const traces = portfolioTraces([response(10, [a]), response(100, [a,c,b])], "b", true);
    const line = traces.find(trace => trace.mode === "lines")!;
    const markers = traces.find(trace => trace.mode === "markers" && trace.customdata?.includes("b"))!;
    expect(line.x).toEqual([.5,.6]); expect(line.y).toEqual([.8,.7]);
    expect(line.line?.color).toBe(markers.marker?.color);
    expect(line.legendgroup).toBe(markers.legendgroup);
    expect(line.showlegend).toBe(false);
    expect(traces.filter(trace => trace.mode === "lines")).toHaveLength(1);
  });

  it("orders a mean-std frontier by standard deviation and omits disabled frontiers", () => {
    const a = point("a", .8, .9, true), b = point("b", .7, .9, true);
    a.utility_sample_std = .2; b.utility_sample_std = .1;
    const enabled = { ...response(10, [a,b]), risk_metric: "std" as const };
    const line = portfolioTraces([enabled], "").find(trace => trace.mode === "lines");
    expect(line?.x).toEqual([.1,.2]); expect(line?.y).toEqual([.7,.8]);
    const disabled = { ...enabled, budget: { ...enabled.budget, frontier_enabled: false } };
    expect(portfolioTraces([disabled], "").some(trace => trace.mode === "lines")).toBe(false);
  });

  it("plots one best-mean bar per distinct portfolio with empirical percentile whiskers", () => {
    const a = point("a", .6, .4, true), b = { ...point("b", .8, .7, true), count_by_fleet: { bus: 5, taxi: 5 } };
    const low = { ...response(5, [a]), best_mean_portfolio_id: "a" };
    const repeated = { ...response(10, [a]), best_mean_portfolio_id: "a" };
    const high = { ...response(15, [a,b]), best_mean_portfolio_id: "b" };
    const utility = portfolioBudgetTrace([high, repeated, low], "utility", new Map());
    expect(utility[0].x).toEqual([5, 10]);
    expect(utility[0].y).toEqual([.6, .8]);
    expect(utility[0].customdata).toEqual(["a", "b"]);
    expect(utility[0].textposition).toBe("none");
    expect(utility[0].hovertemplate).toContain("%{text}");
    expect(utility[1].error_y?.arrayminus).toEqual([expect.closeTo(.2), expect.closeTo(.1)]);
    const coverage = portfolioBudgetTrace([low], "coverage", new Map([["a", { mean: .3, p05: .2, p50: .3, p95: .5 }]]));
    expect(coverage[0].y).toEqual([30]);
    expect(coverage[1].error_y?.array).toEqual([20]);
    expect(coverage[1].error_y?.arrayminus).toEqual([10]);
  });
});
