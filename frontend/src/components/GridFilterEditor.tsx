import { Alert, Button, MenuItem, Stack, TextField, Typography } from "@mui/material";
import { useQuery } from "@tanstack/react-query";
import type { EnvironmentResult, GridFilter, GridFilterPreview, GridFilterRule } from "../api/generated";
import { requestJson } from "../api/client";

type Rule = { kind: GridFilterRule["kind"]; feature: string | null; logic: "any" | "must" };

function filterRows(value?: GridFilter): Rule[] {
  if (value?.rules?.length) return value.rules.map(rule => ({
    kind: rule.kind, feature: rule.feature ?? null, logic: rule.logic ?? "any",
  }));
  const rows: Rule[] = (value?.features ?? []).map(feature => ({
    kind: "spatial_feature", feature, logic: value?.match === "all" ? "must" : "any",
  }));
  if (value?.require_road_intersection) rows.push({ kind: "road_network", feature: null, logic: "must" });
  return rows;
}

const ruleKey = (rule: Rule) => rule.kind === "road_network" ? "road_network" : `spatial_feature:${rule.feature}`;
const ruleName = (rule: Rule) => rule.kind === "road_network" ? "Road network" : rule.feature ?? "Missing spatial feature";

export function gridFilterLabel(value?: GridFilter) {
  const rows = filterRows(value);
  const condition = (rule: Rule) => rule.kind === "road_network" ? "Road network" : `${ruleName(rule)} > 0`;
  const must = rows.filter(row => row.logic === "must").map(condition);
  const any = rows.filter(row => row.logic === "any").map(condition);
  return [must.length ? `Must include (${must.join(" AND ")})` : "", any.length ? `Any of (${any.join(" OR ")})` : ""]
    .filter(Boolean).join(" AND ") || "All prepared cells";
}

export function GridFilterEditor({ prepared, value, disabled, onChange }: {
  prepared?: EnvironmentResult; value?: GridFilter; disabled: boolean; onChange: (value: GridFilter) => void;
}) {
  const selection = value ?? { rules: [], features: [], match: "any", require_road_intersection: false };
  const rows = filterRows(selection);
  const names = [...new Set(prepared?.feature_names ?? [])].filter(name => name !== "uniform");
  const choices: Rule[] = [
    ...names.map(feature => ({ kind: "spatial_feature" as const, feature, logic: "any" as const })),
    { kind: "road_network", feature: null, logic: "any" },
  ];
  const selectedKeys = new Set(rows.map(ruleKey));
  const nextChoice = choices.find(choice => !selectedKeys.has(ruleKey(choice)));
  const locked = disabled || !prepared;
  const save = (rules: Rule[]) => onChange({ rules, features: [], match: "any", require_road_intersection: false });
  const replace = (index: number, row: Rule) => save(rows.map((existing, i) => i === index ? row : existing));
  const anyRows = rows.filter(row => row.logic === "any");
  const requiresRoad = rows.some(row => row.kind === "road_network" && row.logic === "must")
    || (anyRows.length === 1 && anyRows[0].kind === "road_network");
  const preview = useQuery({
    queryKey: ["grid-filter-preview", prepared?.artifact.artifact_id, prepared?.artifact.content_hash, prepared?.features.artifact_id, prepared?.features.content_hash, selection], enabled: Boolean(prepared), retry: false,
    queryFn: () => requestJson<GridFilterPreview>("/api/v1/workbench/grid-filter/preview", { method: "POST", body: JSON.stringify({ prepared_environment: prepared, grid_filter: selection }) }),
  });
  return <Stack gap={1.5}>
    <Typography variant="subtitle1">Grid filter</Typography>
    <Typography variant="body2" color="text.secondary">Retain cells satisfying every “Must include” row and at least one “Any of (OR)” row. An empty group adds no restriction. With no rows, all prepared cells are retained.</Typography>
    {!prepared && <Typography variant="body2">Prepare the environment to enable filtering.</Typography>}
    {prepared && !names.length && <Typography variant="body2">Road network is available now. Add spatial features to the environment to filter by geographical values.</Typography>}
    {rows.map((row, index) => {
      const missing = Boolean(prepared && row.kind === "spatial_feature" && !names.includes(row.feature ?? ""));
      const options = choices.filter(choice => ruleKey(choice) === ruleKey(row) || !selectedKeys.has(ruleKey(choice)));
      return <Stack key={`${index}-${ruleKey(row)}`} direction={{ xs: "column", sm: "row" }} gap={1} alignItems="flex-start">
        <TextField select fullWidth size="small" label={`Filter feature ${index + 1}`} value={ruleKey(row)} disabled={locked}
          error={missing} helperText={missing ? "This feature is absent from the prepared environment. Choose another feature or remove this row." : undefined}
          onChange={event => {
            const chosen = choices.find(choice => ruleKey(choice) === event.target.value);
            if (chosen) replace(index, { ...chosen, logic: row.logic });
          }}>
          {options.map(choice => <MenuItem key={ruleKey(choice)} value={ruleKey(choice)}>{ruleName(choice)}</MenuItem>)}
          {!options.some(choice => ruleKey(choice) === ruleKey(row)) &&
            <MenuItem value={ruleKey(row)} disabled>{ruleName(row)}{missing ? " (not in prepared environment)" : ""}</MenuItem>}
        </TextField>
        <TextField select size="small" label={`Filter logic ${index + 1}`} value={row.logic} disabled={locked} sx={{ minWidth: 180 }}
          onChange={event => replace(index, { ...row, logic: event.target.value as Rule["logic"] })}>
          <MenuItem value="any">Any of (OR)</MenuItem><MenuItem value="must">Must include</MenuItem>
        </TextField>
        <Button disabled={locked} aria-label={`Remove filter row ${index + 1}`} onClick={() => save(rows.filter((_, i) => i !== index))}>Remove</Button>
      </Stack>;
    })}
    <Button variant="outlined" sx={{ alignSelf: "flex-start" }} disabled={locked || !nextChoice}
      onClick={() => { if (nextChoice) save([...rows, nextChoice]); }}>Add filter row</Button>
    <Typography variant="body2" color="text.secondary">Spatial features require a value greater than zero. Population &gt; 0 identifies recorded residents; residential geometry is a proxy. Road network requires positive-length overlap with a prepared road; point contacts do not count.</Typography>
    <Typography variant="body2" color="text.secondary">The filter applies to generated demand, generated initial locations, sensing and downstream portfolios. Routing keeps the complete road network.</Typography>
    {preview.isFetching && <Typography variant="body2">Checking retained cells…</Typography>}
    {!preview.isFetching && preview.data && <Typography variant="body2">{preview.data.retained_cell_count.toLocaleString()} / {preview.data.total_cell_count.toLocaleString()} cells retained.{!requiresRoad && " Sensing and coverage further require a positive-length road intersection."}</Typography>}
    {preview.error && <Alert severity="error">{preview.error.message}</Alert>}
  </Stack>;
}
