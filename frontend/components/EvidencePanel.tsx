"use client";

import { isSimulatedEvidence, type GraphTask } from "@/lib/workflowGraph";

const STATUS_BADGE_STYLES: Record<"OK" | "WARN" | "ERROR", string> = {
  OK: "bg-focus/10 text-focus",
  WARN: "bg-highlight/10 text-highlight",
  ERROR: "bg-danger/10 text-danger",
};

interface EvidencePanelProps {
  task: GraphTask | null;
  onClose?: () => void;
}

/**
 * Node-click side panel: "In" is
 * always the task's own params; "Out" (status + details) only renders when
 * the task carries evidence -- i.e. only in execution view, never for a
 * playbook/build-preview node that hasn't run.
 */
export function EvidencePanel({ task, onClose }: EvidencePanelProps) {
  if (!task) {
    return <p className="rounded-md border border-grid bg-white p-4 text-sm text-muted">Select a node to see its input/output.</p>;
  }

  const paramEntries = Object.entries(task.params);

  return (
    <div className="min-w-0 rounded-md border border-grid bg-white p-4">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h3 className="text-sm font-semibold text-heading">{task.label}</h3>
          <p className="mt-0.5 text-xs text-muted">
            call: <code className="text-heading">{task.call}</code>
            {task.functionVersionNumber != null && <> &middot; v{task.functionVersionNumber}</>}
          </p>
        </div>
        {onClose && (
          <button type="button" onClick={onClose} className="text-xs text-muted hover:text-heading">
            close
          </button>
        )}
      </div>

      <section className="mt-3">
        <h4 className="text-xs font-semibold uppercase tracking-wide text-muted">In</h4>
        {paramEntries.length === 0 ? (
          <p className="mt-1 text-sm text-muted">No parameters.</p>
        ) : (
          <dl className="mt-1 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
            {paramEntries.map(([key, value]) => (
              <div className="contents" key={key}>
                <dt className="text-muted">{key}</dt>
                <dd className="text-heading">{JSON.stringify(value)}</dd>
              </div>
            ))}
          </dl>
        )}
      </section>

      {task.evidence && (
        <section className="mt-3">
          <h4 className="text-xs font-semibold uppercase tracking-wide text-muted">Out</h4>
          <span className={`mt-1 inline-block rounded px-1.5 py-0.5 text-xs font-medium ${STATUS_BADGE_STYLES[task.evidence.status]}`}>
            {task.evidence.status}
          </span>
          <p className="mt-1 text-sm text-heading">{task.evidence.details}</p>
          {isSimulatedEvidence(task.evidence.details) && (
            <p className="mt-1 text-xs text-highlight">Simulated evidence -- not real telemetry.</p>
          )}
        </section>
      )}
    </div>
  );
}
