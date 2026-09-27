// Draft state for editing an existing playbook: the task list the graph
// edits, plus the YAML pane's text. The graph and the YAML share one draft --
// graph edits regenerate the YAML immediately; YAML edits only reach the
// graph after a successful server validation ("Validate & Apply" or Save).

import type { components } from "@/lib/types";

export type RetryPolicy = components["schemas"]["RetryPolicy"];
export type FunctionSpec = components["schemas"]["FunctionSpec"];

/** One stored task, as in WorkflowDefinitionVersion.document. Pins are
 * server-stamped; the draft carries them through so unchanged steps keep
 * displaying their version, but the server re-derives them on save. */
export interface DraftTask {
  name?: string | null;
  call: string;
  with: Record<string, unknown>;
  retry?: RetryPolicy | null;
  function_version_id?: string | null;
  function_version_number?: number | null;
}

/** One validation problem, as returned in a 422's `detail.errors`. */
export interface ApiIssue {
  message: string;
  path: string;
  line: number | null;
  column: number | null;
  task_index: number | null;
}

export function toDraftTasks(document: unknown[]): DraftTask[] {
  return document.map((raw) => {
    const task = raw as DraftTask;
    return { ...task, with: { ...(task.with ?? {}) } };
  });
}

export function moveTask(tasks: DraftTask[], index: number, delta: -1 | 1): DraftTask[] {
  const target = index + delta;
  if (target < 0 || target >= tasks.length) return tasks;
  const next = [...tasks];
  [next[index], next[target]] = [next[target], next[index]];
  return next;
}

export function removeTask(tasks: DraftTask[], index: number): DraftTask[] {
  return tasks.filter((_, i) => i !== index);
}

/** Inserts at `index` (0 = first, tasks.length = last). */
export function insertTask(tasks: DraftTask[], index: number, task: DraftTask): DraftTask[] {
  return [...tasks.slice(0, index), task, ...tasks.slice(index)];
}

export function replaceTask(tasks: DraftTask[], index: number, task: DraftTask): DraftTask[] {
  return tasks.map((t, i) => (i === index ? task : t));
}

/** Mirrors the server's CNCF `document.name` (app/routers/workflows.py). */
export function playbookName(sourceSystemId: string, category: string): string {
  const slug = category
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return `${sourceSystemId}-${slug}`;
}
