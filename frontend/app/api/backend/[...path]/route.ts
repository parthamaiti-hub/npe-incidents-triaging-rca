import type { NextRequest } from "next/server";

// The browser only ever talks to this app's own origin -- /api/backend/*
// proxies server-side to BACKEND_URL, never exposed as NEXT_PUBLIC_*. This
// is what lets the FastAPI backend (app/main.py) stay unauthenticated and
// CORS-closed by default while the UI still reaches it.
//
// A Route Handler, not a next.config.ts `rewrites()` entry: in `output:
// "standalone"` mode, rewrites() is invoked once at `next build` and its
// resolved destination is serialized into .next/routes-manifest.json --
// setting BACKEND_URL on `docker run` had no effect on that already-baked
// string (confirmed live: the container tried connecting to the build-time
// default even with a different runtime env var). A Route Handler is real
// code that runs fresh per request in the running server process, so it
// reads process.env.BACKEND_URL at the time each request actually arrives
// -- the pattern Next.js's own "Proxying to a backend" guide recommends.
function backendUrl(): string {
  return process.env.BACKEND_URL ?? "http://localhost:8421";
}

async function proxy(request: NextRequest, path: string[]) {
  const target = new URL(path.join("/"), `${backendUrl()}/`);
  target.search = request.nextUrl.search;

  const proxyRequest = new Request(target, request);
  return fetch(proxyRequest);
}

export async function GET(request: NextRequest, { params }: { params: Promise<{ path: string[] }> }) {
  return proxy(request, (await params).path);
}

export async function POST(request: NextRequest, { params }: { params: Promise<{ path: string[] }> }) {
  return proxy(request, (await params).path);
}

export async function PUT(request: NextRequest, { params }: { params: Promise<{ path: string[] }> }) {
  return proxy(request, (await params).path);
}

export async function PATCH(request: NextRequest, { params }: { params: Promise<{ path: string[] }> }) {
  return proxy(request, (await params).path);
}

export async function DELETE(request: NextRequest, { params }: { params: Promise<{ path: string[] }> }) {
  return proxy(request, (await params).path);
}
