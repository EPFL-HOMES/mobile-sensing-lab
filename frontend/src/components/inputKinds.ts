import type { InputDescriptor } from "../api/generated";

const odRoles: ReadonlyArray<InputDescriptor["role"]> = ["demand", "feature", "population", "weight"];
const odColumns = ["origin_cell_id", "destination_cell_id", "weight"] as const;

export function isOdDistributionInput(input: InputDescriptor): boolean {
  return ["csv", "parquet"].includes(input.format) && odRoles.includes(input.role)
    && odColumns.every((column) => input.columns.includes(column));
}
