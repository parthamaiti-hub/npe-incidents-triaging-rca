"use client";

import { useMemo, useState } from "react";

import { CheckTypeParamsView } from "@/components/CheckTypeParamsView";
import { FunctionSourceCode } from "@/components/FunctionSourceCode";
import { useFunctionVersions, useFunctions } from "@/lib/queries/functions";

// Check Type Registry tab -- browse every check_type
// (FUNCTION_DEFINITION), its declared parameter contract and version
// history (FUNCTION_DEFINITION_VERSION), and its read-only Python
// implementation source (app/check_types/*.py). Display only -- no
// publish/delete affordance anywhere on this page. List-on-left/
// detail-on-right, same split system-mapping/page.tsx and
// playbooks/page.tsx already establish.
export default function CheckTypesPage() {
  const { data: functions, isLoading, isError } = useFunctions();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [search, setSearch] = useState("");

  const filteredFunctions = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return functions ?? [];
    return (functions ?? []).filter((f) => f.name.toLowerCase().includes(q) || f.description.toLowerCase().includes(q));
  }, [functions, search]);

  const selected = functions?.find((f) => f.name === selectedId) ?? null;

  return (
    <main className="p-6">
      <h1 className="text-xl font-semibold text-heading">Check Type Registry</h1>
      <p className="mt-1 text-sm text-muted">
        Every diagnostic check_type&apos;s input parameters and implementation source, read-only.
      </p>

      <div className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-[1fr_2fr]">
        <div className="rounded-md border border-grid bg-white">
          <div className="border-b border-grid p-2">
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search check types…"
              aria-label="Search check types"
              className="block w-full rounded border border-grid px-2 py-1 text-sm text-heading"
            />
          </div>
          {isLoading && <p className="p-4 text-sm text-muted">Loading…</p>}
          {isError && <p className="p-4 text-sm text-danger">Failed to load check types.</p>}
          <ul className="divide-y divide-grid">
            {filteredFunctions.map((f) => (
              <li key={f.name}>
                <button
                  type="button"
                  onClick={() => setSelectedId(f.name)}
                  className={`block w-full px-3 py-2 text-left text-sm hover:bg-canvas ${
                    f.name === selectedId ? "bg-canvas font-medium text-heading" : "text-muted"
                  }`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <code className="text-heading">{f.name}</code>
                    {!f.has_implementation && <span className="text-xs text-highlight">no impl</span>}
                  </div>
                  <div className="text-xs text-muted">{f.description}</div>
                </button>
              </li>
            ))}
          </ul>
          {functions?.length === 0 && <p className="p-4 text-sm text-muted">No check types registered yet.</p>}
          {!!functions?.length && filteredFunctions.length === 0 && (
            <p className="p-4 text-sm text-muted">No check types match this search.</p>
          )}
        </div>

        <div>
          {!selected && <p className="text-sm text-muted">Select a check type on the left.</p>}
          {selected && <CheckTypeDetail activeFunction={selected} />}
        </div>
      </div>
    </main>
  );
}

function CheckTypeDetail({
  activeFunction,
}: {
  activeFunction: { name: string; description: string; version_number?: number | null };
}) {
  const { data: versions, isLoading } = useFunctionVersions(activeFunction.name);
  const [selectedVersion, setSelectedVersion] = useState<number | null>(null);

  const activeVersionNumber = activeFunction.version_number ?? null;
  const versionNumber = selectedVersion ?? activeVersionNumber ?? versions?.[versions.length - 1]?.version_number ?? null;
  const activeVersionSpec = versions?.find((v) => v.version_number === versionNumber) ?? null;

  return (
    <div>
      <div className="rounded-md border border-grid bg-white p-4">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div>
            <h2 className="text-sm font-semibold text-heading">
              <code>{activeFunction.name}</code>
            </h2>
            <p className="text-xs text-muted">{activeFunction.description}</p>
          </div>
          {versions && versions.length > 0 && (
            <label className="text-xs text-muted">
              Version
              <select
                value={versionNumber ?? ""}
                onChange={(e) => setSelectedVersion(Number(e.target.value))}
                className="ml-2 rounded border border-grid px-2 py-1 text-sm text-heading"
              >
                {versions.map((v) => (
                  <option key={v.version_number} value={v.version_number ?? ""}>
                    v{v.version_number}
                    {v.version_number === activeVersionNumber ? " (active)" : ""}
                  </option>
                ))}
              </select>
            </label>
          )}
        </div>
      </div>

      {isLoading && <p className="mt-4 text-sm text-muted">Loading…</p>}

      {activeVersionSpec && (
        <>
          <div className="mt-4 rounded-md border border-grid bg-white p-4">
            <h3 className="text-xs font-semibold uppercase tracking-wide text-muted">Parameters</h3>
            <CheckTypeParamsView params={activeVersionSpec.params} />
            <p className="mt-3 text-xs text-muted">
              Retry: {activeVersionSpec.default_retry.max_attempts} attempt(s), {activeVersionSpec.default_retry.delay_seconds}s
              delay{activeVersionSpec.default_retry.exponential_backoff ? ", exponential backoff" : ""}.
            </p>
          </div>

          <div className="mt-4 rounded-md border border-grid bg-white p-4">
            <h3 className="text-xs font-semibold uppercase tracking-wide text-muted">Output shape</h3>
            <p className="mt-1 text-sm text-muted">
              Every check_type returns the same fixed shape today -- there is no per-check-type output schema in the data
              model:
            </p>
            <pre className="mt-2 rounded bg-canvas p-2 text-xs text-heading">{`{ "status": "OK" | "WARN" | "ERROR", "details": "<string>" }`}</pre>
          </div>

          <div className="mt-4 rounded-md border border-grid bg-white p-4">
            <h3 className="text-xs font-semibold uppercase tracking-wide text-muted">Source</h3>
            <FunctionSourceCode functionId={activeFunction.name} versionNumber={versionNumber} />
          </div>
        </>
      )}
    </div>
  );
}
