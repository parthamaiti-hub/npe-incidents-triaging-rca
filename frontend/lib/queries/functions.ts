import { useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api";

export function useFunctions() {
  return useQuery({
    queryKey: ["functions"],
    queryFn: async () => {
      const { data, error } = await api.GET("/functions");
      if (error) throw error;
      return data;
    },
    staleTime: 60_000,
  });
}

/** Full version history (active + superseded) for one function --
 * used by the Check Type Registry tab's version selector. */
export function useFunctionVersions(functionId: string | undefined) {
  return useQuery({
    queryKey: ["functions", functionId, "versions"],
    queryFn: async () => {
      const { data, error } = await api.GET("/functions/{function_id}/versions", {
        params: { path: { function_id: functionId! } },
      });
      if (error) throw error;
      return data;
    },
    enabled: !!functionId,
  });
}

/** Read-only source for the playbook tab's function panel. */
export function useFunctionSource(call: string | undefined, versionNumber: number | null | undefined) {
  return useQuery({
    queryKey: ["functions", call, "source", versionNumber],
    queryFn: async () => {
      const { data, error } = await api.GET("/functions/{function_id}/versions/{version_number}/source", {
        params: { path: { function_id: call!, version_number: versionNumber! } },
      });
      if (error) throw error;
      return data;
    },
    enabled: !!call && versionNumber != null,
  });
}
