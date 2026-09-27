"use client";

import Link from "next/link";
import { useParams } from "next/navigation";

import { ExecutionView } from "@/components/ExecutionView";
import { humanizeRcaStatus, rcaStatusBadgeClass } from "@/lib/rca";
import { useExecution } from "@/lib/queries/workflows";

// One run's view -- shared with the incident detail page
// (its latest execution) and the retry page's just-returned result, all
// via the same `ExecutionView` component.
export default function ExecutionDetailPage() {
  const { jiraKey, executionId } = useParams<{ jiraKey: string; executionId: string }>();
  const { data: execution, isLoading, isError } = useExecution(executionId);

  if (isLoading) {
    return (
      <main className="p-6">
        <p className="text-sm text-muted">Loading…</p>
      </main>
    );
  }

  if (isError || !execution) {
    return (
      <main className="p-6">
        <p className="text-sm text-danger">Execution not found.</p>
      </main>
    );
  }

  return (
    <main className="p-6">
      <Link href={`/${jiraKey}`} className="text-sm text-focus hover:underline">
        &larr; back to {jiraKey}
      </Link>
      <h1 className="mt-2 text-xl font-semibold text-heading">Execution</h1>
      <div className="mt-1 flex flex-wrap items-center gap-3 text-sm text-muted">
        <span>{new Date(execution.started_at).toLocaleString()}</span>
        <span>triggered by: {execution.triggered_by}</span>
        {execution.requested_by && <span>by {execution.requested_by}</span>}
        {execution.mapping_overridden && <span className="text-highlight">mapping overridden</span>}
        {execution.rca_status && (
          <span className={`rounded px-1.5 py-0.5 text-xs font-medium ${rcaStatusBadgeClass(execution.rca_status)}`}>
            {humanizeRcaStatus(execution.rca_status)}
          </span>
        )}
      </div>

      <div className="mt-4">
        <ExecutionView execution={execution} />
      </div>
    </main>
  );
}
