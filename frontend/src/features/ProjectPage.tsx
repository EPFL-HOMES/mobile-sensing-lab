import AddRounded from "@mui/icons-material/AddRounded";
import ContentCopyRounded from "@mui/icons-material/ContentCopyRounded";
import FolderOpenRounded from "@mui/icons-material/FolderOpenRounded";
import {
  Button,
  Card,
  CardActions,
  CardContent,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Stack,
  TextField,
  Typography,
  Alert, Chip, LinearProgress,
} from "@mui/material";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Controller, useForm } from "react-hook-form";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { requestJson } from "../api/client";
import { ApiIssues } from "../components/ApiIssues";
import { Page } from "../components/Page";
import { useWorkspace } from "../app/workspace";
import type { WorkspaceInfo, ProjectRecord } from "../api/generated";
import { JobMonitor } from "../components/JobMonitor";
import { ProjectFiles, ImportProject } from "./ProjectFiles";

export function ProjectPage() {
  const workspace = useWorkspace();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const initialization = useQuery({ queryKey: ["workspace-initialization"], queryFn: async () => {
    const info = await requestJson<WorkspaceInfo>("/api/v1/workbench/workspace/initialize", { method: "POST" });
    await queryClient.invalidateQueries({ queryKey: ["projects"] });
    return info;
  }, refetchInterval: (query) => (query.state.data?.example_job_ids?.length || query.state.data?.example_job_id) ? 1500 : false });
  const projects = useQuery({ queryKey: ["projects"], queryFn: () => requestJson<ProjectRecord[]>("/api/v1/projects"), refetchInterval: 3000 });
  const [open, setOpen] = useState(false);
  const [fileProject,setFileProject] = useState<ProjectRecord | null>(null);
  const [editing,setEditing] = useState<ProjectRecord | null>(null);
  const [importing,setImporting] = useState(false);
  const [deleting, setDeleting] = useState<ProjectRecord | null>(null);
  const [deletePending, setDeletePending] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const confirmDelete = async () => {
    if (!deleting || deletePending) return;
    setDeletePending(true);
    setDeleteError(null);
    try {
      await workspace.deleteProject(deleting.project_id);
      setDeleting(null);
      await projects.refetch();
    } catch (caught) {
      setDeleteError(String(caught));
    } finally {
      setDeletePending(false);
    }
  };
  const [error,setError] = useState<string | null>(null);
  const { control, handleSubmit, reset, watch } = useForm({
    defaultValues: { name: "", description: "" },
  });
  const name = watch("name");
  const duplicate=(projects.data ?? []).some(project => project.project_id!==editing?.project_id && project.name.normalize("NFKC").toLowerCase()===name.trim().normalize("NFKC").toLowerCase());
  const create = handleSubmit(async (values) => {
    setError(null);
    try {
    if (editing) {
      await requestJson(`/api/v1/projects/${editing.project_id}`,{method:"PATCH",body:JSON.stringify(values)});
      if (workspace.project?.project_id===editing.project_id) await workspace.openProject(editing.project_id);
    } else {
    await workspace.createProject(values.name.trim(), values.description.trim());
    }
    await projects.refetch();
    setOpen(false);
    reset();
    } catch(e) {setError(String(e));}
  });
  return (
    <Page
      title="Projects"
      description="Open a project, create an editable copy, or start with your own data. Runs keep their original input and configuration snapshots."
      actions={<Stack direction="row" gap={1}><Button variant="outlined" onClick={()=>setImporting(true)}>Import project</Button><Button startIcon={<AddRounded />} variant="contained" onClick={() => {setEditing(null);reset({name:"",description:""});setError(null);setOpen(true);}}>New project</Button></Stack>}
    >
      <ApiIssues error={workspace.error} onClear={workspace.clearError} />
      {initialization.error && <Alert severity="error">{String(initialization.error)}</Alert>}
      {initialization.data && !initialization.data.example_project_id && !initialization.data.example_job_id && <Alert severity="info">The offline example bundle is unavailable. You can create a project using your own data.</Alert>}
      {(initialization.isPending || projects.isPending) && <LinearProgress aria-label="Loading projects" />}
      {workspace.busy && <Stack gap={1}><Typography variant="body2" color="text.secondary">Opening or copying project…</Typography><LinearProgress aria-label="Preparing project" /></Stack>}
      {(initialization.data?.example_job_ids?.length ? initialization.data.example_job_ids : initialization.data?.example_job_id ? [initialization.data.example_job_id] : []).map(jobId => <JobMonitor key={jobId} jobId={jobId} sourceRevisionId={null} />)}
      <Stack gap={1.5}>
        {(projects.data ?? []).map((project) => <Card key={project.project_id} variant="outlined" sx={{ borderColor: workspace.project?.project_id === project.project_id ? "primary.main" : "divider" }}>
          <CardContent sx={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 2 }}><Typography variant="h6">{project.name}</Typography><Chip label={project.status === "results" ? "Results available" : project.status === "running" ? "Running" : project.status === "configured" ? "Configured" : "New"} /></CardContent>
          <CardActions><Button startIcon={<FolderOpenRounded />} disabled={workspace.busy || (project.status === "running" && !project.current_revision_id)} onClick={() => void workspace.openProject(project.project_id).then(() => navigate(project.status === "results" ? "/results" : "/environment"))}>Open</Button>
          <Button startIcon={<ContentCopyRounded />} disabled={workspace.busy || (project.status === "running" && !project.current_revision_id)} onClick={() => void workspace.duplicateProject(project.project_id).then(() => projects.refetch()).catch(() => undefined)}>Duplicate</Button>
          <Button onClick={()=>setFileProject(project)}>Files</Button>
          <Button disabled={workspace.busy || project.status === "running"} onClick={()=>{setEditing(project);reset({name:project.name,description:project.description});setError(null);setOpen(true);}}>Rename</Button>
          <Button color="error" disabled={workspace.busy || project.status === "running"} onClick={() => { setDeleting(project); setDeleteError(null); }}>Delete</Button></CardActions>
        </Card>)}
      </Stack>
      <Typography variant="caption" color="text.secondary" sx={{ mt: 2 }}>Project folder: {initialization.data?.directory ?? "Loading…"}</Typography>
      <Dialog open={open} onClose={() => setOpen(false)} fullWidth maxWidth="sm">
        <DialogTitle>{editing ? "Rename project" : "New project"}</DialogTitle>
        <DialogContent sx={{ pt: "12px !important" }}>
          <Stack gap={2}>
            <Controller name="name" control={control} rules={{ required: true, maxLength: 200 }} render={({ field }) => <TextField {...field} autoFocus label="Project name" required error={duplicate} helperText={duplicate ? "A project with this name already exists." : "Used as the folder name. Names must be unique."} inputProps={{ maxLength: 200 }} />} />
            {error && <Alert severity="error">{error}</Alert>}
            <Controller name="description" control={control} rules={{ maxLength: 4000 }} render={({ field }) => <TextField {...field} label="Notes" multiline minRows={3} inputProps={{ maxLength: 4000 }} />} />
          </Stack>
        </DialogContent>
        <DialogActions><Button onClick={() => setOpen(false)}>Cancel</Button><Button variant="contained" disabled={!name.trim() || duplicate} onClick={() => void create()}>{editing ? "Save name" : "Create"}</Button></DialogActions>
      </Dialog>
      <Dialog open={Boolean(deleting)} onClose={() => { if (!deletePending) setDeleting(null); }} fullWidth maxWidth="sm" aria-labelledby="delete-project-title" aria-describedby="delete-project-description">
        <DialogTitle id="delete-project-title">Delete project?</DialogTitle>
        <DialogContent>
          <Stack gap={2}>
            <Typography fontWeight={700}>{deleting?.name}</Typography>
            <Typography id="delete-project-description">This removes the project from the list and moves its folder to the workspace trash. The project’s data, settings and results are retained there. Other projects remain independent.</Typography>
            {deleteError && <Alert severity="error">{deleteError}</Alert>}
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button autoFocus disabled={deletePending} onClick={() => setDeleting(null)}>Cancel</Button>
          <Button color="error" variant="contained" disabled={deletePending} onClick={() => void confirmDelete()}>{deletePending ? "Deleting…" : "Delete project"}</Button>
        </DialogActions>
      </Dialog>
      {fileProject && <ProjectFiles project={fileProject} onClose={()=>setFileProject(null)} />}
      {importing && <ImportProject onClose={()=>setImporting(false)} onImported={()=>void projects.refetch()} />}
    </Page>
  );
}
