"use client";

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Suspense } from "react";

import { IncidentFilters } from "@/components/IncidentFilters";
import { SkeletonTableRows } from "@/components/Skeleton";
import { classificationBadgeClass, classificationMethodAnnotation, classificationStatusLabel } from "@/lib/classification";
import { useIncidentsDashboard } from "@/lib/queries/incidents";
import { humanizeRcaStatus, rcaStatusBadgeClass } from "@/lib/rca";

const PAGE_SIZE = 20;

// Tab 1 -- RCA of Incidents. One row per incident,
// newest-processed-first, joined server-side to its latest RCA attempt
// (GET /incidents/dashboard already dedupes "processed more than once" to
// the latest version).
//
// useSearchParams() (here and in IncidentFilters) requires a Suspense
// boundary above it or `next build`'s static-page bailout check fails --
// the actual content lives in IncidentsPageContent, wrapped below.
export default function IncidentsPage() {
  return (
    <Suspense fallback={<main className="p-6 text-sm text-muted">Loading…</main>}>
      <IncidentsPageContent />
    </Suspense>
  );
}

function IncidentsPageContent() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const offset = Number(searchParams.get("offset") ?? "0");

  const { data, isLoading, isError } = useIncidentsDashboard({
    q: searchParams.get("q") || undefined,
    classification_status: searchParams.get("classification_status") || undefined,
    rca_status: searchParams.get("rca_status") || undefined,
    source_system_id: searchParams.get("source_system_id") || undefined,
    category: searchParams.get("category") || undefined,
    date_from: searchParams.get("date_from") || undefined,
    date_to: searchParams.get("date_to") || undefined,
    limit: PAGE_SIZE,
    offset,
  });

  function goToOffset(next: number) {
    const params = new URLSearchParams(searchParams.toString());
    params.set("offset", String(next));
    router.push(`${pathname}?${params.toString()}`);
  }

  return (
    <main className="p-6">
      <h1 className="text-xl font-semibold text-heading">RCA of Incidents</h1>
      <IncidentFilters />

      {isError && <p className="mt-4 text-sm text-danger">Failed to load incidents.</p>}

      {(isLoading || data) && (
        <>
          <div className="mt-4 overflow-x-auto rounded-md border border-grid bg-white">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-grid text-left text-xs uppercase tracking-wide text-muted">
                  <th className="px-3 py-2">Incident ID</th>
                  <th className="px-3 py-2">Subject</th>
                  <th className="px-3 py-2">Classification</th>
                  <th className="px-3 py-2">RCA Outcome</th>
                  <th className="px-3 py-2">Last Run</th>
                  <th className="px-3 py-2">Source System</th>
                  <th className="px-3 py-2">Category</th>
                </tr>
              </thead>
              <tbody>
                {isLoading && <SkeletonTableRows />}
                {data?.items.map((row) => (
                  <tr key={row.id} className="border-b border-grid last:border-0 hover:bg-canvas">
                    <td className="px-3 py-2">
                      {/* Every incident has an incident_key: its Jira key, or a generated int_... ID. */}
                      <Link href={`/${row.incident_key}`} className="font-medium text-focus hover:underline">
                        {row.incident_key}
                      </Link>
                    </td>
                    <td className="max-w-xs truncate px-3 py-2 text-heading">{row.subject ?? "—"}</td>
                    <td className="px-3 py-2">
                      <span className={`rounded px-1.5 py-0.5 text-xs font-medium ${classificationBadgeClass(row.classification_status)}`}>
                        {classificationStatusLabel(row.classification_status)}
                      </span>
                      {classificationMethodAnnotation(row.classification_method, row.llm_confidence) && (
                        <span className="ml-1 text-xs text-muted">
                          ({classificationMethodAnnotation(row.classification_method, row.llm_confidence)})
                        </span>
                      )}
                    </td>
                    <td className="px-3 py-2">
                      {row.latest_rca_status ? (
                        <span className={`rounded px-1.5 py-0.5 text-xs font-medium ${rcaStatusBadgeClass(row.latest_rca_status)}`}>
                          {humanizeRcaStatus(row.latest_rca_status)}
                        </span>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td className="px-3 py-2 text-muted">
                      {row.latest_executed_at ? new Date(row.latest_executed_at).toLocaleString() : "—"}
                    </td>
                    <td className="px-3 py-2 text-muted">{row.source_system_id ?? "—"}</td>
                    <td className="px-3 py-2 text-muted">{row.category ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {data?.items.length === 0 && <p className="p-4 text-sm text-muted">No incidents match these filters.</p>}
          </div>

          {data && (
            <div className="mt-3 flex items-center justify-between text-sm text-muted">
              <span>
                {data.total === 0 ? 0 : offset + 1}-{Math.min(offset + PAGE_SIZE, data.total)} of {data.total}
              </span>
              <div className="flex gap-2">
                <button
                  type="button"
                  disabled={offset === 0}
                  onClick={() => goToOffset(Math.max(0, offset - PAGE_SIZE))}
                  className="rounded border border-grid px-2 py-1 disabled:opacity-40"
                >
                  Prev
                </button>
                <button
                  type="button"
                  disabled={offset + PAGE_SIZE >= data.total}
                  onClick={() => goToOffset(offset + PAGE_SIZE)}
                  className="rounded border border-grid px-2 py-1 disabled:opacity-40"
                >
                  Next
                </button>
              </div>
            </div>
          )}
        </>
      )}
    </main>
  );
}
