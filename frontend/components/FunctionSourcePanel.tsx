"use client";

import { FunctionSourceCode } from "@/components/FunctionSourceCode";
import type { GraphTask } from "@/lib/workflowGraph";

interface FunctionSourcePanelProps {
  task: GraphTask | null;
  onClose?: () => void;
}

/**
 * Playbook-tab node panel: the task's own params (read-only,
 * same as EvidencePanel's "In") plus the function's actual code at the
 * version this task is pinned to. Distinct from EvidencePanel -- that one is
 * about one execution's In/Out; this one is about the workflow definition
 * itself, so there's no "Out" to show.
 */
export function FunctionSourcePanel({ task, onClose }: FunctionSourcePanelProps) {
  if (!task) {
    return <p className="rounded-md border border-grid bg-white p-4 text-sm text-muted">Select a node to see its parameters and function source.</p>;
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
        <h4 className="text-xs font-semibold uppercase tracking-wide text-muted">Parameters</h4>
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

      <section className="mt-3">
        <h4 className="text-xs font-semibold uppercase tracking-wide text-muted">Function Source</h4>
        <FunctionSourceCode functionId={task.call} versionNumber={task.functionVersionNumber} />
      </section>
    </div>
  );
}
