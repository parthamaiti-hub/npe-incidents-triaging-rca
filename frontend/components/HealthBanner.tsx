"use client";

import { useHealth } from "@/lib/queries/health";

/** Backend-down banner -- GET /health exercises DB/Redis/RabbitMQ, not just "is FastAPI
 * listening", so a non-ok response here means the app is genuinely degraded,
 * not just slow. Polls every 15s; shows nothing while healthy. */
export function HealthBanner() {
  const { data, isError, isLoading } = useHealth();
  // GET /health returns a raw JSONResponse (app/main.py), not a Pydantic
  // response_model, so the OpenAPI schema -- and this hook's `data` -- is
  // `unknown`. Narrow it here rather than widening the generated type.
  const status = data && typeof data === "object" && "status" in data ? data.status : undefined;

  if (isLoading) return null; // don't flash the banner during the first check
  if (!isError && status === "ok") return null;

  return (
    <div className="bg-danger px-6 py-1.5 text-center text-sm font-medium text-white">
      Backend unavailable -- some features may not work until it recovers.
    </div>
  );
}
