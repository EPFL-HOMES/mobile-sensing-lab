import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import { RunManagement } from "./RunManagement";
import type { RunView } from "../api/generated";
import fixture from "../test/studioFixture.json";

beforeEach(() => { localStorage.clear(); vi.restoreAllMocks(); });
it("requires a named confirmation, preserves dependent analyses and offers recovery", async () => {
  let deleted = false;
  const fetch = vi.fn(async (input: RequestInfo | URL, options?: RequestInit) => {
    const url=String(input);
    if (options?.method === "DELETE") { deleted=true; return new Response(null,{status:204}); }
    if (options?.method === "POST") { deleted=false; return new Response(null,{status:204}); }
    const row = { run_id: fixture.run.run_id, name: fixture.run.name, dependent_analyses: ["Existing portfolio"], dependent_runs: [], deleted_at_utc: deleted ? "2026-09-15" : null };
    return new Response(JSON.stringify(url.includes("deletion-preview") ? row : deleted ? [row] : []), {status:200});
  });
  vi.stubGlobal("fetch",fetch);
  const client=new QueryClient({defaultOptions:{queries:{retry:false}}});
  render(<QueryClientProvider client={client}><MemoryRouter><RunManagement runs={[fixture.run as unknown as RunView]} projectId="project_1" /></MemoryRouter></QueryClientProvider>);
  fireEvent.click(screen.getByRole("button",{name:"Delete run"}));
  const dialog=screen.getByRole("dialog");
  expect(within(dialog).getByText(fixture.run.name)).toBeInTheDocument();
  await screen.findByText(/Existing portfolio/);
  expect(deleted).toBe(false);
  fireEvent.click(within(dialog).getByRole("button",{name:"Cancel"}));
  await waitFor(()=>expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(deleted).toBe(false);
  fireEvent.click(screen.getByRole("button",{name:"Delete run"}));
  await waitFor(()=>expect(within(screen.getByRole("dialog")).getByRole("button",{name:"Delete run"})).toBeEnabled());
  fireEvent.click(within(screen.getByRole("dialog")).getByRole("button",{name:"Delete run"}));
  await waitFor(()=>expect(deleted).toBe(true));
  fireEvent.click(await screen.findByText("Deleted runs (1)"));
  fireEvent.click(await screen.findByRole("button",{name:"Restore run"}));
  await waitFor(()=>expect(deleted).toBe(false));
});
