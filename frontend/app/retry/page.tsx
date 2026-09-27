"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useRef, useState } from "react";

import { ExecutionView } from "@/components/ExecutionView";
import { IncidentFilters } from "@/components/IncidentFilters";
import { useOperatorIdentity } from "@/components/OperatorIdentityProvider";
import { classificationBadgeClass, classificationMethodAnnotation, classificationStatusLabel } from "@/lib/classification";
import { useIncidentsDashboard, useRetryIncident } from "@/lib/queries/incidents";
import { useExecution, useExecutionsForJiraKey, useWorkflowDefinitions, useWorkflowVersions } from "@/lib/queries/workflows";
import { humanizeRcaStatus, rcaStatusBadgeClass } from "@/lib/rca";

const PAGE_SIZE = 20;

type IncidentRow = NonNullable<ReturnType<typeof useIncidentsDashboard>["data"]>["items"][number];

// Tab 3 -- Retry RCA. Left: the same incident
// picker/filters as tab 1, single-select. Right: current mapping, an
// optional override, Execute, and full retry history -- all rendered with
// the same components tabs 1/2 already built (WorkflowGraph, EvidencePanel,
// RcaSections via ExecutionView), not re-implemented.
export default function RetryPage() {
  return (
    <Suspense fallback={<main className="p-6 text-sm text-muted">Loading…</main>}>
      <RetryPageContent />
    </Suspense>
  );
}

function RetryPageContent() {
  const searchParams = useSearchParams();
  const offset = Number(searchParams.get("offset") ?? "0");
  const [selectedJiraKey, setSelectedJiraKey] = useState<string | null>(null);

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

  const selectedIncident = data?.items.find((item) => item.jira_key === selectedJiraKey) ?? null;

  return (
    <main className="p-6">
      <h1 className="text-xl font-semibold text-heading">Retry RCA</h1>
      <IncidentFilters />

      {isLoading && <p className="mt-4 text-sm text-muted">Loading…</p>}
      {isError && <p className="mt-4 text-sm text-danger">Failed to load incidents.</p>}

      {data && (
        <div className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-[1fr_1fr]">
          <div className="overflow-x-auto rounded-md border border-grid bg-white">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-grid text-left text-xs uppercase tracking-wide text-muted">
                  <th className="px-3 py-2"></th>
                  <th className="px-3 py-2">Jira Key</th>
                  <th className="px-3 py-2">Classification</th>
                  <th className="px-3 py-2">RCA Outcome</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((row) => (
                  <tr
                    key={row.id}
                    onClick={() => setSelectedJiraKey(row.jira_key)}
                    className={`cursor-pointer border-b border-grid last:border-0 hover:bg-canvas ${
                      row.jira_key === selectedJiraKey ? "bg-canvas" : ""
                    }`}
                  >
                    <td className="px-3 py-2">
                      <input type="radio" checked={row.jira_key === selectedJiraKey} readOnly disabled={!row.jira_key} />
                    </td>
                    <td className="px-3 py-2 font-medium text-heading">{row.jira_key ?? "—"}</td>
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
                  </tr>
                ))}
              </tbody>
            </table>
            {data.items.length === 0 && <p className="p-4 text-sm text-muted">No incidents match these filters.</p>}
          </div>

          <div>
            {selectedIncident ? (
              <RetryPanel incident={selectedIncident} />
            ) : (
              <p className="text-sm text-muted">Select an incident on the left.</p>
            )}
          </div>
        </div>
      )}
    </main>
  );
}

function RetryPanel({ incident }: { incident: IncidentRow }) {
  const jiraKey = incident.jira_key!;
  const { data: definitions } = useWorkflowDefinitions();
  const definition = definitions?.find(
    (d) => d.source_system_id === incident.source_system_id && d.category === incident.category,
  );
  const { data: versions } = useWorkflowVersions(definition?.id);
  const { data: latestExecution } = useExecution(incident.latest_execution_id ?? undefined);
  const { data: history } = useExecutionsForJiraKey(jiraKey);

  const { name: requestedBy } = useOperatorIdentity();
  const [overrideEnabled, setOverrideEnabled] = useState(false);
  const overrideSelectRef = useRef<HTMLSelectElement>(null);
  const [operatorContext, setOperatorContext] = useState("");
  const retry = useRetryIncident();

  const retryableVersions = versions?.filter((v) => v.status === "approved" || v.status === "superseded") ?? [];
  const currentMappingVersion = versions?.find((v) => v.id === latestExecution?.workflow_definition_version_id);
  const defaultVersionId = latestExecution?.workflow_definition_version_id ?? retryableVersions.find((v) => v.status === "approved")?.id ?? "";

  function handleExecute() {
    if (!requestedBy) return;
    const override = overrideEnabled ? overrideSelectRef.current?.value : undefined;
    retry.mutate({
      jiraKey,
      workflowDefinitionVersionId: override || undefined,
      requestedBy,
      operatorContext: operatorContext.trim() || undefined,
    });
  }

  return (
    <div className="space-y-4">
      <div className="rounded-md border border-grid bg-white p-4">
        <h2 className="text-sm font-semibold text-heading">{incident.jira_key}</h2>
        <p className="text-xs text-muted">{incident.subject}</p>

        <p className="mt-2 text-xs text-muted">
          Current mapping:{" "}
          {currentMappingVersion
            ? `v${currentMappingVersion.version_number} (${currentMappingVersion.status})`
            : "active workflow (no prior run)"}
          {latestExecution?.mapping_overridden && " -- previously overridden"}
        </p>

        <label className="mt-3 flex items-center gap-2 text-xs text-muted">
          <input type="checkbox" checked={overrideEnabled} onChange={(e) => setOverrideEnabled(e.target.checked)} />
          Edit mapping
        </label>

        {overrideEnabled && retryableVersions.length > 0 && (
          <select
            key={defaultVersionId}
            ref={overrideSelectRef}
            defaultValue={defaultVersionId}
            className="mt-2 block w-56 rounded border border-grid px-2 py-1 text-sm text-heading"
          >
            {retryableVersions.map((v) => (
              <option key={v.id} value={v.id}>
                v{v.version_number} ({v.status})
              </option>
            ))}
          </select>
        )}

        <label className="mt-3 block text-xs text-muted">
          Additional context for RCA (optional)
          <textarea
            value={operatorContext}
            onChange={(e) => setOperatorContext(e.target.value)}
            rows={2}
            maxLength={2000}
            placeholder="e.g. known related change, a hint the automated evidence won't have"
            className="mt-1 block w-full rounded border border-grid px-2 py-1 text-sm text-heading"
          />
        </label>

        {retry.isError && <p className="mt-2 text-sm text-danger">{(retry.error as Error).message}</p>}

        <div className="mt-3 flex items-center gap-3">
          <button
            type="button"
            onClick={handleExecute}
            disabled={!requestedBy || retry.isPending}
            className="rounded bg-action px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
          >
            {retry.isPending ? "Executing…" : "Execute"}
          </button>
          <span className="text-xs text-muted">as {requestedBy ?? "…"}</span>
        </div>
      </div>

      {retry.isSuccess && retry.data && (
        <div className="rounded-md border border-grid bg-white p-4">
          <p className="text-xs text-muted">
            {retry.data.mapping_overridden ? "Mapping overridden for this run" : "Ran the active workflow"} &middot; triggered_by=
            {retry.data.triggered_by}
          </p>
          <div className="mt-3">
            <ExecutionView execution={retry.data} />
          </div>
        </div>
      )}

      <div className="rounded-md border border-grid bg-white p-4">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-muted">Retry History</h3>
        {history && history.length === 0 && <p className="mt-1 text-sm text-muted">No executions yet.</p>}
        <ul className="mt-2 divide-y divide-grid text-sm">
          {history?.map((execution) => (
            <li key={execution.id} className="flex items-center justify-between py-2">
              <Link href={`/${jiraKey}/executions/${execution.id}`} className="text-focus hover:underline">
                {new Date(execution.started_at).toLocaleString()}
              </Link>
              <span className="text-xs text-muted">
                {execution.triggered_by}
                {execution.mapping_overridden ? " (overridden)" : ""} &middot;{" "}
                {execution.rca_status ? humanizeRcaStatus(execution.rca_status) : "—"} &middot; {execution.status}
              </span>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
