import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Accordion, AccordionDetails, AccordionSummary, Alert, Button, Dialog, DialogActions, DialogContent, DialogTitle, LinearProgress, Paper, Stack, Typography } from "@mui/material";
import { requestJson } from "../api/client";
import type { RunView, RunDeletionView } from "../api/generated";

export function RunManagement({ runs, projectId }: { runs: RunView[]; projectId: string }) {
  const client = useQueryClient();
  const [selected, setSelected] = useState<RunView | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const suffix = `?project_id=${encodeURIComponent(projectId)}`;
  const removed = useQuery({ queryKey: ["deleted-runs", projectId], queryFn: () => requestJson<RunDeletionView[]>(`/api/v1/workbench/deleted-runs${suffix}`) });
  const preview = useQuery({ queryKey: ["run-deletion", projectId, selected?.run_id], enabled: Boolean(selected), queryFn: () => requestJson<RunDeletionView>(`/api/v1/workbench/runs/${selected!.run_id}/deletion-preview${suffix}`) });
  const refresh = async () => { await Promise.all([client.invalidateQueries({ queryKey: ["runs", projectId] }), client.invalidateQueries({ queryKey: ["deleted-runs", projectId] }), client.invalidateQueries({ queryKey: ["projects"] })]); };
  const remove = async () => {
    if (!selected || !preview.data) return;
    setPending(true); setError(null);
    try { await requestJson(`/api/v1/workbench/runs/${selected.run_id}${suffix}`, { method: "DELETE" }); setSelected(null); await refresh(); }
    catch (caught) { setError(String(caught)); } finally { setPending(false); }
  };
  const restore = async (id: string) => {
    setPending(true); setError(null);
    try { await requestJson(`/api/v1/workbench/runs/${id}/restore${suffix}`, { method: "POST" }); await refresh(); }
    catch (caught) { setError(String(caught)); } finally { setPending(false); }
  };
  return <Stack gap={2}>
    <Typography variant="h6">Completed runs</Typography>
    {error && !selected && <Alert severity="error">{error}</Alert>}
    {runs.map(run => <Paper key={run.run_id} variant="outlined" sx={{ p: 2.5 }}><Stack gap={1.25}>
      <Typography fontWeight={700}>{run.name}</Typography>
      <Typography variant="body2">{run.config.simulation?.time_mode === "relative" ? "Generic day" : run.config.simulation?.service_date} · {run.config.simulation?.start_time}–{run.config.simulation?.end_time} · {run.replications} joint replications · {run.config.simulation?.temporal_resolution_minutes} minute bins</Typography>
      <Typography variant="body2" color="text.secondary">{run.mobility_reused ? "Reused completed movement" : "Computed vehicle operations"} · {Object.entries(run.vehicle_counts).map(([fleet, count]) => `${run.config.fleets?.find(value => value.fleet_id === fleet)?.name ?? fleet}: ${count} vehicles`).join(" · ")}</Typography>
      <Stack direction="row" justifyContent="space-between" gap={2}><Button component={Link} to={`/results?view=fleet&fleet_run=${run.run_id}`}>View fleet results</Button><Button color="error" onClick={() => { setSelected(run); setError(null); }}>Delete run</Button></Stack>
    </Stack></Paper>)}
    {!!removed.data?.length && <Accordion><AccordionSummary>Deleted runs ({removed.data.length})</AccordionSummary><AccordionDetails><Stack gap={2}>{removed.data.map(run => <Stack key={run.run_id} direction="row" justifyContent="space-between" alignItems="center" gap={2}><Typography>{run.name}</Typography><Button disabled={pending} onClick={() => void restore(run.run_id)}>Restore run</Button></Stack>)}</Stack></AccordionDetails></Accordion>}
    <Dialog open={Boolean(selected)} onClose={() => { if (!pending) setSelected(null); }} fullWidth maxWidth="sm" aria-labelledby="delete-run-title">
      <DialogTitle id="delete-run-title">Delete simulation run?</DialogTitle><DialogContent><Stack gap={2}>
        <Typography fontWeight={700}>{selected?.name}</Typography>
        <Typography>This hides the run from Simulation and Results. Its data are retained for existing analyses and recovery. You can restore it from Deleted runs.</Typography>
        {preview.isFetching && <LinearProgress aria-label="Checking run dependencies" />}
        {!!preview.data?.dependent_analyses.length && <Alert severity="info">Existing analyses remain available: {preview.data.dependent_analyses.join("; ")}</Alert>}
        {!!preview.data?.dependent_runs.length && <Typography variant="body2" color="text.secondary">Related runs retain their source data: {preview.data.dependent_runs.join("; ")}</Typography>}
        {(error || preview.error) && <Alert severity="error">{error ?? String(preview.error)}</Alert>}
      </Stack></DialogContent><DialogActions><Button autoFocus disabled={pending} onClick={() => setSelected(null)}>Cancel</Button><Button color="error" variant="contained" disabled={pending || !preview.data || preview.isFetching} onClick={() => void remove()}>{pending ? "Deleting…" : "Delete run"}</Button></DialogActions>
    </Dialog>
  </Stack>;
}
