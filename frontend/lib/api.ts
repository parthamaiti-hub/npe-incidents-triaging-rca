import createClient from "openapi-fetch";

import type { paths } from "./types";

// Same-origin only: every call goes through this app's own /api/backend/*
// rewrite (see next.config.ts), which Next.js forwards server-side to
// BACKEND_URL. The browser never talks to the FastAPI backend directly, so
// the backend's CORS stays off.
export const api = createClient<paths>({ baseUrl: "/api/backend" });
