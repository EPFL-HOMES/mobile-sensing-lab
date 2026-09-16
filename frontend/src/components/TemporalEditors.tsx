import { Alert, Button, MenuItem, Stack, TextField, Typography } from "@mui/material";
import type { DemandEditor, SupplyEditor } from "../api/generated";

export function DemandTimeEditor({ value, onChange, input }: { value: DemandEditor; onChange: (patch: Partial<DemandEditor>) => void; input: React.ReactNode }) {
  return <Stack gap={2}>
    <TextField select label="Daily demand distribution" value={value.temporal_mode === "rates" ? "" : value.temporal_mode ?? "window"} SelectProps={{ displayEmpty: value.temporal_mode === "rates", renderValue: value.temporal_mode === "rates" ? () => "Saved rate profile" : undefined }} InputLabelProps={{ shrink: true }} onChange={(e) => onChange({ temporal_mode: e.target.value as DemandEditor["temporal_mode"], release_mode: "uniform", time_profile_input: null, time_profile: e.target.value === "window" ? [] : [{ start_time: "00:00", end_time: "24:00", value: 1 }] })}>
      <MenuItem value="window">Uniform within a window / release at start</MenuItem><MenuItem value="shares">Time intervals with relative shares</MenuItem>
    </TextField>
    {value.temporal_mode === "rates" && <Alert severity="info">This project retains a saved rate profile{value.time_profile_input ? " from a registered input" : ` with ${value.time_profile?.length ?? 0} intervals`}. Its values and random task counts are unchanged. Choose a distribution above to replace it and set a daily total.</Alert>}
    {value.temporal_mode === "shares" && <>
      <Typography color="text.secondary">{value.temporal_mode === "shares" ? "Shares are normalized over the supplied intervals. Fixed totals preserve the exact task count; expected totals retain random counts." : "Each rate is integrated over its interval. Counts follow Poisson distributions; no separate daily total is used."} Gaps have zero demand; overlapping intervals are rejected.</Typography>
      <TextField select label="Temporal profile source" value={value.time_profile_input != null ? "input" : "inline"} onChange={(e) => onChange({ time_profile_input: e.target.value === "input" ? "" : null, time_profile: e.target.value === "input" ? [] : [{ start_time: "00:00", end_time: "24:00", value: 1 }] })}><MenuItem value="inline">Edit intervals</MenuItem><MenuItem value="input">Registered CSV / Parquet</MenuItem></TextField>
      {value.time_profile_input != null ? <>{input}<Typography>Columns: start_time, end_time, value. Times use HH:MM; 24:00 is allowed only as the day boundary.</Typography></> : <>
        {value.time_profile?.map((row, i) => <Stack key={i} direction="row" gap={1} alignItems="center">
          {(["start_time", "end_time", "value"] as const).map((field) => <TextField key={field} size="small" label={field === "value" ? (value.temporal_mode === "shares" ? "Relative share" : "Tasks / hour") : field === "start_time" ? "Interval starts" : "Interval ends"} type={field === "value" ? "number" : "text"} value={row[field]} onChange={(e) => onChange({ time_profile: value.time_profile?.map((old, j) => i === j ? { ...old, [field]: field === "value" ? Number(e.target.value) : e.target.value } : old) })} />)}
          <Button aria-label={`Remove interval ${i+1}`} onClick={() => onChange({ time_profile: value.time_profile?.filter((_, j) => j !== i) })}>Remove</Button>
        </Stack>)}
        <Button sx={{ alignSelf: "flex-start" }} variant="outlined" onClick={() => onChange({ time_profile: [...(value.time_profile ?? []), { start_time: "00:00", end_time: "24:00", value: 1 }] })}>Add interval</Button>
      </>}
    </>}
  </Stack>;
}

export function ShiftGroupsEditor({ value, onChange }: { value: SupplyEditor; onChange: (patch: Partial<SupplyEditor>) => void }) {
  return <Stack gap={2}>
    <TextField select label="Shift configuration" value={value.shift_groups?.length ? "groups" : "single"} onChange={(e) => onChange({ shift_groups: e.target.value === "single" ? [] : [{ name: "Shift 1", count: value.fleet_size ?? 1, start_time: value.operating_start ?? "08:00", latest_start: value.activation === "uniform_bounded" ? value.latest_start ?? "08:00" : value.operating_start ?? "08:00", work_hours: value.work_hours ?? 8, day_offset: 0 }] })}><MenuItem value="single">One common shift rule</MenuItem><MenuItem value="groups">Explicit groups of physical vehicles</MenuItem></TextField>
    {!!value.shift_groups?.length && <>
      <Typography color="text.secondary">Group counts must sum to fleet size. Each vehicle has one shift. Earlier-day starts create explicit carry-in vehicles; add warm-up demand in Simulation when their pre-day activity is needed.</Typography>
      {value.shift_groups.map((row, i) => <Stack gap={1} key={i} sx={{ p: 2, border: "1px solid", borderColor: "divider", borderRadius: 1 }}>
        <div className="form-grid">{(["name", "count", "start_time", "latest_start", "work_hours"] as const).map((field) => <TextField key={field} size="small" label={{ name: "Shift name", count: "Vehicles in group", start_time: "Earliest start", latest_start: "Latest start", work_hours: "Work duration (hours)" }[field]} type={field === "count" || field === "work_hours" ? "number" : "text"} value={row[field]} onChange={(e) => onChange({ shift_groups: value.shift_groups?.map((old, j) => i === j ? { ...old, [field]: field === "count" || field === "work_hours" ? Number(e.target.value) : e.target.value } : old) })} />)}
          <TextField size="small" select label="Start day" value={row.day_offset ?? 0} onChange={(e) => onChange({ shift_groups: value.shift_groups?.map((old, j) => i === j ? { ...old, day_offset: Number(e.target.value) } : old) })}><MenuItem value={0}>Observation day</MenuItem><MenuItem value={-1}>Previous day</MenuItem><MenuItem value={-2}>Two days before</MenuItem></TextField>
        </div><Button onClick={() => onChange({ shift_groups: value.shift_groups?.filter((_, j) => i !== j) })}>Remove shift</Button>
      </Stack>)}
      <Button sx={{ alignSelf: "flex-start" }} variant="outlined" onClick={() => onChange({ shift_groups: [...(value.shift_groups ?? []), { name: `Shift ${(value.shift_groups?.length ?? 0)+1}`, count: 1, start_time: "08:00", latest_start: "08:00", work_hours: 8, day_offset: 0 }] })}>Add shift group</Button>
    </>}
  </Stack>;
}
