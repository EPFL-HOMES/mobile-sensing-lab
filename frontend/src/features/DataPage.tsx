import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Accordion, AccordionDetails, AccordionSummary, Alert, Button, MenuItem, Paper, Stack, TextField, Typography, Table, TableBody, TableCell, TableHead, TableRow } from "@mui/material";
import { Link } from "react-router-dom";
import { requestJson } from "../api/client";
import type { InputRegistration, InputDescriptor } from "../api/generated";
import { useWorkspace } from "../app/workspace";
import { Page } from "../components/Page";

export type InputRecord = InputDescriptor;
export const GLOBAL_ROLES = ["boundary", "grid", "network", "speed", "feature"] as const;
export const featureRoles = ["feature", "population", "weight"];
const category = (role: InputRegistration["role"]) => featureRoles.includes(role) ? "feature" : role;

export function InputLibrary({ roles = GLOBAL_ROLES }: { roles?: ReadonlyArray<InputRegistration["role"]> }) {
  const workspace = useWorkspace();
  const inputs = useQuery({ queryKey: ["inputs", workspace.project?.project_id], queryFn: () => requestJson<InputRecord[]>("/api/v1/inputs") });
  const [role, setRole] = useState<InputRegistration["role"]>(category(roles[0]));
  const featureRole = role === "feature";
  const categories = [...new Set(roles.map(category))];
  const [name, setName] = useState(""); const [path, setPath] = useState("");
  const [crs, setCrs] = useState(""); const [layer, setLayer] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false); const [error, setError] = useState<string | null>(null);
  const [preview, setPreview] = useState<InputRecord | null>(null);
  const [confirmedCrs, setConfirmedCrs] = useState("");
  const register = async () => {
    setError(null); setBusy(true);
    try {
      if (file) {
        const form = new FormData(); form.append("file", file); form.append("name", name || file.name);
        form.append("role", role); form.append("source_crs", crs); form.append("layer", layer);
        setPreview(await requestJson<InputRecord>("/api/v1/inputs/upload", { method: "POST", body: form }));
      } else {
        setPreview(await requestJson<InputRecord>("/api/v1/inputs/register", { method: "POST", body: JSON.stringify({ path, name: name || path.split("/").pop(), role, source_crs: crs || null, layer: layer || null }) }));
      }
      await inputs.refetch(); setFile(null); setPath(""); setName("");
    } catch (caught) { setError(caught instanceof Error ? caught.message : String(caught)); }
    finally { setBusy(false); }
  };
  return <Stack gap={2}>
    {error && <Alert severity="error">{error}</Alert>}
    <Paper variant="outlined" className="analysis-panel"><Stack gap={2.5}>
      <Typography variant="h6">Register a local file</Typography>
      <Typography color="text.secondary">GeoPackage, GeoJSON, GeoParquet for geometry; CSV or Parquet for tables; ZIP for GTFS. A snapshot preserves the bytes used by your project.</Typography>
      {featureRole && <Alert severity="info">Population, activity and custom weights are all features. Keep their original values and units; normalization happens when a feature is used. Uniform weighting needs no file.</Alert>}
      <div className="form-grid">
        <TextField select label="Data category" value={featureRole ? "feature" : role} onChange={(e) => setRole(e.target.value as InputRegistration["role"])}>{categories.map((r) => <MenuItem value={r} key={r}>{r.replaceAll("_", " ")}</MenuItem>)}</TextField>
        <TextField label="Display name" value={name} onChange={(e) => setName(e.target.value)} helperText="Used when choosing this input elsewhere. Defaults to the file name." />
      </div>
      <div className="file-source-row">
        <TextField label="Local file path" value={path} disabled={Boolean(file)} onChange={(e) => setPath(e.target.value)} helperText="Path on the computer running this local application" />
        <Button component="label" variant="outlined" sx={{ minHeight: 40, maxWidth: 280 }}>{file?.name || "Browse files"}<input hidden type="file" accept=".csv,.parquet,.gpkg,.geojson,.json,.zip" onChange={(e) => { setFile(e.target.files?.[0] ?? null); setPath(""); }} /></Button>
      </div>
      <Accordion><AccordionSummary>Coordinate and layer metadata · when missing from the file</AccordionSummary><AccordionDetails><div className="form-grid">
        <TextField label="Source CRS (if absent from file)" value={crs} onChange={(e) => setCrs(e.target.value)} placeholder="e.g. EPSG:4326" helperText="File metadata takes precedence. Ordinary x/y coordinates require a declared CRS." />
        <TextField label="Layer (optional)" value={layer} onChange={(e) => setLayer(e.target.value)} helperText="For a GeoPackage with multiple layers" />
      </div></AccordionDetails></Accordion>
      <div className="form-actions"><Typography variant="body2" color="text.secondary">Registered inputs are shared across projects.</Typography><Button variant="contained" disabled={busy || (!path && !file)} onClick={() => void register()}>{busy ? "Registering…" : "Register input"}</Button></div>
    </Stack></Paper>
    {preview && <Paper variant="outlined" sx={{ p: 2, overflowX: "auto" }}><Stack gap={1.5}>
      <Typography variant="h6">Input preview: {preview.name}</Typography>
      <Typography>Columns: {preview.columns.join(", ") || "Geometry / feed archive"}. CRS: {preview.source_crs || "Not declared"}.</Typography>
      {preview.crs_required && <>
        <Alert severity="warning">Confirm the coordinate meaning before using this spatial input. Longitude/latitude in degrees use EPSG:4326; ordinary x/y values require their actual source projection.</Alert>
        <TextField label="Confirmed source CRS" value={confirmedCrs} placeholder={preview.crs_suggestion ?? "EPSG code or CRS definition"} onChange={(e) => setConfirmedCrs(e.target.value)} />
        <Button disabled={!confirmedCrs} onClick={() => void (async () => { try { setPreview(await requestJson<InputRecord>(`/api/v1/inputs/${preview.input_id}/confirm-crs`, { method: "POST", body: JSON.stringify({ source_crs: confirmedCrs }) })); await inputs.refetch(); } catch (caught) { setError(String(caught)); } })()}>Confirm and register coordinate interpretation</Button>
      </>}
      {preview.preview.length > 0 && <Table size="small"><TableHead><TableRow>{preview.columns.map((column) => <TableCell key={column}>{column}</TableCell>)}</TableRow></TableHead><TableBody>{preview.preview.map((row, i) => <TableRow key={i}>{preview.columns.map((column) => <TableCell key={column}>{String(row[column] ?? "")}</TableCell>)}</TableRow>)}</TableBody></Table>}
    </Stack></Paper>}
    <Paper variant="outlined" sx={{ overflowX: "auto" }}><Table size="small"><TableHead><TableRow><TableCell>Name</TableCell><TableCell>Category</TableCell><TableCell>Format</TableCell><TableCell>CRS</TableCell><TableCell>Size</TableCell></TableRow></TableHead><TableBody>
      {(inputs.data ?? []).filter((r) => categories.includes(category(r.role))).map((r) => <TableRow key={r.input_id}><TableCell><Button onClick={() => { setPreview(r); setConfirmedCrs(r.crs_suggestion ?? ""); }}>{r.name}</Button></TableCell><TableCell sx={{ textTransform: "capitalize" }}>{category(r.role).replaceAll("_", " ")}</TableCell><TableCell>{r.format}</TableCell><TableCell>{r.source_crs || (r.crs_required ? "Required before spatial preparation" : "Not applicable")}</TableCell><TableCell>{(r.size_bytes / 1048576).toFixed(2)} MB</TableCell></TableRow>)}
    </TableBody></Table></Paper>
  </Stack>;
}

export function DataPage() {
  return <Page title="Data" description="Global inputs shared by all fleets. Import task, vehicle and assignment files inside Fleet Configuration." actions={<Button component={Link} to="/environment">Next: Environment</Button>}><InputLibrary /></Page>;
}
