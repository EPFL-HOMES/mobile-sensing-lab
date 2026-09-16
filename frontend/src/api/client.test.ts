import { afterEach, expect, it, vi } from "vitest";
import { requestJson } from "./client";

afterEach(() => vi.restoreAllMocks());

it("accepts the empty response from a successful logical project deletion", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(null, { status: 204 }));
  await expect(requestJson<void>("/api/v1/projects/example", { method: "DELETE" })).resolves.toBeUndefined();
});
