import { requestJson } from "../../api/client";
import type { MatrixSliceValue, ResultPageValue } from "./types";
export function queryString(values: Record<string, string | number | null | undefined>) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value !== null && value !== undefined && value !== "") query.set(key, String(value));
  }
  return query.toString();
}

export function resultTable<T = Record<string, unknown>>(
  resourceId: string,
  table: string,
  filters: Record<string, string | number | null | undefined> = {},
) {
  const query = queryString({ page_size: 1000, ...filters });
  return requestJson<ResultPageValue<T>>(`/api/v1/results/${resourceId}/${table}?${query}`);
}

export function matrixQuery(body: Record<string, unknown>) {
  return requestJson<MatrixSliceValue>("/api/v1/matrix-queries", {
    method: "POST",
    body: JSON.stringify(body),
  });
}
