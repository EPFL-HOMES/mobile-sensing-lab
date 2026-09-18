import { Alert, Box, Button, CircularProgress, MenuItem, Slider, Stack, TextField, Typography } from "@mui/material";
import { useEffect, useMemo, useRef, useState } from "react";
import type { GeoJsonValue } from "../features/results/types";
import workerUrl from "maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url";

const EMPTY_CONTEXT_DATA: GeoJsonValue[] = [];
const SOURCE_READY_TIMEOUT_MS = 12_000;

type MapStatus =
  | { kind: "loading" }
  | { kind: "ready"; renderedFeatureCount: number }
  | { kind: "webgl" | "data" | "rendering"; message: string };

function coordinateBounds(data: GeoJsonValue) {
  let west = Number.POSITIVE_INFINITY;
  let south = Number.POSITIVE_INFINITY;
  let east = Number.NEGATIVE_INFINITY;
  let north = Number.NEGATIVE_INFINITY;
  const visit = (value: unknown) => {
    if (!Array.isArray(value)) return;
    if (value.length >= 2 && typeof value[0] === "number" && typeof value[1] === "number") {
      west = Math.min(west, value[0]);
      south = Math.min(south, value[1]);
      east = Math.max(east, value[0]);
      north = Math.max(north, value[1]);
      return;
    }
    value.forEach(visit);
  };
  data.features.forEach((feature) => visit(feature.geometry.coordinates));
  return Number.isFinite(west)
    ? [[west, south], [east, north]] as [[number, number], [number, number]]
    : null;
}

function validateMapData(data: GeoJsonValue, mode: "movement" | "duration" | "region" | "feature") {
  if (data.crs !== "EPSG:4326") return "Map data must declare EPSG:4326.";
  const supported = mode === "feature" ? new Set(["Point", "MultiPoint", "LineString", "MultiLineString", "Polygon", "MultiPolygon"]) : mode !== "movement"
    ? new Set(["Polygon", "MultiPolygon"])
    : new Set(["LineString", "MultiLineString"]);
  if (data.features.some((feature) => !supported.has(feature.geometry.type))) {
    return `The ${mode} map contains an unsupported geometry type.`;
  }
  const bounds = coordinateBounds(data);
  if (!bounds) return "Map data has no finite coordinates.";
  const [[west, south], [east, north]] = bounds;
  if (west < -180 || east > 180 || south < -90 || north > 90) {
    return "Map coordinates are outside finite WGS84 longitude/latitude bounds.";
  }
  return null;
}

function webglAvailable() {
  const canvas = document.createElement("canvas");
  const context = canvas.getContext("webgl2") ?? canvas.getContext("webgl");
  if (!context) return false;
  context.getExtension("WEBGL_lose_context")?.loseContext();
  return true;
}

type Basemap = "none" | "light" | "streets" | "imagery";
const EMPTY: GeoJsonValue = { type: "FeatureCollection", features: [], crs: "EPSG:4326", returned_count: 0, total_matching_count: 0, is_complete: true, aggregation: null };
const PROVIDERS = {
  light: { tiles: "https://tile.openstreetmap.org/{z}/{x}/{y}.png", attribution: '© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap contributors</a>' },
  streets: { tiles: "https://tile.openstreetmap.org/{z}/{x}/{y}.png", attribution: '© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap contributors</a>' },
  imagery: { tiles: "https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", attribution: 'Imagery © <a href="https://www.arcgis.com/home/item.html?id=10df2279f9684e4a9f6a7f08febac2a9" target="_blank" rel="noopener">Esri and imagery contributors</a>' },
};

export function ScientificMap({ data, mode, unit, contextData, fallback, loading = false, fitKey, label, network = false }: {
  data: GeoJsonValue | null; mode: "movement" | "duration" | "region" | "feature"; unit?: string;
  contextData?: GeoJsonValue[]; fallback: React.ReactNode; loading?: boolean; fitKey?: string; label?: string; network?: boolean;
}) {
  const container = useRef<HTMLDivElement>(null);
  const map = useRef<import("maplibre-gl").Map | null>(null);
  const [initialized, setInitialized] = useState(false);
  const fitted = useRef(false);
  const fittedKey = useRef<string | undefined>(undefined);
  const renderedOnce = useRef(false);
  const contexts = contextData ?? EMPTY_CONTEXT_DATA;
  const [status, setStatus] = useState<MapStatus>({ kind: "loading" });
  const [basemap, setBasemap] = useState<Basemap>(() => {
    try { const stored = localStorage.getItem("sensing.basemap.v2"); return stored && ["none", "light", "streets", "imagery"].includes(stored) ? stored as Basemap : "imagery"; } catch { return "imagery"; }
  });
  const [opacity, setOpacity] = useState(.7);
  const [tileLoading, setTileLoading] = useState(false);
  const [tileError, setTileError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);
  const maxValue = useMemo(() => (data?.features ?? []).reduce((value, feature) => Math.max(value, Number(feature.properties.value ?? 0)), 0), [data]);
  const durationInMinutes = mode === "duration" && unit === "s";
  const displayedMaxValue = durationInMinutes ? maxValue / 60 : maxValue;
  const displayedUnit = durationInMinutes ? "min" : unit;
  const bounds = useMemo(() => coordinateBounds({ ...(data ?? EMPTY), features: [...(data?.features ?? []), ...contexts.flatMap(context => context.features)] }), [data, contexts]);
  const boundsRef = useRef(bounds); boundsRef.current = bounds;
  const hasMapExtent = Boolean(bounds || fitted.current);

  const dataError = data?.features.length ? validateMapData(data, mode) : null;

  useEffect(() => {
    if (!container.current) return;
    if (dataError) { setStatus({ kind: "data", message: dataError }); return; }
    if (!webglAvailable()) { setStatus({ kind: "webgl", message: "WebGL is unavailable." }); return; }
    let disposed = false;
    let observer: ResizeObserver | undefined;
    void import("maplibre-gl").then((library) => {
      if (disposed || !container.current) return;
      library.setWorkerUrl(workerUrl);
      const created = new library.Map({ container: container.current, attributionControl: { compact: false },
        style: { version: 8, sources: {}, layers: [{ id: "background", type: "background", paint: { "background-color": "#f4f7fa" } }] }, center: [6.63, 46.52], zoom: 10 });
      map.current = created;
      created.on("error", (event) => {
        const message = event.error instanceof Error ? event.error.message : "Map renderer error";
        if ((event as { sourceId?: string }).sourceId === "basemap" || /tile\.openstreetmap|arcgisonline|basemap/i.test(message)) {
          setTileError("The selected online basemap could not load. Scientific results remain available."); setTileLoading(false);
          if (created.getLayer("basemap-tiles")) created.setLayoutProperty("basemap-tiles", "visibility", "none");
        } else setStatus({ kind: "rendering", message: `Map rendering failed: ${message}` });
      });
      created.on("load", () => {
        if (disposed) return;
        created.addSource("context", { type: "geojson", data: EMPTY });
        created.addLayer({ id: "context-line", type: "line", source: "context", paint: { "line-color": "#798b9b", "line-width": 1.3, "line-opacity": .7 } });
        created.addSource("scientific-result", { type: "geojson", data: EMPTY });
        created.addLayer({ id: "duration-fill", type: "fill", source: "scientific-result", paint: { "fill-color": "#168d9b", "fill-opacity": .7 } });
        created.addLayer({ id: "network-casing", type: "line", source: "scientific-result", layout: { visibility: "none", "line-cap": "round", "line-join": "round" }, paint: { "line-color": "#142b36", "line-width": ["interpolate", ["linear"], ["zoom"], 10, 1.8, 14, 3.2, 18, 5.5], "line-opacity": .65 } });
        created.addLayer({ id: "movement-line", type: "line", source: "scientific-result", paint: { "line-color": "#00abb7", "line-width": ["interpolate", ["linear"], ["zoom"], 10, .8, 14, 2, 18, 4], "line-opacity": .85 } });
        created.addLayer({ id: "region-line", type: "line", source: "scientific-result", paint: { "line-color": "#087f8c", "line-width": 2 } });
        created.addLayer({ id: "feature-point", type: "circle", source: "scientific-result", filter: ["==", ["geometry-type"], "Point"], layout: { visibility: "none" }, paint: { "circle-radius": 5, "circle-color": "#168d9b", "circle-stroke-color": "#ffffff", "circle-stroke-width": 1 } });
        setInitialized(true);
      });
      if (typeof ResizeObserver !== "undefined") { observer = new ResizeObserver(() => created.resize()); observer.observe(container.current); }
    }).catch((error: unknown) => { if (!disposed) setStatus({ kind: "rendering", message: `Map initialization failed: ${String(error)}` }); });
    return () => { disposed = true; observer?.disconnect(); map.current?.remove(); map.current = null; };
  }, [dataError]);

  useEffect(() => {
    const created = map.current;
    if (!initialized || !created || !hasMapExtent) return;
    if (created.getLayer("basemap-tiles")) created.removeLayer("basemap-tiles");
    if (created.getSource("basemap")) created.removeSource("basemap");
    setTileError(null); setTileLoading(basemap !== "none");
    if (basemap === "none") return;
    const provider = PROVIDERS[basemap];
    created.addSource("basemap", { type: "raster", tiles: [provider.tiles], tileSize: 256, maxzoom: basemap === "imagery" ? 19 : 19, attribution: provider.attribution });
    created.addLayer({ id: "basemap-tiles", type: "raster", source: "basemap", paint: { "raster-opacity": basemap === "light" ? .65 : 1, "raster-saturation": basemap === "light" ? -1 : 0 } }, "context-line");
    let failed = false;
    const pending = () => { if (!failed) setTileLoading(!created.areTilesLoaded()); };
    const error = (event: import("maplibre-gl").ErrorEvent) => { if ((event as { sourceId?: string }).sourceId === "basemap") failed = true; };
    const timeout = window.setTimeout(() => {
      if (!created.isSourceLoaded("basemap")) { failed = true; setTileLoading(false); setTileError("The online basemap is taking too long. Retry or use the local view."); }
    }, 15000);
    created.on("sourcedataloading", pending); created.on("sourcedata", pending); created.on("idle", pending); created.on("error", error);
    return () => { window.clearTimeout(timeout); created.off("sourcedataloading", pending); created.off("sourcedata", pending); created.off("idle", pending); created.off("error", error); };
  }, [basemap, initialized, retry, hasMapExtent]);

  useEffect(() => {
    const created = map.current;
    if (!created || !initialized) return;
    const invalid = data?.features.length ? validateMapData(data, mode) ?? contexts.map((context) => validateMapData(context, "region")).find(Boolean) : null;
    if (invalid) { setStatus({ kind: "data", message: invalid }); return; }
    setStatus({ kind: "loading" });
    (created.getSource("scientific-result") as import("maplibre-gl").GeoJSONSource).setData(data ?? EMPTY);
    (created.getSource("context") as import("maplibre-gl").GeoJSONSource).setData({ ...EMPTY, features: contexts.flatMap(context => context.features) });
    created.setLayoutProperty("duration-fill", "visibility", mode === "movement" ? "none" : "visible");
    created.setLayoutProperty("movement-line", "visibility", mode === "movement" || mode === "feature" ? "visible" : "none");
    created.setLayoutProperty("network-casing", "visibility", mode === "movement" && network ? "visible" : "none");
    created.setLayoutProperty("feature-point", "visibility", mode === "feature" ? "visible" : "none");
    created.setLayoutProperty("region-line", "visibility", mode === "region" ? "visible" : "none");
    created.setPaintProperty("movement-line", "line-color", network ? "#79e6e6" : maxValue > 0 ? ["interpolate", ["linear"], ["sqrt", ["max", 0, ["coalesce", ["get", "value"], 0]]], 0, "#aecdd6", Math.max(Math.sqrt(maxValue)*.5, Number.EPSILON), "#168d9b", Math.max(Math.sqrt(maxValue), Number.EPSILON*2), "#073b64"] : "#00abb7");
    created.setPaintProperty("duration-fill", "fill-color", mode === "region" ? "#24a0aa" : ["interpolate", ["linear"], ["sqrt", ["max", 0, ["coalesce", ["get", "value"], 0]]], 0, "#edf2f6", Math.max(Math.sqrt(maxValue)*.5, Number.EPSILON), "#42b7c4", Math.max(Math.sqrt(maxValue), Number.EPSILON*2), "#073b64"]);
    created.setPaintProperty("feature-point", "circle-color", ["interpolate", ["linear"], ["sqrt", ["max", 0, ["coalesce", ["get", "value"], 0]]], 0, "#edf2f6", Math.max(Math.sqrt(maxValue)*.5, Number.EPSILON), "#42b7c4", Math.max(Math.sqrt(maxValue), Number.EPSILON*2), "#073b64"]);
    created.setPaintProperty("duration-fill", "fill-opacity", mode === "region" ? .12 : ["case", [">", ["coalesce", ["get", "value"], 0], 0], opacity, 0]);
    if ((!fitted.current || fittedKey.current !== fitKey) && boundsRef.current) { created.resize(); created.fitBounds(boundsRef.current, { padding: 36, maxZoom: 22, duration: 0 }); fitted.current = true; fittedKey.current = fitKey; }
    const layer = mode === "movement" ? "movement-line" : mode === "region" ? "region-line" : "duration-fill";
    let ready = false;
    const inspect = (idle = false) => {
      if (ready) return;
      if (!created.isSourceLoaded("scientific-result")) return;
      const count = created.queryRenderedFeatures({ layers: mode === "feature" ? ["duration-fill", "movement-line", "feature-point"] : [layer] }).length;
      // A user may pan away or make zero cells transparent after the initial fit.
      if (count || !data?.features.length || renderedOnce.current || ((mode === "duration" || mode === "feature") && maxValue === 0)) { ready = true; if (count) renderedOnce.current = true; setStatus({ kind: "ready", renderedFeatureCount: count }); }
      else if (idle) setStatus({ kind: "rendering", message: "The scientific source loaded, but no feature reached the rendered layer." });
    };
    const onRender = () => inspect();
    const onIdle = () => inspect(true);
    created.on("render", onRender); created.once("idle", onIdle);
    const timeout = window.setTimeout(() => { if (!ready && !document.hidden) setStatus({ kind: "rendering", message: "The scientific GeoJSON is still waiting for a rendered frame." }); }, SOURCE_READY_TIMEOUT_MS);
    return () => { created.off("render", onRender); created.off("idle", onIdle); window.clearTimeout(timeout); };
  }, [data, contexts, initialized, mode, maxValue, fitKey, network]);
  useEffect(() => {
    if (initialized && (mode === "duration" || mode === "feature")) map.current?.setPaintProperty("duration-fill", "fill-opacity", ["case", [">", ["coalesce", ["get", "value"], 0], 0], opacity, 0]);
    if (initialized && mode === "feature") { map.current?.setPaintProperty("feature-point", "circle-opacity", opacity); map.current?.setPaintProperty("movement-line", "line-opacity", opacity); }
  }, [opacity, initialized, mode]);

  const failed = status.kind !== "loading" && status.kind !== "ready";
  const busy = loading || (Boolean(data?.features.length) && status.kind === "loading");
  return <Stack gap={1}>
    <Stack direction="row" gap={2} alignItems="center" flexWrap="wrap">
      <TextField disabled={false} select size="small" label="Basemap" value={basemap} sx={{ minWidth: 190 }} onChange={(e) => { const value = e.target.value as Basemap; setBasemap(value); try { localStorage.setItem("sensing.basemap.v2", value); } catch { /* Presentation preferences are optional. */ } }}><MenuItem value="none">Local / no online tiles</MenuItem><MenuItem value="light">Light streets · OSM</MenuItem><MenuItem value="streets">Street map · OSM</MenuItem><MenuItem value="imagery">Satellite · Esri World Imagery</MenuItem></TextField>
      <Button disabled={!initialized || !bounds} onClick={() => { if (bounds) map.current?.fitBounds(bounds, { padding: 36, maxZoom: 22, duration: 300 }); }}>Fit result</Button>
      {(mode === "duration" || mode === "feature") && <Stack direction="row" gap={2} alignItems="center"><Typography variant="caption">Overlay opacity</Typography><Slider aria-label="Overlay opacity" value={opacity} min={0} max={1} step={.05} onChange={(_, value) => setOpacity(value as number)} sx={{ width: 100 }} /></Stack>}
      {tileLoading && <Stack direction="row" alignItems="center" gap={1} role="status"><CircularProgress size={16} /><Typography variant="caption">Loading basemap…</Typography></Stack>}
    </Stack>
    {tileError && <Alert severity="warning" action={<Button onClick={() => setRetry(value => value+1)}>Retry</Button>}>{tileError}</Alert>}
    {failed && <Alert severity="warning">{status.message} The scientific table remains available below.</Alert>}
    {!data && !loading && <Alert severity="info">Select a complete result to load its bounded map query.</Alert>}
    {data?.features.length === 0 && !loading && <Alert severity="info">The selected complete result contains zero visible features. Sparse absence represents zero where certified.</Alert>}
    <Box sx={{ position: "relative" }}>
      <Box ref={container} className="scientific-map" aria-label={label ?? (mode === "region" ? "Study region map" : mode === "duration" ? "Sensing duration map" : "Recorded movement map")} data-map-status={status.kind} data-source-feature-count={data?.features.length ?? 0} data-rendered-feature-count={status.kind === "ready" ? status.renderedFeatureCount : 0} />
      {busy && <Stack role="status" direction="row" gap={1} alignItems="center" sx={{ position: "absolute", top: 12, left: 12, p: 1.2, borderRadius: 1, bgcolor: "rgba(255,255,255,.94)", boxShadow: 1 }}><CircularProgress size={20} /><Typography variant="body2">Loading result map…</Typography></Stack>}
    </Box>
    {failed && fallback}
    {status.kind === "ready" && !busy && data && <Typography variant="caption" color="text.secondary">Map ready: {status.renderedFeatureCount} rendered features from {data?.features.length ?? 0} source features.</Typography>}
    {(mode === "duration" || mode === "feature" || mode === "movement" && Boolean(unit) && maxValue > 0) && <Stack direction="row" alignItems="center" gap={1}><Box className="duration-legend" /><Typography variant="caption">0–{displayedMaxValue.toPrecision(4)} {displayedUnit}; square-root color scale.{mode === "duration" ? " Zero cells are transparent and retained in statistics." : mode === "feature" ? " Original source values." : " Mean time per directed edge."}</Typography></Stack>}
  </Stack>;
}
