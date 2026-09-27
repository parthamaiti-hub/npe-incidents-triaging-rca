// Shared shape + mapper for WorkflowGraph/EvidencePanel.
// The backend declares `document` / `document_snapshot` / `evidence` as
// plain `list[dict]` (JSONB) -- openapi-typescript can only type them as
// `{[key: string]: unknown}[]`, so this is where the *actual* known task/
// evidence shape (per app/workflow_spec.py's TaskSpec and
// app/playbook_engine.py's evidence entries) is asserted, once, for every
// consumer (playbook view, execution view, build preview).

export type GraphNodeStatus = "OK" | "WARN" | "ERROR";

export interface GraphTask {
  id: string;
  /** Position in the task list (the document is a plain ordered list). */
  index: number;
  label: string;
  call: string;
  functionVersionNumber: number | null;
  params: Record<string, unknown>;
  /** null when this task hasn't been executed (playbook/build-preview views). */
  evidence: { status: GraphNodeStatus; details: string } | null;
}

interface RawTask {
  name?: string | null;
  call: string;
  with?: Record<string, unknown>;
  function_version_number?: number | null;
}

interface RawEvidence {
  check?: string;
  status?: string;
  details?: string;
}

function normalizeStatus(status: string | undefined): GraphNodeStatus {
  return status === "WARN" || status === "ERROR" ? status : "OK";
}

/** `[STUBBED]` (v1 template stubs) / `[FUNCTIONAL DUMMY v2]` (v2 dummy code)
 * -- neither is real telemetry, so every node carrying either marker gets a
 * visible badge (mirrors the Jira comment's disclaimer panel rule in
 * app/adf_report.py). */
export function isSimulatedEvidence(details: string): boolean {
  return details.includes("[STUBBED]") || details.includes("[FUNCTIONAL DUMMY");
}

/**
 * Builds the graph's node list from a task document, optionally joined to
 * an evidence list by index (the engine appends evidence in task order,
 * with no reordering -- see app/playbook_engine.py::execute_workflow).
 * Passing no `evidence` (playbook view, build preview) yields nodes with
 * no status coloring, no In/Out split -- just the task's own params.
 */
export function toGraphTasks(document: unknown[], evidence?: unknown[] | null): GraphTask[] {
  return document.map((raw, i) => {
    const task = raw as RawTask;
    const rawEvidence = evidence?.[i] as RawEvidence | undefined;
    return {
      id: `task-${i}`,
      index: i,
      label: task.name ?? task.call,
      call: task.call,
      functionVersionNumber: task.function_version_number ?? null,
      params: task.with ?? {},
      evidence: rawEvidence
        ? { status: normalizeStatus(rawEvidence.status), details: rawEvidence.details ?? "" }
        : null,
    };
  });
}
