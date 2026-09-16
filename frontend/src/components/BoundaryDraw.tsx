import { useEffect, useRef, useState } from "react";
import { Alert, Box, Button, Stack, Typography } from "@mui/material";

export function BoundaryDraw({ onComplete }: { onComplete: (geometry: Record<string, unknown>) => void }) {
  const container = useRef<HTMLDivElement>(null);
  const map = useRef<import("maplibre-gl").Map | null>(null);
  const vertices = useRef<number[][]>([]);
  const [count, setCount] = useState(0);
  const [error, setError] = useState("");
  useEffect(() => {
    let disposed = false;
    void import("maplibre-gl").then((library) => {
      if (disposed || !container.current) return;
      const value = new library.Map({ container: container.current, center: [6.64, 46.56], zoom: 10,
        style: { version: 8, sources: {}, layers: [{ id: "background", type: "background", paint: { "background-color": "#edf2f5" } }] } });
      map.current = value;
      value.addControl(new library.NavigationControl());
      value.on("load", () => {
        value.addSource("drawn", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
        value.addLayer({ id: "drawn", source: "drawn", type: "line", paint: { "line-color": "#007f83", "line-width": 3 } });
      });
      value.on("click", (event) => {
        vertices.current.push([event.lngLat.lng, event.lngLat.lat]); setCount(vertices.current.length);
        const coordinates = vertices.current;
        if (coordinates.length > 1) (value.getSource("drawn") as import("maplibre-gl").GeoJSONSource)?.setData({ type: "Feature", properties: {}, geometry: { type: "LineString", coordinates: [...coordinates, coordinates[0]] } });
      });
    }).catch((caught) => setError(String(caught)));
    return () => { disposed = true; map.current?.remove(); map.current = null; };
  }, []);
  return <Stack gap={1}>
    <Typography>Click vertices, then finish the polygon. Pan and zoom to set the extent. This drawing canvas uses no online basemap.</Typography>
    {error && <Alert severity="error">{error}</Alert>}
    <Box ref={container} sx={{ height: 280 }} />
    <Stack direction="row" gap={1}><Button disabled={count < 3} onClick={() => onComplete({ type: "Polygon", coordinates: [[...vertices.current, vertices.current[0]]] })}>Use polygon ({count} vertices)</Button>
      <Button onClick={() => { vertices.current = []; setCount(0); (map.current?.getSource("drawn") as import("maplibre-gl").GeoJSONSource | undefined)?.setData({ type: "FeatureCollection", features: [] }); }}>Clear</Button></Stack>
  </Stack>;
}
