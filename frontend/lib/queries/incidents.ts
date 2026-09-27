import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api";

export interface DashboardFilters {
  q?: string;
  classification_status?: string;
  rca_status?: string;
  source_system_id?: string;
  category?: string;
  date_from?: string;
  date_to?: string;
  limit?: number;
  offset?: number;
}

export function useIncidentsDashboard(filters: DashboardFilters) {
  return useQuery({
    queryKey: ["incidents", "dashboard", filters],
    queryFn: async () => {
      const { data, error } = await api.GET("/incidents/dashboard", { params: { query: filters } });
      if (error) throw error;
      return data;
    },
  });
}

/**
 * There's no `GET /incidents/by-jira-key/{key}` route -- the dashboard
 * endpoint's `q` filter already ILIKE-matches jira_key, so this reuses it
 * as a lookup-by-key and picks the exact match client-side (q also
 * substring-matches `subject`, so an exact-key check is still needed).
 * Returns `null` (not an error) when no incident with this jira_key has
 * been ingested yet -- distinct from a real fetch failure.
 */
export function useIncidentByJiraKey(jiraKey: string | undefined) {
  return useQuery({
    queryKey: ["incidents", "by-jira-key", jiraKey],
    queryFn: async () => {
      const { data, error } = await api.GET("/incidents/dashboard", {
        params: { query: { q: jiraKey, limit: 5 } },
      });
      if (error) throw error;
      return data.items.find((item) => item.jira_key === jiraKey) ?? null;
    },
    enabled: !!jiraKey,
  });
}

export interface RetryInput {
  jiraKey: string;
  workflowDefinitionVersionId?: string;
  requestedBy: string;
  /** Optional operator-supplied hint for RCA synthesis --
   * persisted regardless, only actually consumed by the LLM prompt once
   * RCA_SYNTHESIS_LLM_ENABLED. */
  operatorContext?: string;
}

/** POST /incidents/{jira_key}/retry -- re-runs RCA against an already-
 * ingested incident (no live Jira refetch, unlike POST /rca/{jira_key}).
 * Throws the backend's error message on 404 (never ingested) / 422
 * (no active workflow, or an unretriable version), surfaced inline by the
 * retry page rather than a toast. */
export function useRetryIncident() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ jiraKey, workflowDefinitionVersionId, requestedBy, operatorContext }: RetryInput) => {
      const { data, error, response } = await api.POST("/incidents/{jira_key}/retry", {
        params: { path: { jira_key: jiraKey } },
        body: {
          workflow_definition_version_id: workflowDefinitionVersionId,
          requested_by: requestedBy,
          operator_context: operatorContext || undefined,
        },
      });
      if (error) {
        const detail = typeof error === "object" && error && "detail" in error ? String(error.detail) : null;
        throw new Error(detail ?? `Retry failed (${response.status})`);
      }
      return data;
    },
    onSuccess: (_data, variables) => {
      queryClient.invalidateQueries({ queryKey: ["executions", "by-jira-key", variables.jiraKey] });
      queryClient.invalidateQueries({ queryKey: ["incidents", "dashboard"] });
    },
  });
}
