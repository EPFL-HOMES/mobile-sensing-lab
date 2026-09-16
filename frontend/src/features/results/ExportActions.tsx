import DownloadRounded from "@mui/icons-material/DownloadRounded";
import { Button, Stack } from "@mui/material";
import { useState } from "react";
import { useWorkspace } from "../../app/workspace";
import { JobMonitor } from "../../components/JobMonitor";

export function ExportActions({
  resourceId,
  table,
  sourceRevisionId,
}: {
  resourceId: string;
  table: string;
  sourceRevisionId?: string | null;
}) {
  const workspace = useWorkspace();
  const [jobId, setJobId] = useState<string | null>(null);
  const submit = async (format: "csv" | "parquet") => {
    const job = await workspace.submitJob(
      "/api/v1/exports",
      `export-${table}-${format}`,
      { resource_id: resourceId, table, format },
      sourceRevisionId,
    );
    setJobId(job.job_id);
  };
  return (
    <Stack gap={1}>
      <Stack direction="row" gap={1} flexWrap="wrap">
        <Button size="small" startIcon={<DownloadRounded />} onClick={() => void submit("csv")}>Export table CSV</Button>
        <Button size="small" startIcon={<DownloadRounded />} onClick={() => void submit("parquet")}>Export table Parquet</Button>
      </Stack>
      {jobId && <JobMonitor jobId={jobId} sourceRevisionId={sourceRevisionId ?? null} />}
    </Stack>
  );
}
