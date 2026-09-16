import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { WorkspaceProvider, useWorkspace } from "./workspace";
import fixture from "../test/studioFixture.json";

function Probe() {
  const workspace = useWorkspace();
  return <><div>{workspace.project?.name}</div><div>{workspace.draft.authoring?.read_only ? "Reference settings" : "Editable settings"}</div>
    <button onClick={() => workspace.updateDraft(draft => { draft.authoring!.fleets![0].demand!.task_volume = 123; return draft; })}>Edit</button>
    <button onClick={() => void workspace.openProject("b")}>Open reference</button></>;
}

it("commits a new project and migrated configuration atomically without copying the previous dirty draft", async () => {
  localStorage.clear();
  localStorage.setItem("mobile-sensing-workbench@1", JSON.stringify({ activeProjectId: "a", trackedJobIds: [], jobRevisionIds: {} }));
  const projects = [{ project_id: "a", name: "Editable project", current_revision_id: "ra" }, { project_id: "b", name: "Reference project", current_revision_id: "rb" }];
  const reply = (value: unknown) => new Response(JSON.stringify(value), { headers: { "Content-Type": "application/json" } });
  let finishMigration!: (response: Response) => void;
  const migration = new Promise<Response>(resolve => { finishMigration = resolve; });
  const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async input => {
    const path = String(input);
    if (path === "/api/v1/projects") return reply(projects);
    if (path === "/api/v1/projects/a") return reply(projects[0]);
    if (path === "/api/v1/projects/b") return reply(projects[1]);
    if (path.endsWith("/revisions/ra")) return reply({ revision_id: "ra", payload: { ...fixture.config, schema_version: "3.4" } });
    if (path.endsWith("/revisions/rb")) return reply({ revision_id: "rb", payload: { ...fixture.config, schema_version: "3.1" } });
    if (path.endsWith("/b/migration")) return (await migration).clone();
    throw new Error(path);
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><WorkspaceProvider><Probe /></WorkspaceProvider></QueryClientProvider>);
  const user = userEvent.setup();
  await screen.findByText("Editable project");
  await user.click(screen.getByText("Edit"));
  await waitFor(() => expect(localStorage.getItem("mobile-sensing-draft:a")).toContain('"task_volume":123'));
  await user.click(screen.getByText("Open reference"));
  await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/b/migration"))).toBe(true));
  expect(screen.getByText("Editable project")).toBeInTheDocument();
  expect(localStorage.getItem("mobile-sensing-draft:b")).toBeNull();
  await act(async () => finishMigration(reply({ config: { ...fixture.config, schema_version: "3.4", read_only: true }, notices: [] })));
  await screen.findByText("Reference project");
  expect(screen.getByText("Reference settings")).toBeInTheDocument();
  expect(localStorage.getItem("mobile-sensing-draft:b")).toBeNull();
});
