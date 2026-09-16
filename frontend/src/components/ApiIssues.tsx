import ErrorOutlineRounded from "@mui/icons-material/ErrorOutlineRounded";
import { Alert, AlertTitle, Button, List, ListItem, ListItemText } from "@mui/material";
import type { ApiError } from "../api/client";

export function ApiIssues({ error, onClear }: { error: ApiError | null; onClear?: () => void }) {
  if (!error) return null;
  if (!error.body) return <Alert severity="error" onClose={onClear}>{error.message || "The request could not be completed. Reconnect and retry."}</Alert>;
  return (
    <Alert
      severity="error"
      icon={<ErrorOutlineRounded />}
      action={onClear ? <Button onClick={onClear}>Dismiss</Button> : undefined}
      sx={{ mb: 2 }}
      data-testid="api-error"
    >
      <AlertTitle>{error.body.code}</AlertTitle>
      {error.body.message}
      {error.body.issues.length > 0 && (
        <List dense aria-label="Validation issues">
          {error.body.issues.map((issue, index) => (
            <ListItem key={`${issue.code}-${issue.field_path}-${index}`} disableGutters>
              <ListItemText
                primary={
                  <a href={`#field-${issue.field_path.replace(/^body\./, "").replaceAll(".", "-")}`}>
                    {issue.field_path || "Request"}: {issue.message}
                  </a>
                }
                secondary={issue.corrective_action}
              />
            </ListItem>
          ))}
        </List>
      )}
    </Alert>
  );
}
