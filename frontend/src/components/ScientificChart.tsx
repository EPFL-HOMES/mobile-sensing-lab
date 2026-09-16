import { Alert, Box, Skeleton } from "@mui/material";
import { useEffect, useRef, useState } from "react";

export interface ScientificTrace {
  x: Array<number | string>;
  y: Array<number | string>;
  name: string;
  mode?: "lines" | "markers" | "lines+markers";
  type?: "scatter" | "bar";
  text?: string[];
  customdata?: unknown[];
  hovertemplate?: string;
  marker?: Record<string, unknown>;
  line?: Record<string, unknown>;
}

export function ScientificChart({
  traces,
  xTitle,
  yTitle,
  onPoint,
  fallback,
  precision,
  height = 330,
  horizontalLegend = false,
}: {
  traces: ScientificTrace[];
  xTitle: string;
  yTitle: string;
  onPoint?: (customData: unknown) => void;
  fallback: React.ReactNode;
  precision?: number;
  height?: number;
  horizontalLegend?: boolean;
}) {
  const target = useRef<HTMLDivElement>(null);
  const [failed, setFailed] = useState(false);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    if (!target.current || traces.length === 0) return;
    let disposed = false;
    let plotly: typeof import("plotly.js") | null = null;
    void import("plotly.js/dist/plotly-basic.min.js").then(async (module) => {
      if (disposed || !target.current) return;
      plotly = (module.default ?? module) as typeof import("plotly.js");
      try {
        await plotly.react(
          target.current,
          traces as Plotly.Data[],
          {
            autosize: true,
            margin: { l: 74, r: 20, t: horizontalLegend ? 60 : 20, b: 60 },
            paper_bgcolor: "transparent",
            plot_bgcolor: "#ffffff",
            font: { family: "Inter, sans-serif", color: "#26384b", size: 13 },
            xaxis: { title: { text: xTitle }, automargin: true, nticks: 5, tickformat: precision == null ? undefined : `.${precision}f`, gridcolor: "#e8eef3", zeroline: false },
            yaxis: { title: { text: yTitle }, automargin: true, tickformat: precision == null ? undefined : `.${precision}f`, gridcolor: "#e8eef3", zeroline: false },
            showlegend: traces.length > 1,
            legend: horizontalLegend ? { orientation: "h", x: 0, y: 1.18, font: { size: 11 } } : undefined,
            hovermode: "closest",
          },
          { responsive: true, displaylogo: false, modeBarButtonsToRemove: ["lasso2d", "select2d"] },
        );
        if (onPoint) {
          const plot = target.current as unknown as Plotly.PlotlyHTMLElement;
          plot.on("plotly_click", (event: Plotly.PlotMouseEvent) => {
            onPoint(event.points[0]?.customdata);
          });
        }
        setLoading(false);
      } catch {
        setFailed(true);
        setLoading(false);
      }
    }).catch(() => {
      setFailed(true);
      setLoading(false);
    });
    return () => {
      disposed = true;
      if (plotly && target.current) plotly.purge(target.current);
    };
  }, [onPoint, traces, xTitle, yTitle, precision, horizontalLegend]);
  if (traces.length === 0) return <><Alert severity="info">No chart values are available for this selection.</Alert>{fallback}</>;
  if (failed) return <>{fallback}</>;
  return <Box sx={{ position: "relative", minHeight: height }}>{loading && <Skeleton variant="rounded" height={height} sx={{ position: "absolute", inset: 0 }} />}<Box ref={target} sx={{ height }} aria-label={`${yTitle} by ${xTitle}`} /></Box>;
}
