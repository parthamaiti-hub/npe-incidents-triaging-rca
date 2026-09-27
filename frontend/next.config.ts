import type { NextConfig } from "next";

// The actual /api/backend/* proxy lives in app/api/backend/[...path]/route.ts
// (a Route Handler, not a rewrites() entry here) -- see that file's comment
// for why: rewrites() destinations get resolved once at `next build` time in
// `output: "standalone"` mode, so BACKEND_URL set at `docker run` would be
// silently ignored.
const nextConfig: NextConfig = {
  output: "standalone",
};

export default nextConfig;
