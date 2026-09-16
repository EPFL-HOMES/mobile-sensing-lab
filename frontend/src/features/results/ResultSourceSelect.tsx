import { FormControl, InputLabel, MenuItem, Select } from "@mui/material";
import type { JobSnapshot } from "../../api/client";
import { artifactFromJob } from "./types";

export function ResultSourceSelect({
  label,
  jobs,
  value,
  onChange,
}: {
  label: string;
  jobs: JobSnapshot[];
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <FormControl size="small" sx={{ minWidth: 260 }}>
      <InputLabel>{label}</InputLabel>
      <Select inputProps={{ "aria-label": label }} label={label} value={value} onChange={(event) => onChange(event.target.value)}>
        {jobs.map((job) => {
          const artifact = artifactFromJob(job);
          if (!artifact) return null;
          return <MenuItem value={artifact.artifact_id} key={job.job_id}>{String(job.result?.name ?? `${job.kind} · ${new Date(job.created_at_utc).toLocaleString()}`)}</MenuItem>;
        })}
      </Select>
    </FormControl>
  );
}
