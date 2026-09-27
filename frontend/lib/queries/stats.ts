import { useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api";

export function useIncidentStats(period: "24h" | "7d" | "30d" | "all" = "7d") {
  return useQuery({
    queryKey: ["stats", "incidents", period],
    queryFn: async () => {
      const { data, error } = await api.GET("/stats/incidents", { params: { query: { period } } });
      if (error) throw error;
      return data;
    },
    refetchInterval: 30_000,
  });
}
