import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api";

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

export function useExecutionsForJiraKey(jiraKey: string | undefined) {
  return useQuery({
    queryKey: ["executions", "by-jira-key", jiraKey],
    queryFn: async () => {
      const { data, error } = await api.GET("/workflows/executions", { params: { query: { jira_key: jiraKey } } });
      if (error) throw error;
      return data; // already newest-first (started_at desc), per the backend's own ordering
    },
    enabled: !!jiraKey,
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
      if (error) {
        throw new Error(typeof error === "object" && error && "detail" in error ? String(error.detail) : `Request failed (${response.status})`);
      }
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
      if (error) {
        const detail = typeof error === "object" && error && "detail" in error ? String(error.detail) : null;
        throw new Error(detail ?? `Approve failed (${response.status})`);
      }
      return data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["workflows", "definitions"] });
    },
  });
}
