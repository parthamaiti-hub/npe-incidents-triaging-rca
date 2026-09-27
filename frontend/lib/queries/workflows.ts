import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api";
import type { ApiIssue, DraftTask } from "@/lib/workflowDraft";

/** A failed workflow API call. For playbook YAML/edit endpoints, a 422
 * carries structured `issues` (with YAML line and task index) and a `kind`
 * of syntax | schema | registry; other failures just carry a message. */
export class WorkflowApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly kind: string | null = null,
    readonly issues: ApiIssue[] = [],
  ) {
    super(message);
  }
}

function toWorkflowApiError(error: unknown, response: Response, fallback: string): WorkflowApiError {
  const detail = typeof error === "object" && error && "detail" in error ? (error as { detail: unknown }).detail : null;
  if (detail && typeof detail === "object" && !Array.isArray(detail) && "errors" in detail) {
    const d = detail as { kind?: string; message?: string; errors?: ApiIssue[] };
    return new WorkflowApiError(d.message ?? fallback, response.status, d.kind ?? null, d.errors ?? []);
  }
  if (Array.isArray(detail)) {
    // FastAPI request-body validation errors: [{loc, msg, type}]
    const message = detail.map((d) => String((d as { msg?: unknown }).msg ?? d)).join("; ");
    return new WorkflowApiError(message || fallback, response.status);
  }
  if (typeof detail === "string") return new WorkflowApiError(detail, response.status);
  return new WorkflowApiError(`${fallback} (${response.status})`, response.status);
}

export function useExecution(executionId: string | undefined) {
  return useQuery({
    queryKey: ["executions", executionId],
    queryFn: async () => {
      const { data, error } = await api.GET("/workflows/executions/{execution_id}", {
        params: { path: { execution_id: executionId! } },
      });
      if (error) throw error;
      return data;
    },
    enabled: !!executionId,
    // Evidence can finish (status="completed") before
    // LLM-primary RCA synthesis does (rca_status still null, picked up
    // async by app.rca_worker) -- poll only in that specific pending
    // window, same simple-polling pattern StatSticker already uses
    // elsewhere, not a new push/websocket mechanism.
    refetchInterval: (query) => {
      const data = query.state.data;
      return data && data.status === "completed" && data.rca_status === null ? 3000 : false;
    },
  });
}

export function useExecutionsForIncident(incidentKey: string | undefined) {
  return useQuery({
    queryKey: ["executions", "by-incident-key", incidentKey],
    queryFn: async () => {
      const { data, error } = await api.GET("/workflows/executions", {
        params: { query: { incident_key: incidentKey } },
      });
      if (error) throw error;
      return data; // already newest-first (started_at desc), per the backend's own ordering
    },
    enabled: !!incidentKey,
  });
}

export function useWorkflowDefinitions() {
  return useQuery({
    queryKey: ["workflows", "definitions"],
    queryFn: async () => {
      const { data, error } = await api.GET("/workflows/definitions");
      if (error) throw error;
      return data;
    },
  });
}

export function useWorkflowVersions(definitionId: string | undefined) {
  return useQuery({
    queryKey: ["workflows", "definitions", definitionId, "versions"],
    queryFn: async () => {
      const { data, error } = await api.GET("/workflows/definitions/{definition_id}/versions", {
        params: { path: { definition_id: definitionId! } },
      });
      if (error) throw error;
      return data;
    },
    enabled: !!definitionId,
  });
}

export interface BuildWorkflowInput {
  source_system_id: string;
  category: string;
  requested_functions: { call: string; with: Record<string, unknown> }[];
  requested_by: string;
  use_case_description?: string;
}

/** Throws the backend's 422 message string on validation failure (surfaced
 * inline by the wizard, not as a toast). */
export function useBuildWorkflow() {
  return useMutation({
    mutationFn: async (input: BuildWorkflowInput) => {
      const { data, error, response } = await api.POST("/workflows/build-requests", { body: input });
      if (error) throw toWorkflowApiError(error, response, "Request failed");
      return data;
    },
  });
}

export function useApproveBuildRequest() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ requestId, approvedBy }: { requestId: string; approvedBy: string }) => {
      const { data, error, response } = await api.POST("/workflows/build-requests/{request_id}/approve", {
        params: { path: { request_id: requestId } },
        body: { approved_by: approvedBy },
      });
      if (error) throw toWorkflowApiError(error, response, "Approve failed");
      return data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["workflows", "definitions"] });
    },
  });
}

export function useRejectBuildRequest() {
  return useMutation({
    mutationFn: async ({ requestId, rejectedBy, reason }: { requestId: string; rejectedBy: string; reason?: string }) => {
      const { error, response } = await api.POST("/workflows/build-requests/{request_id}/reject", {
        params: { path: { request_id: requestId } },
        body: { rejected_by: rejectedBy, reason: reason ?? null },
      });
      if (error) throw toWorkflowApiError(error, response, "Reject failed");
    },
  });
}

/** A stored version as CNCF Serverless Workflow YAML (the editor's starting text). */
export function useVersionYaml(versionId: string | undefined) {
  return useQuery({
    queryKey: ["workflows", "versions", versionId, "yaml"],
    queryFn: async () => {
      const { data, error, response } = await api.GET("/workflows/versions/{version_id}/yaml", {
        params: { path: { version_id: versionId! } },
      });
      if (error) throw toWorkflowApiError(error, response, "Failed to load YAML");
      return data.yaml;
    },
    enabled: !!versionId,
    staleTime: Infinity, // a version is immutable
  });
}

/** Graph edits -> YAML text. Pure conversion; no registry validation. */
export function useRenderYaml() {
  return useMutation({
    mutationFn: async ({ tasks, name }: { tasks: DraftTask[]; name: string }) => {
      const { data, error, response } = await api.POST("/workflows/render-yaml", {
        body: { tasks: tasks as unknown as Record<string, unknown>[], name, version: "draft" },
      });
      if (error) throw toWorkflowApiError(error, response, "Failed to render YAML");
      return data.yaml;
    },
  });
}

async function validateYaml(yaml: string, baseVersionId: string | null): Promise<DraftTask[]> {
  const { data, error, response } = await api.POST("/workflows/validate-yaml", {
    body: { yaml, base_version_id: baseVersionId },
  });
  if (error) throw toWorkflowApiError(error, response, "Validation failed");
  return data.tasks as unknown as DraftTask[];
}

/** Dry-run validation; never persists. */
export function useValidateYaml() {
  return useMutation({
    mutationFn: ({ yaml, baseVersionId }: { yaml: string; baseVersionId: string | null }) => validateYaml(yaml, baseVersionId),
  });
}

/** Live (debounced) validation of the YAML pane while it has unapplied edits. */
export function useLiveYamlValidation(yaml: string, baseVersionId: string | null, enabled: boolean) {
  return useQuery({
    queryKey: ["workflows", "validate-yaml", baseVersionId, yaml],
    queryFn: () => validateYaml(yaml, baseVersionId),
    enabled,
    retry: false,
    staleTime: Infinity,
  });
}

export interface SubmitEditInput {
  definitionId: string;
  baseVersionId: string;
  editedBy: string;
  changeNote?: string;
  /** Exactly one of yaml (YAML pane has unapplied edits) or tasks (graph draft). */
  yaml?: string;
  tasks?: DraftTask[];
}

/** Saves an edit as a 'rendered' build request -- nothing is published until
 * it's approved with useApproveBuildRequest. */
export function useSubmitEdit() {
  return useMutation({
    mutationFn: async (input: SubmitEditInput) => {
      const { data, error, response } = await api.POST("/workflows/definitions/{definition_id}/edits", {
        params: { path: { definition_id: input.definitionId } },
        body: {
          base_version_id: input.baseVersionId,
          edited_by: input.editedBy,
          change_note: input.changeNote || null,
          yaml: input.yaml ?? null,
          tasks: (input.tasks as unknown as Record<string, unknown>[] | undefined) ?? null,
        },
      });
      if (error) throw toWorkflowApiError(error, response, "Save failed");
      return data;
    },
  });
}
