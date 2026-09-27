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
 * Looks an incident up by its incident_key -- its Jira key, or the
 * generated int_... key for incidents that arrived without one. Reuses the
 * dashboard's `q` filter (which ILIKE-matches incident_key) and picks the
 * exact match client-side, since `q` also substring-matches `subject`.
 * Returns `null` (not an error) when no such incident exists -- distinct
 * from a real fetch failure.
 */
export function useIncidentByKey(incidentKey: string | undefined) {
  return useQuery({
    queryKey: ["incidents", "by-key", incidentKey],
    queryFn: async () => {
      const { data, error } = await api.GET("/incidents/dashboard", {
        params: { query: { q: incidentKey, limit: 5 } },
      });
      if (error) throw error;
      return data.items.find((item) => item.incident_key === incidentKey) ?? null;
    },
    enabled: !!incidentKey,
  });
}

export interface RetryInput {
  incidentKey: string;
  workflowDefinitionVersionId?: string;
  requestedBy: string;
  /** Optional operator-supplied hint for RCA synthesis --
   * persisted regardless, only actually consumed by the LLM prompt once
   * RCA_SYNTHESIS_LLM_ENABLED. */
  operatorContext?: string;
}

/** POST /incidents/{incident_key}/retry -- reprocesses an already-ingested
 * incident: re-classifies its stored text with the current mapping rules,
 * then runs the active playbook (or the chosen version); 'no playbook' /
 * 'not classified' outcomes are recorded, not rejected. Throws the
 * backend's message on 404 (unknown key) / 422 (unretriable version),
 * surfaced inline by the retry page rather than a toast. */
export function useRetryIncident() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ incidentKey, workflowDefinitionVersionId, requestedBy, operatorContext }: RetryInput) => {
      const { data, error, response } = await api.POST("/incidents/{incident_key}/retry", {
        params: { path: { incident_key: incidentKey } },
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
      queryClient.invalidateQueries({ queryKey: ["executions", "by-incident-key", variables.incidentKey] });
      // Retry re-classifies, so the incident's own fields may have changed too.
      queryClient.invalidateQueries({ queryKey: ["incidents"] });
    },
  });
}
