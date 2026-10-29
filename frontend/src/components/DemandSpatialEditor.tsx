import { Alert, MenuItem, Stack, TextField, Typography } from "@mui/material";
import type { DemandEditor, InputDescriptor } from "../api/generated";
import { FormSection } from "./FormSection";
import { SpatialWeightEditor } from "./SpatialWeightEditor";
import { isOdDistributionInput } from "./inputKinds";

interface Props {
  demand: DemandEditor;
  inputs?: readonly InputDescriptor[];
  features: string[];
  prepared: boolean;
  readOnly?: boolean;
  inputsError?: string;
  onChange: (patch: Partial<DemandEditor>) => void;
}

const inputLabel = (input: InputDescriptor) => `${input.name} — ${input.original_filename}`;

export function DemandSpatialEditor({ demand, inputs, features, prepared, readOnly = false, inputsError, onChange }: Props) {
  const isOd = demand.task_type === "od";
  const selectedId = demand.od_distribution_input ?? "";
  const selected = inputs?.find((input) => input.input_id === selectedId);
  const compatible = inputs?.filter(isOdDistributionInput) ?? [];
  const invalidSelected = Boolean(selectedId && inputs !== undefined && (!selected || !isOdDistributionInput(selected)));
  const selectedLabel = selected ? inputLabel(selected) : `${inputs === undefined ? "Loading input" : "Unavailable input"}: ${selectedId}`;
  const featureEditor = (label: string, destination = false) => <SpatialWeightEditor
    label={label}
    features={features}
    legacyFeature={(destination ? demand.destination_feature : demand.spatial_feature) ?? "uniform"}
    value={destination ? demand.destination_spatial_weights : demand.spatial_weights}
    disabled={readOnly || !prepared}
    onChange={(value) => onChange(destination ? { destination_spatial_weights: value } : { spatial_weights: value })}
  />;

  return <FormSection title="Spatial distribution">
    {isOd && <>
      <TextField select label="Demand spatial source" value={selectedId} disabled={readOnly || inputs === undefined}
        slotProps={{ select: { displayEmpty: true }, inputLabel: { shrink: true } }}
        error={invalidSelected || Boolean(inputsError)} onChange={(event) => onChange({ od_distribution_input: event.target.value || null })}>
        <MenuItem value="">Independent origin and destination distributions</MenuItem>
        {compatible.map((input) => <MenuItem key={input.input_id} value={input.input_id}>{inputLabel(input)}</MenuItem>)}
        {selectedId && !compatible.some((input) => input.input_id === selectedId) &&
          <MenuItem value={selectedId} disabled>{selectedLabel}</MenuItem>}
      </TextField>
      {inputsError ? <Alert severity="error">Could not load registered inputs: {inputsError}. The saved spatial source has not changed.</Alert>
        : inputs === undefined ? <Typography role="status" variant="body2">Loading registered OD inputs…</Typography>
          : invalidSelected ? <Alert severity="error">
            {selected ? `The selected input “${selected.name}” is not a compatible OD distribution.` : `The selected OD input “${selectedId}” is unavailable.`}
            {" "}Choose a compatible OD file or explicitly select independent distributions. The saved input reference is retained.
          </Alert> : null}
      {selectedId ? <Stack gap={1}>
        {selected && <Typography variant="body2">Selected OD file: <strong>{selected.original_filename}</strong></Typography>}
        <Typography variant="body2" color="text.secondary">Origin and destination cells are sampled together from the selected file, in proportion to each pair’s weight. Task volume and release times are configured separately.</Typography>
      </Stack> : <Typography variant="body2" color="text.secondary">Origins and destinations are sampled independently from their prepared grid features.</Typography>}
      <Typography variant="body2" color="text.secondary">OD files require columns: origin_cell_id, destination_cell_id, weight. Register a demand input, then select it here.</Typography>
    </>}
    {(!isOd || !selectedId) && <Stack gap={2}>
      {featureEditor(isOd ? "Origin spatial distribution" : "Task spatial distribution")}
      {isOd ? featureEditor("Destination spatial distribution", true) :
        demand.task_type === "location" && <TextField select label="Location reachability" value={demand.location_condition ?? "all_resolved"}
          disabled={readOnly} onChange={(event) => onChange({ location_condition: event.target.value as DemandEditor["location_condition"] })}>
          <MenuItem value="all_resolved">All resolved locations</MenuItem>
          <MenuItem value="depot_roundtrip">Require a route from and back to the depot</MenuItem>
        </TextField>}
    </Stack>}
  </FormSection>;
}
