import { useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api";

export function useHealth() {
  return useQuery({
    queryKey: ["health"],
    queryFn: async () => {
      const { data, error } = await api.GET("/health");
      if (error) throw error;
      return data;
    },
    refetchInterval: 15_000,
    retry: false, // a down backend should show the banner immediately, not after retries
  });
}
