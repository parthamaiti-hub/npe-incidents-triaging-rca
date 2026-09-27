import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api";

export function useFeedback(executionId: string | undefined) {
  return useQuery({
    queryKey: ["feedback", executionId],
    queryFn: async () => {
      const { data, error } = await api.GET("/workflows/executions/{execution_id}/feedback", {
        params: { path: { execution_id: executionId! } },
      });
      if (error) throw error;
      return data;
    },
    enabled: !!executionId,
  });
}

export interface FeedbackInput {
  comment?: string;
  confidence_score: number;
  given_by: string;
  /** Optional structured correction -- cleaner RAG grounding
   * than free-text comment alone. Neither required. */
  corrected_pattern_id?: string;
  corrected_rca_status?: string;
}

export function useRcaPatterns() {
  return useQuery({
    queryKey: ["rca-patterns"],
    queryFn: async () => {
      const { data, error } = await api.GET("/workflows/rca-patterns");
      if (error) throw error;
      return data;
    },
    staleTime: 5 * 60 * 1000, // catalog data, changes rarely -- no need to refetch aggressively
  });
}

export function useSubmitFeedback(executionId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: FeedbackInput) => {
      const { data, error, response } = await api.POST("/workflows/executions/{execution_id}/feedback", {
        params: { path: { execution_id: executionId } },
        body: input,
      });
      if (error) {
        const detail = typeof error === "object" && error && "detail" in error ? String(error.detail) : null;
        throw new Error(detail ?? `Feedback submission failed (${response.status})`);
      }
      return data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["feedback", executionId] });
    },
  });
}
