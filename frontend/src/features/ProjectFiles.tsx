import { Alert, Button, Dialog, DialogActions, DialogContent, DialogTitle, MenuItem, Stack, Table, TableBody, TableCell, TableHead, TablePagination, TableRow, TextField, Typography } from "@mui/material";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useState } from "react";
import { requestJson } from "../api/client";
import type { ProjectRecord, ProjectFilesView, JobSnapshot, JobSubmission } from "../api/generated";
import { JobMonitor } from "../components/JobMonitor";

export function ProjectFiles({ project, onClose }: { project: ProjectRecord; onClose: () => void }) {
  const [category,setCategory] = useState("");
  const [page,setPage] = useState(0);
  const [error,setError] = useState<string | null>(null);
  const client=useQueryClient();
  const files=useQuery({queryKey:["project-files",project.project_id],queryFn:() => requestJson<ProjectFilesView>(`/api/v1/projects/${project.project_id}/files`)});
  const jobs=useQuery({queryKey:["project-file-jobs",project.project_id],queryFn:() => requestJson<JobSnapshot[]>(`/api/v1/jobs?project_id=${project.project_id}`),refetchInterval:2000});
  const latest=jobs.data?.filter(job => ["project_files","project_export","project_import"].includes(job.kind)).at(-1);
  const rows=(files.data?.files ?? []).filter(file => !category || file.category===category);
  const ready=useCallback(() => {void client.invalidateQueries({queryKey:["project-files",project.project_id]});},[client,project.project_id]);
  const submit=async(archive: boolean) => {setError(null);try {await requestJson<JobSubmission>(`/api/v1/projects/${project.project_id}/package`,{method:"POST",body:JSON.stringify({archive})});await jobs.refetch();} catch(e){setError(String(e));}};
  return <Dialog open onClose={onClose} maxWidth="lg" fullWidth><DialogTitle>{project.name} · Files</DialogTitle><DialogContent><Stack gap={2}>
    <Typography variant="body2" sx={{overflowWrap:"anywhere"}}>{files.data?.directory}</Typography>
    <Typography variant="body2" color="text.secondary">This project owns its data, environment, settings and results. Copy the complete project folder, including its hidden metadata, or export a project ZIP to transfer it to another workspace.</Typography>
    <Stack direction="row" gap={1} flexWrap="wrap"><Button variant="outlined" onClick={() => void requestJson(`/api/v1/projects/${project.project_id}/open-directory`,{method:"POST"}).catch(e=>setError(String(e)))}>Open folder</Button><Button variant="outlined" onClick={() => void submit(false)}>Prepare summary files</Button><Button variant="contained" onClick={() => void submit(true)}>Export complete project</Button></Stack>
    {(error || files.error) && <Alert severity="error">{error ?? String(files.error)}</Alert>}
    {latest && <JobMonitor jobId={latest.job_id} sourceRevisionId={null} onCompleted={ready} />}
    <TextField disabled={false} select label="File category" value={category} onChange={e=>{setCategory(e.target.value);setPage(0);}}><MenuItem value="">All files</MenuItem>{[...new Set(files.data?.files.map(file=>file.category))].map(value=><MenuItem value={value} key={value}>{value}</MenuItem>)}</TextField>
    <Table size="small"><TableHead><TableRow><TableCell>File</TableCell><TableCell>Category</TableCell><TableCell>Location</TableCell><TableCell align="right">Size</TableCell></TableRow></TableHead><TableBody>{rows.slice(page*50,(page+1)*50).map(file=><TableRow key={file.file_id}><TableCell sx={{overflowWrap:"anywhere"}}><a href={`/api/v1/projects/${project.project_id}/files/${file.file_id}`} download>{file.name}</a></TableCell><TableCell>{file.category}</TableCell><TableCell>{file.shared ? "Shared data" : "Project folder"}</TableCell><TableCell align="right" sx={{whiteSpace:"nowrap"}}>{(file.size_bytes/1024).toLocaleString(undefined,{maximumFractionDigits:1})} KB</TableCell></TableRow>)}</TableBody></Table>
    <TablePagination component="div" count={rows.length} page={page} rowsPerPage={50} rowsPerPageOptions={[50]} onPageChange={(_,next)=>setPage(next)} />
  </Stack></DialogContent><DialogActions><Button onClick={onClose}>Close</Button></DialogActions></Dialog>;
}

export function ImportProject({onClose,onImported}:{onClose:()=>void;onImported:()=>void}) {
  const [name,setName]=useState("");
  const [file,setFile]=useState<File | null>(null);
  const [error,setError]=useState<string | null>(null);
  const [job,setJob]=useState<string | null>(null);
  const [uploading,setUploading]=useState(false);
  const submit=async()=>{if(!file)return;setUploading(true);setError(null);const data=new FormData();data.append("name",name.trim());data.append("file",file);try{const result=await requestJson<JobSubmission>("/api/v1/project-imports",{method:"POST",body:data});setJob(result.job_id);onImported();}catch(e){setError(String(e));}finally{setUploading(false);}};
  return <Dialog open onClose={onClose} fullWidth maxWidth="sm"><DialogTitle>Import complete project</DialogTitle><DialogContent><Stack gap={2} sx={{mt:1}}><TextField label="Imported project name" value={name} onChange={e=>setName(e.target.value)} disabled={!!job} helperText="The name must be unique in this workspace." /><Button component="label" variant="outlined" disabled={!!job || uploading}>{file?.name ?? "Choose project ZIP"}<input hidden type="file" accept=".zip" onChange={e=>setFile(e.target.files?.[0] ?? null)} /></Button>{error && <Alert severity="error">{error}</Alert>}{job && <JobMonitor jobId={job} sourceRevisionId={null} onCompleted={onImported} />}</Stack></DialogContent><DialogActions><Button onClick={onClose}>Close</Button><Button variant="contained" disabled={!name.trim() || !file || uploading || !!job} onClick={()=>void submit()}>{uploading ? "Uploading…" : "Import project"}</Button></DialogActions></Dialog>;
}
