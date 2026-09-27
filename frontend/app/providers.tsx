"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";

export function Providers({ children }: { children: React.ReactNode }) {
  // One QueryClient per browser session, created lazily so it isn't
  // recreated on every render (the standard App Router pattern -- a
  // module-level singleton would leak state across users on the server).
  const [queryClient] = useState(() => new QueryClient());
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
}
