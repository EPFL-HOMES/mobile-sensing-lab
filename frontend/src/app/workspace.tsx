import { useQueryClient } from "@tanstack/react-query";
import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, jobHeaders, requestJson } from "../api/client";
import type { JobSubmission, ProjectRecord, RevisionRecord } from "../api/client";
import { defaultDraft, fromProjectConfig } from "./model";
import type { ProjectConfig, MigrationView } from "../api/generated";
import type { WorkbenchDraft } from "./model";

const STORAGE_KEY = "mobile-sensing-workbench@1";

interface StoredState {
  activeProjectId: string | null;
  trackedJobIds: string[];
  jobRevisionIds: Record<string, string | null>;
}

interface WorkspaceValue {
  project: ProjectRecord | null;
  projects: ProjectRecord[];
  revision: RevisionRecord | null;
  draft: WorkbenchDraft;
  dirty: boolean;
  trackedJobIds: string[];
  jobRevisionIds: Record<string, string | null>;
  busy: boolean;
  error: ApiError | null;
  refreshProjects: () => Promise<void>;
  createProject: (name: string, description: string) => Promise<void>;
  duplicateProject: (projectId?: string) => Promise<void>;
  deleteProject: (projectId: string) => Promise<void>;
  openProject: (projectId: string) => Promise<void>;
  updateDraft: (update: (draft: WorkbenchDraft) => WorkbenchDraft) => void;
  saveRevision: () => Promise<RevisionRecord>;
  submitJob: (
    path: string,
    operation: string,
    body: unknown,
    sourceRevisionId?: string | null,
  ) => Promise<JobSubmission>;
  forgetJob: (jobId: string) => void;
  clearError: () => void;
}

const WorkspaceContext = createContext<WorkspaceValue | null>(null);

function readStored(): StoredState {
  try {
    const value = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "null") as StoredState | null;
    return value
      ? { ...value, jobRevisionIds: value.jobRevisionIds ?? {} }
      : { activeProjectId: null, trackedJobIds: [], jobRevisionIds: {} };
  } catch {
    return { activeProjectId: null, trackedJobIds: [], jobRevisionIds: {} };
  }
}

export function WorkspaceProvider({ children }: { children: React.ReactNode }) {
  const queryClient = useQueryClient();
  const [stored, setStored] = useState<StoredState>(readStored);
  const [projects, setProjects] = useState<ProjectRecord[]>([]);
  const [project, setProject] = useState<ProjectRecord | null>(null);
  const [revision, setRevision] = useState<RevisionRecord | null>(null);
  const [draft, setDraft] = useState<WorkbenchDraft>(defaultDraft);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const loadSequence = useRef(0);

  useEffect(() => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(stored));
  }, [stored]);

  const refreshProjects = useCallback(async () => {
    const values = await requestJson<ProjectRecord[]>("/api/v1/projects");
    setProjects(values);
  }, []);

  const loadProject = useCallback(async (projectId: string) => {
    const sequence = ++loadSequence.current;
    setBusy(true);
    setError(null);
    try {
      const loaded = await requestJson<ProjectRecord>(`/api/v1/projects/${projectId}`);
      let loadedRevision: RevisionRecord | null = null;
      if (loaded.current_revision_id) {
        loadedRevision = await requestJson<RevisionRecord>(
          `/api/v1/projects/${projectId}/revisions/${loaded.current_revision_id}`,
        );
      }
      let configuration: ProjectConfig;
      let notices: ReadonlyArray<string> = [];
      if (loadedRevision?.payload.schema_version === "3.4") configuration = loadedRevision.payload as unknown as ProjectConfig;
      else if (loadedRevision) {
        const migration = await requestJson<MigrationView>(`/api/v1/workbench/projects/${projectId}/migration`);
        configuration = migration.config; notices = migration.notices;
      } else configuration = await requestJson<ProjectConfig>("/api/v1/workbench/project/defaults");
      let view = fromProjectConfig(configuration);
      let restored = false;
      try {
        const local = JSON.parse(localStorage.getItem(`mobile-sensing-draft:${projectId}`) ?? "null");
        if (local && local.baseRevision === (loadedRevision?.revision_id ?? null)) { view = local.draft; restored = true; }
      } catch { /* A malformed local draft does not replace the saved project. */ }
      view.migrationNotices = notices;
      if (sequence !== loadSequence.current) return;
      // Commit identity and configuration together. Publishing the new identity
      // before migration completes can persist the previous project's dirty draft
      // under the new project's storage key.
      setProject(loaded);
      setRevision(loadedRevision);
      setDraft(view);
      setDirty(restored || (notices.length > 0 && !configuration.read_only));
      localStorage.setItem(STORAGE_KEY, JSON.stringify({ ...readStored(), activeProjectId: projectId }));
      setStored((value) => ({ ...value, activeProjectId: projectId }));
    } catch (caught) {
      if (sequence === loadSequence.current) setError(caught as ApiError);
    } finally {
      if (sequence === loadSequence.current) setBusy(false);
    }
  }, []);

  useEffect(() => {
    void refreshProjects().then(() => {
      if (stored.activeProjectId) void loadProject(stored.activeProjectId);
    });
  }, [loadProject, refreshProjects, stored.activeProjectId]);

  const createProject = useCallback(
    async (name: string, description: string) => {
      setBusy(true);
      setError(null);
      try {
        const created = await requestJson<ProjectRecord>("/api/v1/projects", {
          method: "POST",
          body: JSON.stringify({ name, description }),
        });
        await refreshProjects();
        await loadProject(created.project_id);
      } catch (caught) {
        setError(caught as ApiError);
        throw caught;
      } finally {
        setBusy(false);
      }
    },
    [loadProject, refreshProjects],
  );

  const updateDraft = useCallback((update: (value: WorkbenchDraft) => WorkbenchDraft) => {
    setDraft((value) => update(structuredClone(value)));
    setDirty(true);
  }, []);

  useEffect(() => {
    if (project && dirty) localStorage.setItem(`mobile-sensing-draft:${project.project_id}`, JSON.stringify({ baseRevision: revision?.revision_id ?? null, draft }));
  }, [project, dirty, revision, draft]);

  const saveRevision = useCallback(async () => {
    if (!project) throw new Error("Open a project before saving a revision.");
    setBusy(true);
    setError(null);
    try {
      const saved = await requestJson<RevisionRecord>(
        `/api/v1/projects/${project.project_id}/revisions`,
        {
          method: "POST",
          body: JSON.stringify({
            base_revision_id: revision?.revision_id ?? null,
            payload: draft.authoring,
          }),
        },
      );
      setRevision(saved);
      setProject((value) => (value ? { ...value, current_revision_id: saved.revision_id } : value));
      setDirty(false);
      localStorage.removeItem(`mobile-sensing-draft:${project.project_id}`);
      void queryClient.invalidateQueries({ queryKey: ["revisions", project.project_id] });
      return saved;
    } catch (caught) {
      setError(caught as ApiError);
      throw caught;
    } finally {
      setBusy(false);
    }
  }, [draft, project, queryClient, revision]);

  const duplicateProject = useCallback(async (projectId?: string) => {
    const originalId = projectId ?? project?.project_id;
    if (!originalId) throw new Error("Select a project before duplicating it.");
    setBusy(true);
    setError(null);
    try {
      const configuration = originalId === project?.project_id ? draft.authoring : (await requestJson<MigrationView>(`/api/v1/workbench/projects/${originalId}/migration`)).config;
      const created = await requestJson<ProjectRecord>(`/api/v1/workbench/projects/${originalId}/copy`, {
        method: "POST", body: JSON.stringify({ config: configuration }),
      });
      await refreshProjects();
      await loadProject(created.project_id);
    } catch (caught) {
      setError(caught as ApiError);
      throw caught;
    } finally {
      setBusy(false);
    }
  }, [draft, loadProject, project, refreshProjects]);

  const deleteProject = useCallback(async (projectId: string) => {
    await requestJson(`/api/v1/projects/${projectId}`, { method: "DELETE" });
    if (project?.project_id === projectId) {
      setProject(null); setRevision(null); setDraft(defaultDraft()); setDirty(false);
      setStored((value) => ({ ...value, activeProjectId: null }));
    }
    await refreshProjects();
  }, [project, refreshProjects]);

  const submitJob = useCallback(
    async (path: string, operation: string, body: unknown, sourceRevisionId?: string | null) => {
      if (!project) throw new Error("Open a project before submitting a job.");
      setError(null);
      try {
        const submission = await requestJson<JobSubmission>(path, {
          method: "POST",
          headers: jobHeaders(project.project_id, operation),
          body: JSON.stringify(body),
        });
        if (operation === "studio_run" || operation === "studio_analysis") {
          const current = await requestJson<ProjectRecord>(`/api/v1/projects/${project.project_id}`);
          if (current.current_revision_id) {
            const saved = await requestJson<RevisionRecord>(`/api/v1/projects/${project.project_id}/revisions/${current.current_revision_id}`);
            setRevision(saved); setProject(current); setDirty(false);
            localStorage.removeItem(`mobile-sensing-draft:${project.project_id}`);
          }
        }
        setStored((value) => ({
          ...value,
          trackedJobIds: [...new Set([submission.job_id, ...value.trackedJobIds])].slice(0, 50),
          jobRevisionIds: {
            ...value.jobRevisionIds,
            [submission.job_id]: sourceRevisionId === undefined
              ? (dirty ? null : (revision?.revision_id ?? null))
              : sourceRevisionId,
          },
        }));
        void queryClient.invalidateQueries({ queryKey: ["jobs", project.project_id] });
        return submission;
      } catch (caught) {
        setError(caught as ApiError);
        throw caught;
      }
    },
    [dirty, project, queryClient, revision?.revision_id],
  );

  const forgetJob = useCallback((jobId: string) => {
    setStored((value) => {
      const jobRevisionIds = { ...value.jobRevisionIds };
      delete jobRevisionIds[jobId];
      return {
        ...value,
        trackedJobIds: value.trackedJobIds.filter((candidate) => candidate !== jobId),
        jobRevisionIds,
      };
    });
  }, []);

  const value = useMemo<WorkspaceValue>(
    () => ({
      project,
      projects,
      revision,
      draft,
      dirty,
      trackedJobIds: stored.trackedJobIds,
      jobRevisionIds: stored.jobRevisionIds,
      busy,
      error,
      refreshProjects,
      createProject,
      duplicateProject,
      deleteProject,
      openProject: loadProject,
      updateDraft,
      saveRevision,
      submitJob,
      forgetJob,
      clearError: () => setError(null),
    }),
    [
      busy,
      createProject,
      duplicateProject,
      deleteProject,
      dirty,
      draft,
      error,
      forgetJob,
      loadProject,
      project,
      projects,
      refreshProjects,
      revision,
      saveRevision,
      stored.trackedJobIds,
      stored.jobRevisionIds,
      submitJob,
      updateDraft,
    ],
  );
  return <WorkspaceContext.Provider value={value}>{children}</WorkspaceContext.Provider>;
}

export function useWorkspace(): WorkspaceValue {
  const value = useContext(WorkspaceContext);
  if (!value) throw new Error("useWorkspace must be used inside WorkspaceProvider");
  return value;
}
