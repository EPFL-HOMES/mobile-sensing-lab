import type { PortfolioFrontierPoint, PortfolioFrontierView } from "../../api/generated";
import type { ScientificTrace } from "../../components/ScientificChart";

export type PresentationPoint = Pick<PortfolioFrontierPoint, "portfolio_id" | "utility_mean" | "utility_p05" | "utility_p50" | "utility_p95" | "utility_sample_std" | "nondominated" | "count_by_fleet">;
export type PresentationView = Pick<PortfolioFrontierView, "risk_metric"> & {
  budget: Pick<PortfolioFrontierView["budget"], "budget_minor" | "minor_unit_scale" | "cost_unit" | "frontier_enabled">;
  points: ReadonlyArray<PresentationPoint>;
  best_mean_portfolio_id?: string | null;
};

export interface CoverageSummary {
  mean: number;
  p05: number;
  p50: number;
  p95: number;
}

export const portfolioColors = ["#1763C6", "#D85D0B", "#803AB5", "#12805D", "#C33057"];
const countLabel = (point: PresentationPoint) => Object.entries(point.count_by_fleet).map(([fleet, count]) => `${fleet.replaceAll("_", " ")} ${count}`).join(" · ");

function riskValue(point: PresentationPoint, riskMetric: PresentationView["risk_metric"]): number {
  return riskMetric === "p05" ? point.utility_p05 : point.utility_sample_std ?? 0;
}

export function portfolioTraces(responses: PresentationView[], selected: string, nondominatedOnly = false): ScientificTrace[] {
  const ordered = [...responses].sort((a, b) => a.budget.budget_minor - b.budget.budget_minor);
  const seen = new Set<string>();
  const frontierIds = new Set(ordered.flatMap(response => response.points.filter(point => point.nondominated).map(point => point.portfolio_id)));
  const traces: ScientificTrace[] = [];
  ordered.forEach((response, index) => {
    const color = portfolioColors[index % portfolioColors.length];
    const points = response.points.filter(point => !seen.has(point.portfolio_id) && (!nondominatedOnly || frontierIds.has(point.portfolio_id)));
    points.forEach(point => seen.add(point.portfolio_id));
    if (response.budget.budget_minor === 0) return;
    const budget = `${response.budget.budget_minor / response.budget.minor_unit_scale} ${response.budget.cost_unit}`;
    const risk = (point: PresentationPoint) => riskValue(point, response.risk_metric);
    const legendgroup = `budget-${response.budget.budget_minor}`;
    const frontier = response.budget.frontier_enabled ? points.filter(point => frontierIds.has(point.portfolio_id))
      .sort((a, b) => risk(a) - risk(b) || a.utility_mean - b.utility_mean || a.portfolio_id.localeCompare(b.portfolio_id)) : [];
    if (frontier.length > 1) traces.push({
      x: frontier.map(risk), y: frontier.map(point => point.utility_mean),
      name: `${budget} frontier`, mode: "lines", type: "scatter", showlegend: false, legendgroup,
      line: { color, width: 2.4 },
    });
    if (points.length) traces.push({
      x: points.map(risk), y: points.map(point => point.utility_mean), text: points.map(countLabel), customdata: points.map(point => point.portfolio_id),
      name: `First feasible at ${budget}`, mode: "markers", type: "scatter", legendgroup,
      hovertemplate: `%{text}<br>Mean %{y:.5f}<br>${response.risk_metric === "p05" ? "P05" : "Std"} %{x:.5f}<extra>%{fullData.name}</extra>`,
      marker: { color, opacity: points.map(point => point.portfolio_id === selected || frontierIds.has(point.portfolio_id) ? 1 : 0.18), size: points.map(point => point.portfolio_id === selected ? 15 : frontierIds.has(point.portfolio_id) ? 11 : 8),
        symbol: points.map(point => frontierIds.has(point.portfolio_id) ? "diamond" : "circle"),
        line: { color: points.map(point => point.portfolio_id === selected ? "#102638" : "#ffffff"), width: points.map(point => point.portfolio_id === selected ? 3 : 1.2) } },
    });
  });
  return traces;
}

export function portfolioBudgetTrace(
  responses: PresentationView[],
  metric: "utility" | "coverage",
  coverageByPortfolio: ReadonlyMap<string, CoverageSummary>,
): ScientificTrace[] {
  const seen = new Set<string>();
  const rows = [...responses]
    .sort((a, b) => a.budget.budget_minor - b.budget.budget_minor)
    .flatMap(response => {
      if (response.budget.budget_minor === 0 || !response.best_mean_portfolio_id || seen.has(response.best_mean_portfolio_id)) return [];
      const point = response.points.find(candidate => candidate.portfolio_id === response.best_mean_portfolio_id);
      if (!point) return [];
      const coverage = coverageByPortfolio.get(point.portfolio_id);
      if (metric === "coverage" && !coverage) return [];
      seen.add(point.portfolio_id);
      const mean = metric === "utility" ? point.utility_mean : 100 * coverage!.mean;
      const p05 = metric === "utility" ? point.utility_p05 : 100 * coverage!.p05;
      const p50 = metric === "utility" ? point.utility_p50 : 100 * coverage!.p50;
      const p95 = metric === "utility" ? point.utility_p95 : 100 * coverage!.p95;
      return [{
        point,
        sensors: Object.values(point.count_by_fleet).reduce((sum, count) => sum + count, 0),
        mean,
        p05,
        p50,
        p95,
      }];
    });
  if (!rows.length) return [];
  const common = {
    x: rows.map(row => row.sensors),
    customdata: rows.map(row => row.point.portfolio_id),
    text: rows.map(row => `${countLabel(row.point)}<br>5th–95th percentile ${row.p05.toFixed(metric === "utility" ? 5 : 2)}–${row.p95.toFixed(metric === "utility" ? 5 : 2)}${metric === "coverage" ? "%" : ""}`),
  };
  return [{
    ...common,
    y: rows.map(row => row.mean),
    name: metric === "utility" ? "Mean utility" : "Mean spatial coverage",
    type: "bar",
    textposition: "none",
    marker: { color: rows.map((_, index) => index % 2 ? "#267E91" : "#176783"), line: { color: "#0F536D", width: 1 }, opacity: .9 },
    hovertemplate: metric === "utility"
      ? "%{text}<br>Sensors %{x}<br>Mean %{y:.5f}<extra></extra>"
      : "%{text}<br>Sensors %{x}<br>Mean coverage %{y:.2f}%<extra></extra>",
  }, {
    ...common,
    y: rows.map(row => row.p50),
    name: "Median and P05–P95",
    type: "scatter",
    mode: "markers",
    marker: { color: "#D79245", size: 9, symbol: "diamond", line: { color: "#FFFFFF", width: 1.3 } },
    error_y: {
      type: "data",
      symmetric: false,
      array: rows.map(row => row.p95 - row.p50),
      arrayminus: rows.map(row => row.p50 - row.p05),
      visible: true,
      color: "#A75F2B",
      thickness: 1.7,
      width: 5,
    },
    hovertemplate: metric === "utility"
      ? "%{text}<br>Sensors %{x}<br>Median %{y:.5f}<extra></extra>"
      : "%{text}<br>Sensors %{x}<br>Median coverage %{y:.2f}%<extra></extra>",
  }];
}
