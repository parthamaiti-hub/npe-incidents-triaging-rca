"use client";

import Link from "next/link";
import { useParams } from "next/navigation";

import { ExecutionView } from "@/components/ExecutionView";
import { FeedbackForm } from "@/components/FeedbackForm";
import { classificationBadgeClass, classificationMethodAnnotation, classificationStatusLabel } from "@/lib/classification";
import { useIncidentByJiraKey } from "@/lib/queries/incidents";
import { useExecution } from "@/lib/queries/workflows";

// Tab 1 detail: incident + its latest RCA attempt's
// sections, executed-workflow graph, and feedback. Deliberately reads only
// stored data (GET /incidents/dashboard + GET /workflows/executions/{id})
// -- never POST /rca/{jira_key}, which always does a live Jira refetch and
// would silently re-trigger classification on every page load.
export default function IncidentDetailPage() {
  const { jiraKey } = useParams<{ jiraKey: string }>();
  const { data: incident, isLoading: loadingIncident, isError: incidentQueryError } = useIncidentByJiraKey(jiraKey);
  const { data: execution, isLoading: loadingExecution } = useExecution(incident?.latest_execution_id ?? undefined);

  if (loadingIncident) {
    return (
      <main className="p-6">
        <p className="text-sm text-muted">Loading…</p>
      </main>
    );
  }

  if (incidentQueryError) {
    return (
      <main className="p-6">
        <p className="text-sm text-danger">Failed to load incident {jiraKey}.</p>
      </main>
    );
  }

  if (!incident) {
    return (
      <main className="p-6">
        <p className="text-sm text-muted">No ingested incident found for {jiraKey}.</p>
      </main>
    );
  }

  return (
    <main className="p-6">
      <Link href="/" className="text-sm text-focus hover:underline">
        &larr; back to dashboard
      </Link>
      <h1 className="mt-2 text-xl font-semibold text-heading">{incident.jira_key}</h1>
      <p className="text-sm text-muted">{incident.subject}</p>

      <div className="mt-3 flex flex-wrap items-center gap-3 text-sm">
        <span className={`rounded px-1.5 py-0.5 text-xs font-medium ${classificationBadgeClass(incident.classification_status)}`}>
          Classification: {classificationStatusLabel(incident.classification_status)}
        </span>
        {classificationMethodAnnotation(incident.classification_method, incident.llm_confidence) && (
          <span className="text-muted">
            ({classificationMethodAnnotation(incident.classification_method, incident.llm_confidence)})
          </span>
        )}
        <span className="text-muted">
          {incident.source_system_id ?? "—"} / {incident.category ?? "—"}
        </span>
        {incident.status && <span className="text-muted">Jira status: {incident.status}</span>}
        <Link href={`/retry?q=${incident.jira_key}`} className="text-focus hover:underline">
          Retry RCA
        </Link>
      </div>

      <section className="mt-6">
        {loadingExecution ? (
          <p className="text-sm text-muted">Loading…</p>
        ) : execution ? (
          <ExecutionView execution={execution} />
        ) : (
          <p className="text-sm text-muted">No RCA available for this incident yet.</p>
        )}
      </section>

      {execution && (
        <section className="mt-6">
          <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">Feedback</h2>
          <div className="mt-2">
            <FeedbackForm executionId={execution.id} />
          </div>
        </section>
      )}
    </main>
  );
}
