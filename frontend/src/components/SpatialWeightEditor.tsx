import { Button, MenuItem, Stack, TextField, Typography } from "@mui/material";

type WeightedFeature = { feature: string; weight: number };
type SavedWeightedFeature = { feature: string; weight?: number };

interface Props {
  label: string;
  features: string[];
  legacyFeature: string;
  value?: readonly SavedWeightedFeature[];
  disabled?: boolean;
  onChange: (value: WeightedFeature[]) => void;
}

export function SpatialWeightEditor({ label, features, legacyFeature, value, disabled, onChange }: Props) {
  const rows: WeightedFeature[] = value?.length ? value.map((row) => ({ feature: row.feature, weight: row.weight ?? 1 })) : [{ feature: legacyFeature || "uniform", weight: 1 }];
  const update = (index: number, patch: Partial<WeightedFeature>) =>
    onChange(rows.map((row, i) => i === index ? { ...row, ...patch } : row));
  return <Stack gap={1.25}>
    <Typography fontWeight={700}>{label}</Typography>
    <Typography variant="body2" color="text.secondary">Each feature is normalized over the full prepared grid first. The normalized layers are combined using these coefficients; routing eligibility is applied afterward.</Typography>
    {rows.map((row, index) => <Stack direction={{ xs: "column", sm: "row" }} gap={1} key={`${index}-${row.feature}`}>
      <TextField select fullWidth size="small" label="Spatial feature" value={row.feature} disabled={disabled} onChange={(event) => update(index, { feature: event.target.value })}>
        {features.map((feature) => <MenuItem key={feature} value={feature}>{feature === "uniform" ? "Uniform" : feature}</MenuItem>)}
      </TextField>
      <TextField size="small" label="Mixture coefficient" type="number" value={row.weight} disabled={disabled} onChange={(event) => update(index, { weight: Number(event.target.value) })} slotProps={{ htmlInput: { min: 0, step: 0.1 } }} sx={{ minWidth: 180 }} />
      <Button disabled={disabled || rows.length === 1} onClick={() => onChange(rows.filter((_, i) => i !== index))}>Remove</Button>
    </Stack>)}
    <Button disabled={disabled || !features.length} variant="outlined" sx={{ alignSelf: "flex-start" }} onClick={() => onChange([...rows, { feature: features[0] ?? "uniform", weight: 1 }])}>Add feature</Button>
  </Stack>;
}
