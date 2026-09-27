import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "@/lib/api";

/** Extracts the backend's HTTPException(...) detail string, falling back to
 * a generic message -- same shape every mutation hook in this codebase
 * already uses (see useBuildWorkflow/useApproveBuildRequest, workflows.ts). */
function errorMessage(error: unknown, response: Response, fallback: string): string {
  const detail = typeof error === "object" && error && "detail" in error ? String((error as { detail: unknown }).detail) : null;
  return detail ?? `${fallback} (${response.status})`;
}

export function useSourceSystems() {
  return useQuery({
    queryKey: ["catalog", "source-systems"],
    queryFn: async () => {
      const { data, error } = await api.GET("/catalog/source-systems");
      if (error) throw error;
      return data;
    },
    staleTime: 5 * 60_000,
  });
}

export function useTeams() {
  return useQuery({
    queryKey: ["catalog", "teams"],
    queryFn: async () => {
      const { data, error } = await api.GET("/catalog/teams");
      if (error) throw error;
      return data;
    },
    staleTime: 5 * 60_000,
  });
}

/** System Mapping Data tab -- a SourceSystem's footprints,
 * scoped via the backend's existing ?source_system_id= filter
 * (app/routers/catalog.py's filterable_by_source_system). */
export function useFootprints(sourceSystemId: string | undefined) {
  return useQuery({
    queryKey: ["catalog", "footprints", sourceSystemId],
    queryFn: async () => {
      const { data, error } = await api.GET("/catalog/footprints", {
        params: { query: { source_system_id: sourceSystemId } },
      });
      if (error) throw error;
      return data;
    },
    enabled: !!sourceSystemId,
  });
}

export interface SourceSystemInput {
  id: string;
  name: string;
  code: string;
  type: string;
  description: string;
  owning_team: string;
  environment: string;
  owning_team_id?: string | null;
}

export function useCreateSourceSystem() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: SourceSystemInput) => {
      const { data, error, response } = await api.POST("/catalog/source-systems", { body: input });
      if (error) throw new Error(errorMessage(error, response, "Create failed"));
      return data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["catalog", "source-systems"] });
    },
  });
}

export function useUpdateSourceSystem() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: SourceSystemInput) => {
      const { data, error, response } = await api.PUT("/catalog/source-systems/{item_id}", {
        params: { path: { item_id: input.id } },
        body: input,
      });
      if (error) throw new Error(errorMessage(error, response, "Update failed"));
      return data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["catalog", "source-systems"] });
    },
  });
}

export function useDeleteSourceSystem() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (id: string) => {
      const { error, response } = await api.DELETE("/catalog/source-systems/{item_id}", {
        params: { path: { item_id: id } },
      });
      // 409 here means "referenced by other rows" -- a real, expected
      // outcome the UI surfaces verbatim, not an unexpected failure.
      if (error) throw new Error(errorMessage(error, response, "Delete failed"));
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["catalog", "source-systems"] });
    },
  });
}

export interface FootprintInput {
  id: string;
  source_system_id: string;
  footprint_type: string;
  value: string;
  notes?: string | null;
}

export function useCreateFootprint() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: FootprintInput) => {
      const { data, error, response } = await api.POST("/catalog/footprints", { body: input });
      if (error) throw new Error(errorMessage(error, response, "Create failed"));
      return data;
    },
    onSuccess: (_data, input) => {
      queryClient.invalidateQueries({ queryKey: ["catalog", "footprints", input.source_system_id] });
    },
  });
}

export function useUpdateFootprint() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (input: FootprintInput) => {
      const { data, error, response } = await api.PUT("/catalog/footprints/{item_id}", {
        params: { path: { item_id: input.id } },
        body: input,
      });
      if (error) throw new Error(errorMessage(error, response, "Update failed"));
      return data;
    },
    onSuccess: (_data, input) => {
      queryClient.invalidateQueries({ queryKey: ["catalog", "footprints", input.source_system_id] });
    },
  });
}

export function useDeleteFootprint() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ id }: { id: string; sourceSystemId: string }) => {
      const { error, response } = await api.DELETE("/catalog/footprints/{item_id}", {
        params: { path: { item_id: id } },
      });
      if (error) throw new Error(errorMessage(error, response, "Delete failed"));
    },
    onSuccess: (_data, { sourceSystemId }) => {
      queryClient.invalidateQueries({ queryKey: ["catalog", "footprints", sourceSystemId] });
    },
  });
}

/**
 * No canonical category-list endpoint exists, and the unshipped categories'
 * exact taxonomy strings (TEST/REL/COMM/SEC)
 * aren't confirmed anywhere in code or real data -- so this derives filter
 * options from what's actually loaded in `INCIDENT_MAPPING_RULE`, excluding
 * the `ANY` keyword-fallback sentinel (never a real incident category).
 */
export function useCategoryOptions() {
  return useQuery({
    queryKey: ["catalog", "categories"],
    queryFn: async () => {
      const { data, error } = await api.GET("/catalog/mapping-rules");
      if (error) throw error;
      const categories = Array.from(new Set(data.map((rule) => rule.category))).filter((c) => c !== "ANY");
      categories.sort();
      return categories;
    },
    staleTime: 5 * 60_000,
  });
}

/**
 * `/catalog/mapping-rules` only filters by `source_system_id` -- the
 * playbook tab's header wants the rule for one specific
 * (source_system, category) pair, so the category match happens
 * client-side over that system's (typically small) rule list.
 */
export function useMappingRulesForSourceSystem(sourceSystemId: string | undefined) {
  return useQuery({
    queryKey: ["catalog", "mapping-rules", sourceSystemId],
    queryFn: async () => {
      const { data, error } = await api.GET("/catalog/mapping-rules", {
        params: { query: { source_system_id: sourceSystemId } },
      });
      if (error) throw error;
      return data;
    },
    enabled: !!sourceSystemId,
  });
}
