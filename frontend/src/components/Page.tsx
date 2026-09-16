import { Alert, Box, Button, Stack, Typography, ThemeProvider, createTheme, useTheme } from "@mui/material";
import type { ReactNode } from "react";
import { useMemo } from "react";
import { useLocation } from "react-router-dom";
import { useWorkspace } from "../app/workspace";

export function Page({
  title,
  description,
  actions,
  children,
}: {
  title: string;
  description?: string;
  actions?: ReactNode;
  children: ReactNode;
}) {
  const workspace = useWorkspace();
  const path = useLocation().pathname;
  const readOnly = Boolean(workspace.draft.authoring?.read_only && ["/environment", "/fleets", "/simulation", "/portfolio"].includes(path));
  const theme = useTheme();
  const lockedTheme = useMemo(() => createTheme(theme, { components: {
    MuiTextField: { defaultProps: { disabled: true } }, MuiCheckbox: { defaultProps: { disabled: true } },
    MuiSwitch: { defaultProps: { disabled: true } }, MuiButton: { defaultProps: { disabled: true } },
  } }), [theme]);
  return (
    <Box component="main" className="page">
      <Stack direction="row" justifyContent="space-between" alignItems="flex-start" gap={2}>
        <Box>
          <Typography component="h1" variant="h4">
            {title}
          </Typography>
          {description && (
            <Typography color="text.secondary" sx={{ mt: 0.5, maxWidth: 760 }}>
              {description}
            </Typography>
          )}
        </Box>
        {actions}
      </Stack>
      {readOnly && <Alert severity="info" sx={{ mt: 2 }} action={<Button onClick={() => void workspace.duplicateProject()}>Create editable copy</Button>}>Reference example. Settings are preserved with its verified results.</Alert>}
      <Box sx={{ mt: 3 }}>{readOnly ? <ThemeProvider theme={lockedTheme}>{children}</ThemeProvider> : children}</Box>
    </Box>
  );
}
