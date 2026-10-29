import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ScientificMap } from "./ScientificMap";
import type { GeoJsonValue } from "../features/results/types";

const mapHarness = vi.hoisted(() => ({
  instances: [] as Array<{
    options: Record<string, unknown>;
    sources: string[];
    layers: string[];
    resizeCount: number;
    fitCount: number;
    fitOptions: Record<string, unknown> | null;
    removed: boolean;
    sourceUpdates: Array<{ id: string; data: GeoJsonValue }>;
    paint: Array<{ id: string; property: string; value: unknown }>;
  }>,
  renderedFeatureCount: 1,
  emit: (_type: string, _event: { error?: Error; sourceId?: string }) => {},
}));

vi.mock("maplibre-gl", () => ({
  setWorkerUrl: vi.fn(),
  Map: class {
    private handlers = new Map<string, Set<(event: { error?: Error; sourceId?: string }) => void>>();
    private record: {
      options: Record<string, unknown>;
      sources: string[];
      layers: string[];
      resizeCount: number;
      fitCount: number;
      fitOptions: Record<string, unknown> | null;
      removed: boolean;
      sourceUpdates: Array<{ id: string; data: GeoJsonValue }>;
      paint: Array<{ id: string; property: string; value: unknown }>;
    };

    constructor(options: Record<string, unknown>) {
      this.record = { options, sources: [], layers: [], resizeCount: 0, fitCount: 0, fitOptions: null, removed: false, sourceUpdates: [], paint: [] };
      mapHarness.instances.push(this.record);
      mapHarness.emit = (type, event) => this.handlers.get(type)?.forEach(listener => listener(event));
      queueMicrotask(() => mapHarness.emit("load", {}));
    }

    on(type: string, listener: (event: { error?: Error }) => void) {
      const listeners = this.handlers.get(type) ?? new Set(); listeners.add(listener); this.handlers.set(type, listeners);
      return this;
    }

    once(type: string, listener: () => void) {
      if (type === "idle") queueMicrotask(listener);
      return this;
    }

    off(type: string, listener: (event: { error?: Error }) => void) { this.handlers.get(type)?.delete(listener); return this; }
    getSource(id: string) { return this.record.sources.includes(id) ? { setData: (data: GeoJsonValue) => this.record.sourceUpdates.push({ id, data }) } : undefined; }
    getLayer(id: string) { return this.record.layers.includes(id); }
    removeSource(id: string) { this.record.sources = this.record.sources.filter(value => value !== id); }
    removeLayer(id: string) { this.record.layers = this.record.layers.filter(value => value !== id); }
    setLayoutProperty() {}
    setPaintProperty(id: string, property: string, value: unknown) { this.record.paint.push({ id, property, value }); }
    areTilesLoaded() { return true; }
    addSource(id: string) { this.record.sources.push(id); }
    addLayer(layer: { id: string }) { this.record.layers.push(layer.id); }
    resize() { this.record.resizeCount += 1; }
    fitBounds(_bounds: unknown, options: Record<string, unknown>) { this.record.fitOptions = options; this.record.fitCount += 1; }
    isSourceLoaded() { return true; }
    queryRenderedFeatures() { return Array.from({ length: mapHarness.renderedFeatureCount }, () => ({})); }
    remove() { this.record.removed = true; }
  },
}));

const grid: GeoJsonValue = {
  type: "FeatureCollection",
  features: [{
    type: "Feature",
    geometry: {
      type: "Polygon",
      coordinates: [[[6.52666, 46.49755], [6.52731, 46.49755], [6.52731, 46.49792], [6.52666, 46.49792], [6.52666, 46.49755]]],
    },
    properties: { cell_id: "cell-1", value: 5 },
  }],
  returned_count: 1,
  total_matching_count: 1,
  is_complete: true,
  crs: "EPSG:4326",
  aggregation: null,
};

function coloredGrid(p95 = 120, maximum = 60000): GeoJsonValue {
  return { ...grid, color_scale: { positive_p95: p95, maximum, positive_count: 20, scope: "complete_query" },
    features: grid.features.map(feature => ({ ...feature, properties: { ...feature.properties, value: maximum } })),
  };
}

function lastPaint(id: string, property: string) {
  return mapHarness.instances[0].paint.filter(item => item.id === id && item.property === property).at(-1)?.value;
}

async function chooseLimit(option: string) {
  fireEvent.mouseDown(screen.getByRole("combobox", { name: "Color upper limit" }));
  fireEvent.click(await screen.findByRole("option", { name: option }));
}

describe("ScientificMap lifecycle", () => {
  let resizeCallback: ResizeObserverCallback;

  beforeEach(() => {
    localStorage.clear();
    mapHarness.instances.length = 0;
    mapHarness.renderedFeatureCount = 1;
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({ getExtension: () => null } as unknown as WebGLRenderingContext);
    class TestResizeObserver {
      constructor(callback: ResizeObserverCallback) { resizeCallback = callback; }
      observe() { return undefined; }
      disconnect() { return undefined; }
      unobserve() { return undefined; }
    }
    vi.stubGlobal("ResizeObserver", TestResizeObserver);
  });

  it("accepts mixed source points, lines and polygons", async () => {
    const source: GeoJsonValue = { ...grid, features: [...grid.features,
      { type: "Feature", properties: { value: 2 }, geometry: { type: "Point", coordinates: [6.6, 46.5] } },
      { type: "Feature", properties: { value: 3 }, geometry: { type: "LineString", coordinates: [[6.6, 46.5], [6.7, 46.6]] } },
    ], returned_count: 3, total_matching_count: 3 };
    render(<ScientificMap data={source} mode="feature" unit="count" fallback={<div>fallback</div>} />);
    expect(await screen.findByText("Map ready: 1 rendered features from 3 source features.")).toBeInTheDocument();
    expect(mapHarness.instances[0].layers).toContain("feature-point");
  });

  it("keeps one map across parent rerenders, fits the small fixture, and reacts to layout resize", async () => {
    const view = render(<ScientificMap data={grid} mode="duration" unit="s" fallback={<div>table fallback</div>} />);
    expect(await screen.findByText("Map ready: 1 rendered features from 1 source features.")).toBeInTheDocument();
    expect(mapHarness.instances).toHaveLength(1);
    expect(mapHarness.instances[0].sources).toEqual(["context", "scientific-result", "basemap"]);
    expect(mapHarness.instances[0].layers).toEqual(["context-line", "duration-fill", "network-casing", "movement-line", "region-line", "feature-point", "basemap-tiles"]);
    expect(mapHarness.instances[0].fitOptions).toMatchObject({ padding: 36, maxZoom: 22, duration: 0 });
    expect(screen.getByText(/0–0\.08333 min; square-root color scale/)).toBeInTheDocument();

    view.rerender(<ScientificMap data={{ ...grid, features: grid.features.map(feature => ({ ...feature, properties: { ...feature.properties, value: 8 } })) }} mode="duration" unit="s" fallback={<div>updated fallback</div>} />);
    expect(mapHarness.instances).toHaveLength(1);
    const beforeResize = mapHarness.instances[0].resizeCount;
    act(() => resizeCallback([], {} as ResizeObserver));
    expect(mapHarness.instances[0].resizeCount).toBe(beforeResize + 1);
  });

  it("refits changed search regions without recreating the map or resetting result cameras", async () => {
    const view = render(<ScientificMap data={grid} fitKey="search:first" mode="region" fallback={<div>fallback</div>} />);
    await screen.findByText("Map ready: 1 rendered features from 1 source features.");
    const record = mapHarness.instances[0];
    const initial = record.fitCount;
    view.rerender(<ScientificMap data={{ ...grid }} fitKey="search:first" mode="region" fallback={<div>fallback</div>} />);
    expect(record.fitCount).toBe(initial);
    view.rerender(<ScientificMap data={{ ...grid }} fitKey="search:second" mode="region" fallback={<div>fallback</div>} />);
    await waitFor(() => expect(record.fitCount).toBe(initial + 1));
    expect(mapHarness.instances).toHaveLength(1);
  });

  it("classifies a loaded source with no rendered feature and retains the table fallback", async () => {
    mapHarness.renderedFeatureCount = 0;
    render(<ScientificMap data={grid} mode="duration" unit="s" fallback={<div>scientific table fallback</div>} />);
    expect(await screen.findByText(/source loaded, but no feature reached the rendered layer/)).toBeInTheDocument();
    expect(screen.getByText("scientific table fallback")).toBeInTheDocument();
  });

  it("reports online tile failures without discarding a rendered scientific layer", async () => {
    render(<ScientificMap data={grid} mode="duration" unit="s" fallback={<div>table fallback</div>} />);
    await screen.findByText("Map ready: 1 rendered features from 1 source features.");
    expect(screen.getByRole("combobox", { name: /Basemap/ })).toHaveTextContent("Satellite · Esri World Imagery");
    expect(screen.getByText("Loading basemap…")).toBeInTheDocument();
    act(() => mapHarness.emit("error", { sourceId: "basemap", error: new Error("Tile unavailable") }));
    expect(await screen.findByText(/online basemap could not load/)).toBeInTheDocument();
    expect(screen.getByText("Map ready: 1 rendered features from 1 source features.")).toBeInTheDocument();
    expect(screen.queryByText("table fallback")).not.toBeInTheDocument();
    expect(mapHarness.instances).toHaveLength(1);
  });

  it("applies the new satellite default once and remembers an explicit local choice", async () => {
    localStorage.setItem("sensing.basemap", "none");
    const view = render(<ScientificMap data={grid} mode="duration" unit="s" fallback={<div>fallback</div>} />);
    await screen.findByText("Map ready: 1 rendered features from 1 source features.");
    expect(screen.getByRole("combobox", { name: /Basemap/ })).toHaveTextContent("Satellite");
    fireEvent.mouseDown(screen.getByRole("combobox", { name: /Basemap/ }));
    fireEvent.click(await screen.findByRole("option", { name: "Local / no online tiles" }));
    expect(localStorage.getItem("sensing.basemap.v2")).toBe("none");
    expect(mapHarness.instances[0].sources).not.toContain("basemap");
    view.unmount();
    render(<ScientificMap data={grid} mode="duration" unit="s" fallback={<div>fallback</div>} />);
    await screen.findByText("Map ready: 1 rendered features from 1 source features.");
    expect(screen.getByRole("combobox", { name: /Basemap/ })).toHaveTextContent("Local / no online tiles");
    expect(mapHarness.instances[1].sources).not.toContain("basemap");
  });

  it("rejects non-finite or non-WGS84 coordinates before renderer initialization", async () => {
    const invalid = structuredClone(grid);
    invalid.features[0].geometry.coordinates = [[[200, 46], [201, 46], [201, 47], [200, 47], [200, 46]]];
    render(<ScientificMap data={invalid} mode="duration" unit="s" fallback={<div>scientific table fallback</div>} />);
    expect(await screen.findByText(/outside finite WGS84 longitude\/latitude bounds/)).toBeInTheDocument();
    await waitFor(() => expect(mapHarness.instances).toHaveLength(0));
  });

  it("uses the backend positive P95 for every colored geometry without changing raw values", async () => {
    const data = coloredGrid();
    data.features.push({ type: "Feature", properties: { value: 60 }, geometry: { type: "Point", coordinates: [6.6, 46.5] } });
    data.features.push({ type: "Feature", properties: { value: 30 }, geometry: { type: "LineString", coordinates: [[6.6, 46.5], [6.7, 46.6]] } });
    render(<ScientificMap data={data} mode="feature" unit="count" fallback={<div>fallback</div>} />);
    await screen.findByText("Map ready: 1 rendered features from 3 source features.");
    expect(screen.getByRole("combobox", { name: "Color upper limit" })).toHaveTextContent("95th percentile");
    expect(screen.getByText(/P95 of positive values in the selected query/)).toHaveTextContent("Highest color: ≥ 120.0 count");
    for (const [id, property] of [["duration-fill", "fill-color"], ["feature-point", "circle-color"], ["movement-line", "line-color"]]) {
      expect(lastPaint(id, property)).toEqual(expect.arrayContaining([Math.sqrt(120)]));
      expect(lastPaint(id, property)).not.toEqual(expect.arrayContaining([Math.sqrt(60000)]));
    }
    const original = mapHarness.instances[0].sourceUpdates.find(update => update.id === "scientific-result");
    expect(original?.data).toBe(data);
    expect(original?.data.features[0].properties.value).toBe(60000);
  });

  it("changes only paint for manual caps, uses displayed minutes, and preserves the last valid cap", async () => {
    render(<ScientificMap data={coloredGrid()} mode="duration" unit="s" fitKey="run:first" fallback={<div>fallback</div>} />);
    await screen.findByText("Map ready: 1 rendered features from 1 source features.");
    const record = mapHarness.instances[0];
    const sourceUpdates = record.sourceUpdates.length;
    const fitCount = record.fitCount;
    await chooseLimit("Custom upper limit");
    const input = screen.getByRole("spinbutton", { name: "Upper limit (min)" });
    expect(input).toHaveValue(2);
    fireEvent.change(input, { target: { value: "3" } });
    expect(lastPaint("duration-fill", "fill-color")).toEqual(expect.arrayContaining([Math.sqrt(180)]));
    expect(screen.getByText(/Highest color:/)).toHaveTextContent("Highest color: ≥ 3.000 min");
    for (const value of ["0", "-2", "", "1e308"]) {
      fireEvent.change(input, { target: { value } });
      expect(screen.getByText(/Enter a finite value greater than zero/)).toBeInTheDocument();
      expect(lastPaint("duration-fill", "fill-color")).toEqual(expect.arrayContaining([Math.sqrt(180)]));
    }
    fireEvent.change(input, { target: { value: "4" } });
    expect(screen.queryByText(/Enter a finite value greater than zero/)).not.toBeInTheDocument();
    expect(lastPaint("duration-fill", "fill-color")).toEqual(expect.arrayContaining([Math.sqrt(240)]));
    expect(record.sourceUpdates).toHaveLength(sourceUpdates);
    expect(record.fitCount).toBe(fitCount);
    expect(mapHarness.instances).toHaveLength(1);
    await chooseLimit("Data maximum");
    expect(lastPaint("duration-fill", "fill-color")).toEqual(expect.arrayContaining([Math.sqrt(60000)]));
    expect(screen.getByText(/0–1000 min/)).not.toHaveTextContent("Highest color:");
    await chooseLimit("95th percentile");
    expect(lastPaint("duration-fill", "fill-color")).toEqual(expect.arrayContaining([Math.sqrt(120)]));
  });

  it("preserves a manual cap across query values but resets it for a changed resource, unit, or layer", async () => {
    const view = render(<ScientificMap data={coloredGrid()} mode="duration" unit="s" fitKey="run:first" fallback={<div>fallback</div>} />);
    await screen.findByText("Map ready: 1 rendered features from 1 source features.");
    await chooseLimit("Custom upper limit");
    fireEvent.change(screen.getByRole("spinbutton", { name: "Upper limit (min)" }), { target: { value: "3" } });
    view.rerender(<ScientificMap data={coloredGrid(240)} mode="duration" unit="s" fitKey="run:first" fallback={<div>fallback</div>} />);
    expect(screen.getByRole("spinbutton", { name: "Upper limit (min)" })).toHaveValue(3);
    expect(lastPaint("duration-fill", "fill-color")).toEqual(expect.arrayContaining([Math.sqrt(180)]));
    view.rerender(<ScientificMap data={coloredGrid(240)} mode="duration" unit="s" fitKey="run:second" fallback={<div>fallback</div>} />);
    expect(screen.getByRole("combobox", { name: "Color upper limit" })).toHaveTextContent("95th percentile");
    expect(screen.queryByRole("spinbutton")).not.toBeInTheDocument();
    expect(lastPaint("duration-fill", "fill-color")).toEqual(expect.arrayContaining([Math.sqrt(240)]));
    await chooseLimit("Custom upper limit");
    view.rerender(<ScientificMap data={coloredGrid(240)} mode="duration" unit="s^2" fitKey="run:second" fallback={<div>fallback</div>} />);
    expect(screen.getByRole("combobox", { name: "Color upper limit" })).toHaveTextContent("95th percentile");
    expect(screen.getByText(/0–240.0 s\^2/)).toBeInTheDocument();
    await chooseLimit("Custom upper limit");
    view.rerender(<ScientificMap data={coloredGrid(240)} mode="feature" unit="s^2" fitKey="run:second" fallback={<div>fallback</div>} />);
    expect(screen.getByRole("combobox", { name: "Color upper limit" })).toHaveTextContent("95th percentile");
  });

  it.each([null, 0, -1, Number.NaN, Number.POSITIVE_INFINITY, 60001])("falls back to the maximum for unavailable or invalid P95 %s", async p95 => {
    const data = coloredGrid();
    data.color_scale!.positive_p95 = p95;
    render(<ScientificMap data={data} mode="duration" unit="s" fallback={<div>fallback</div>} />);
    await screen.findByText("Map ready: 1 rendered features from 1 source features.");
    expect(screen.getByText(/P95 unavailable; using the data maximum/)).toBeInTheDocument();
    expect(lastPaint("duration-fill", "fill-color")).toEqual(expect.arrayContaining([Math.sqrt(60000)]));
  });

  it("keeps zero cells transparent and an empty positive domain finite", async () => {
    const data = coloredGrid(0, 0);
    data.color_scale!.positive_p95 = null;
    data.color_scale!.positive_count = 0;
    mapHarness.renderedFeatureCount = 0;
    render(<ScientificMap data={data} mode="duration" unit="s" fallback={<div>fallback</div>} />);
    await screen.findByText("Map ready: 0 rendered features from 1 source features.");
    expect(screen.getByText(/No positive values in the selected query/)).toBeInTheDocument();
    expect(lastPaint("duration-fill", "fill-color")).toEqual(expect.arrayContaining([Number.EPSILON, Number.EPSILON * 2]));
    expect(lastPaint("duration-fill", "fill-opacity")).toEqual(["case", [">", ["coalesce", ["get", "value"], 0], 0], .7, 0]);
  });

  it("labels legacy missing metadata and returned-feature summaries explicitly", async () => {
    const view = render(<ScientificMap data={grid} mode="duration" unit="s" fallback={<div>fallback</div>} />);
    await screen.findByText("Map ready: 1 rendered features from 1 source features.");
    expect(screen.getByText(/P95 unavailable; using the data maximum/)).toBeInTheDocument();
    const data = coloredGrid();
    data.color_scale!.scope = "returned_features";
    view.rerender(<ScientificMap data={data} mode="duration" unit="s" fallback={<div>fallback</div>} />);
    expect(screen.getByText(/P95 of positive values in the returned features/)).toBeInTheDocument();
  });

  it("caps movement duration but omits numeric color controls for regions and plain networks", async () => {
    const data: GeoJsonValue = { ...coloredGrid(), features: [{ type: "Feature", properties: { value: 60000 }, geometry: { type: "LineString", coordinates: [[6.6, 46.5], [6.7, 46.6]] } }] };
    const view = render(<ScientificMap data={data} mode="movement" unit="s" fallback={<div>fallback</div>} />);
    await screen.findByText("Map ready: 1 rendered features from 1 source features.");
    expect(lastPaint("movement-line", "line-color")).toEqual(expect.arrayContaining([Math.sqrt(120)]));
    expect(screen.getByRole("combobox", { name: "Color upper limit" })).toBeInTheDocument();
    view.rerender(<ScientificMap data={data} mode="movement" unit="s" network fallback={<div>fallback</div>} />);
    expect(screen.queryByRole("combobox", { name: "Color upper limit" })).not.toBeInTheDocument();
    expect(lastPaint("movement-line", "line-color")).toBe("#79e6e6");
    view.rerender(<ScientificMap data={grid} mode="region" fallback={<div>fallback</div>} />);
    expect(screen.queryByRole("combobox", { name: "Color upper limit" })).not.toBeInTheDocument();
    expect(lastPaint("duration-fill", "fill-color")).toBe("#24a0aa");
  });

});
