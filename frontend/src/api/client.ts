import type { ApiPath } from "./generated";

export interface ApiIssue {
  severity: "error" | "warning" | "info";
  code: string;
  field_path: string;
  message: string;
  corrective_action?: string | null;
  dataset_id?: string;
  source_row?: string | number;
  task_id?: string;
}

export interface ApiErrorBody {
  code: string;
  message: string;
  issues: ApiIssue[];
  request_id: string;
}

export class ApiError extends Error {
  readonly status: number;
  readonly body: ApiErrorBody;

  constructor(status: number, body: ApiErrorBody) {
    super(body.message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

export type { ProjectRecord, RevisionRecord, JobSnapshot, JobSubmission } from "./generated";
export type JobStatus = import("./generated").JobSnapshot["status"];

export async function requestJson<T>(
  path: ApiPath | string,
  options: RequestInit = {},
): Promise<T> {
  let projectId: string | null = null;
  try { projectId = JSON.parse(localStorage.getItem("mobile-sensing-workbench@1") ?? "{}").activeProjectId ?? null; } catch { /* No project selected. */ }
  const response = await fetch(path, {
    ...options,
    headers: {
      Accept: "application/json",
      ...(projectId ? { "X-Project-ID": projectId } : {}),
      ...(options.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
      ...options.headers,
    },
  });
  if (!response.ok) {
    let body: ApiErrorBody;
    try {
      body = (await response.json()) as ApiErrorBody;
    } catch {
      body = {
        code: "HTTP_ERROR",
        message: `Request failed with HTTP ${response.status}`,
        issues: [],
        request_id: response.headers.get("X-Request-ID") ?? "unknown",
      };
    }
    throw new ApiError(response.status, body);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export function jobHeaders(projectId: string, operation: string): HeadersInit {
  return {
    "X-Project-ID": projectId,
    "Idempotency-Key": `${operation}-${crypto.randomUUID()}`,
  };
}
