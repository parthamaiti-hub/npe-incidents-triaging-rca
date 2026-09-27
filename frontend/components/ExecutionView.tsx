"use client";

import { useState } from "react";

import { EvidencePanel } from "@/components/EvidencePanel";
import { RcaSections } from "@/components/RcaSections";
import { WorkflowGraph } from "@/components/WorkflowGraph";
import { asRcaResult } from "@/lib/rca";
import { toGraphTasks, type GraphTask } from "@/lib/workflowGraph";

export interface ExecutionViewData {
  document_snapshot: unknown[];
  evidence: unknown[] | null;
  rca: unknown;
  status: string;
  rca_status: string | null;
  rca_meta?: unknown;
  operator_context?: string | null;
}

/**
 * One execution's RCA sections + graph + evidence panel -- shared between
 * the incident detail page (its latest execution), the retry page's
 * just-returned result, and the per-execution route
 * (`[jiraKey]/executions/[executionId]`), so "one run's view" is the same
 * component everywhere, not three copies of the same markup.
 */
export function ExecutionView({ execution }: { execution: ExecutionViewData }) {
  const [selected, setSelected] = useState<GraphTask | null>(null);
  const tasks = toGraphTasks(execution.document_snapshot, execution.evidence);
  // Evidence gathered, LLM-primary RCA synthesis still in
  // flight (app.rca_worker hasn't written it back yet) -- a real, possibly
  // longer-lived state now, not a rare race window.
  const rcaPending = execution.status === "completed" && execution.rca_status === null;

  return (
    <div className="space-y-4">
      <div>
        <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">RCA Outcome</h2>
        <div className="mt-2">
          {rcaPending ? (
            <div className="flex items-center gap-2 rounded-md border border-grid bg-white p-4 text-sm text-muted">
              <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-highlight" aria-hidden />
              RCA synthesis in progress…
            </div>
          ) : (
            <RcaSections rca={asRcaResult(execution.rca)} llmGenerated={execution.rca_meta != null} />
          )}
          {execution.operator_context && (
            <p className="mt-2 text-xs text-muted">
              <span className="font-medium">Operator context supplied:</span> {execution.operator_context}
            </p>
          )}
        </div>
      </div>

      <div>
        <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">Executed Workflow</h2>
        <div className="mt-2 grid grid-cols-1 gap-4 lg:grid-cols-[2fr_1fr]">
          <WorkflowGraph tasks={tasks} selectedTaskId={selected?.id ?? null} onSelectTask={setSelected} />
          <EvidencePanel task={selected} onClose={() => setSelected(null)} />
        </div>
      </div>
    </div>
  );
}
