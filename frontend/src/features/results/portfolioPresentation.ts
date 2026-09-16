import type { PortfolioFrontierPoint, PortfolioFrontierView } from "../../api/generated";
import type { ScientificTrace } from "../../components/ScientificChart";

export type PresentationPoint = Pick<PortfolioFrontierPoint, "portfolio_id" | "utility_mean" | "utility_p05" | "utility_sample_std" | "nondominated" | "count_by_fleet">;
export type PresentationView = Pick<PortfolioFrontierView, "risk_metric"> & {
  budget: Pick<PortfolioFrontierView["budget"], "budget_minor" | "minor_unit_scale" | "cost_unit" | "frontier_enabled">;
  points: ReadonlyArray<PresentationPoint>;
};

export const portfolioColors = ["#1763C6", "#D85D0B", "#803AB5", "#12805D", "#C33057"];
const countLabel = (point: PresentationPoint) => Object.entries(point.count_by_fleet).map(([fleet, count]) => `${fleet.replaceAll("_", " ")} ${count}`).join(" · ");

export function portfolioTraces(responses: PresentationView[], selected: string, nondominatedOnly = false): ScientificTrace[] {
  const ordered = [...responses].sort((a, b) => a.budget.budget_minor - b.budget.budget_minor);
  const seen = new Set<string>();
  const frontierIds = new Set(ordered.flatMap(response => response.points.filter(point => point.nondominated).map(point => point.portfolio_id)));
  const traces: ScientificTrace[] = [];
  ordered.forEach((response, index) => {
    const color = portfolioColors[index % portfolioColors.length];
    const points = response.points.filter(point => !seen.has(point.portfolio_id) && (!nondominatedOnly || frontierIds.has(point.portfolio_id)));
    points.forEach(point => seen.add(point.portfolio_id));
    const budget = `${response.budget.budget_minor / response.budget.minor_unit_scale} ${response.budget.cost_unit}`;
    const risk = (point: PresentationPoint) => response.risk_metric === "p05" ? point.utility_p05 : point.utility_sample_std ?? 0;
    if (points.length) traces.push({
      x: points.map(risk), y: points.map(point => point.utility_mean), text: points.map(countLabel), customdata: points.map(point => point.portfolio_id),
      name: `First feasible at ${budget}`, mode: "markers", type: "scatter",
      hovertemplate: `%{text}<br>Mean %{y:.5f}<br>${response.risk_metric === "p05" ? "P05" : "Std"} %{x:.5f}<extra>%{fullData.name}</extra>`,
      marker: { color, opacity: points.map(point => point.portfolio_id === selected || frontierIds.has(point.portfolio_id) ? 1 : 0.18), size: points.map(point => point.portfolio_id === selected ? 15 : frontierIds.has(point.portfolio_id) ? 11 : 8),
        symbol: points.map(point => frontierIds.has(point.portfolio_id) ? "diamond" : "circle"),
        line: { color: points.map(point => point.portfolio_id === selected ? "#102638" : "#ffffff"), width: points.map(point => point.portfolio_id === selected ? 3 : 1.2) } },
    });
  });
  return traces;
}
