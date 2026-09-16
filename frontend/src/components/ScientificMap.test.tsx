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
    };

    constructor(options: Record<string, unknown>) {
      this.record = { options, sources: [], layers: [], resizeCount: 0, fitCount: 0, fitOptions: null, removed: false };
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
    getSource(id: string) { return this.record.sources.includes(id) ? { setData: vi.fn() } : undefined; }
    getLayer(id: string) { return this.record.layers.includes(id); }
    removeSource(id: string) { this.record.sources = this.record.sources.filter(value => value !== id); }
    removeLayer(id: string) { this.record.layers = this.record.layers.filter(value => value !== id); }
    setLayoutProperty() {}
    setPaintProperty() {}
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
});
