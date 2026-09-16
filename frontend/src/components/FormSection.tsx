import { Box, Stack, Typography } from "@mui/material";
import type { ReactNode } from "react";

export function FormSection({ title, description, children, action }: {
  title: string; description?: string; children: ReactNode; action?: ReactNode;
}) {
  return <Box component="section" className="form-section">
    <Stack direction="row" justifyContent="space-between" alignItems="flex-start" gap={2} className="section-heading">
      <Box><Typography component="h2" variant="subtitle1" fontWeight={700}>{title}</Typography>
        {description && <Typography variant="body2" color="text.secondary">{description}</Typography>}</Box>{action}
    </Stack><Stack gap={2}>{children}</Stack>
  </Box>;
}

export function OptionHint({ label, hint }: { label: string; hint: string }) {
  return <span>{label} <Typography component="span" variant="body2" color="text.secondary">({hint})</Typography></span>;
}
