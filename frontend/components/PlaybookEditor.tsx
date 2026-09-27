"use client";

import dynamic from "next/dynamic";
import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";

import { useOperatorIdentity } from "@/components/OperatorIdentityProvider";
import { StepEditorPanel } from "@/components/StepEditorPanel";
import { WorkflowGraph, type WorkflowGraphEditActions } from "@/components/WorkflowGraph";
import { useFunctions } from "@/lib/queries/functions";
import {
  WorkflowApiError,
  useApproveBuildRequest,
  useLiveYamlValidation,
  useRejectBuildRequest,
  useRenderYaml,
  useSubmitEdit,
  useValidateYaml,
} from "@/lib/queries/workflows";
import {
  insertTask,
  moveTask,
  playbookName,
  removeTask,
  replaceTask,
  toDraftTasks,
  type ApiIssue,
  type DraftTask,
  type FunctionSpec,
} from "@/lib/workflowDraft";
import { toGraphTasks } from "@/lib/workflowGraph";

const YamlEditor = dynamic(() => import("@/components/YamlEditor").then((m) => m.YamlEditor), {
  ssr: false,
  loading: () => <p className="rounded-md border border-grid bg-white p-4 text-sm text-muted">Loading editor…</p>,
});

interface PendingRequest {
  id: string;
  generated_document: unknown[] | null;
}

type StepPanel = { mode: "edit"; index: number } | { mode: "insert"; index: number } | null;

interface DraftState {
  tasks: DraftTask[];
  yamlText: string;
  /** YAML pane has edits not yet applied to the graph. */
  yamlDirty: boolean;
  /** Anything changed since editing began. */
  changed: boolean;
  /** Issues from the last explicit Apply/Save (live issues are separate). */
  issues: ApiIssue[];
  issueSummary: string | null;
  stepPanel: StepPanel;
  /** Saved but not yet approved/rejected edit request. */
  pending: PendingRequest | null;
}

type Action =
  | { type: "graphEdit"; tasks: DraftTask[] }
  | { type: "yamlRendered"; yaml: string }
  | { type: "yamlEdit"; yaml: string }
  | { type: "yamlApplied"; tasks: DraftTask[] }
  | { type: "failed"; issues: ApiIssue[]; summary: string }
  | { type: "stepPanel"; panel: StepPanel }
  | { type: "saved"; request: PendingRequest }
  | { type: "rejected" };

function reducer(state: DraftState, action: Action): DraftState {
  switch (action.type) {
    case "graphEdit":
      return { ...state, tasks: action.tasks, changed: true, issues: [], issueSummary: null, stepPanel: null };
    case "yamlRendered":
      return state.yamlDirty ? state : { ...state, yamlText: action.yaml };
    case "yamlEdit":
      // Live validation takes over reporting once the text changes.
      return { ...state, yamlText: action.yaml, yamlDirty: true, changed: true, issues: [], issueSummary: null };
    case "yamlApplied":
      return { ...state, tasks: action.tasks, yamlDirty: false, issues: [], issueSummary: null };
    case "failed":
      return { ...state, issues: action.issues, issueSummary: action.summary };
    case "stepPanel":
      return { ...state, stepPanel: action.panel };
    case "saved":
      return {
        ...state,
        pending: action.request,
        tasks: toDraftTasks(action.request.generated_document ?? []),
        yamlDirty: false,
        issues: [],
        issueSummary: null,
        stepPanel: null,
      };
    case "rejected":
      return { ...state, pending: null };
  }
}

function describeError(error: unknown): { issues: ApiIssue[]; summary: string } {
  if (error instanceof WorkflowApiError) {
    const kind = error.kind === "syntax" ? "YAML syntax error" : error.kind === "schema" ? "Unsupported workflow structure" : error.kind === "registry" ? "Invalid step" : null;
    const summary = kind ? `${kind} -- nothing was saved.` : error.message;
    return { issues: error.issues, summary };
  }
  return { issues: [], summary: error instanceof Error ? error.message : String(error) };
}

export interface PlaybookEditorProps {
  definition: { id: string; source_system_id: string; category: string };
  baseVersion: { id: string; version_number: number; document: unknown[] };
  initialYaml: string;
  /** Leave edit mode; `publishedVersionId` is set when an edit was approved. */
  onExit: (publishedVersionId?: string) => void;
}

/**
 * Edit mode for an approved playbook version: the CNCF graph and the YAML
 * side by side over one shared draft. Saving creates a pending edit request
 * (version N+1 once approved); the base version itself is never modified.
 */
export function PlaybookEditor({ definition, baseVersion, initialYaml, onExit }: PlaybookEditorProps) {
  const { name: operator } = useOperatorIdentity();
  const { data: functions } = useFunctions();
  const [state, dispatch] = useReducer(reducer, null, () => ({
    tasks: toDraftTasks(baseVersion.document),
    yamlText: initialYaml,
    yamlDirty: false,
    changed: false,
    issues: [],
    issueSummary: null,
    stepPanel: null,
    pending: null,
  }));
  const [changeNote, setChangeNote] = useState("");
  const [confirmDiscard, setConfirmDiscard] = useState(false);
  const [conflict, setConflict] = useState<string | null>(null);

  const { mutate: renderYaml } = useRenderYaml(); // mutate is referentially stable
  const validateYaml = useValidateYaml();
  const submitEdit = useSubmitEdit();
  const approve = useApproveBuildRequest();
  const reject = useRejectBuildRequest();

  const name = playbookName(definition.source_system_id, definition.category);
  const locked = state.pending !== null;

  // Live validation while the YAML pane has unapplied edits (debounced).
  const debouncedYaml = useDebouncedValue(state.yamlText, 500);
  const live = useLiveYamlValidation(debouncedYaml, baseVersion.id, state.yamlDirty && !locked);
  const liveIssues = state.yamlDirty && live.error instanceof WorkflowApiError ? live.error.issues : null;
  const issues = liveIssues ?? state.issues;
  const errorTaskIndexes = useMemo(
    () => new Set(issues.map((i) => i.task_index).filter((i): i is number => i != null)),
    [issues],
  );

  // Warn before leaving the page with unsaved work.
  useEffect(() => {
    if (!state.changed || locked) return;
    const handler = (e: BeforeUnloadEvent) => e.preventDefault();
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [state.changed, locked]);

  // Graph edits regenerate the YAML; only the latest request's result is kept.
  const renderSeq = useRef(0);
  const applyGraphEdit = useCallback(
    (tasks: DraftTask[]) => {
      dispatch({ type: "graphEdit", tasks });
      const seq = ++renderSeq.current;
      renderYaml(
        { tasks, name },
        {
          onSuccess: (yaml) => {
            if (seq === renderSeq.current) dispatch({ type: "yamlRendered", yaml });
          },
        },
      );
    },
    [name, renderYaml],
  );

  const graphLocked = locked || state.yamlDirty;
  const editActions = useMemo<WorkflowGraphEditActions | null>(
    () =>
      graphLocked
        ? null
        : {
            onMoveUp: (i) => applyGraphEdit(moveTask(state.tasks, i, -1)),
            onMoveDown: (i) => applyGraphEdit(moveTask(state.tasks, i, 1)),
            onEdit: (i) => dispatch({ type: "stepPanel", panel: { mode: "edit", index: i } }),
            onInsertAfter: (i) => dispatch({ type: "stepPanel", panel: { mode: "insert", index: i + 1 } }),
            onDelete: (i) => applyGraphEdit(removeTask(state.tasks, i)),
          },
    [graphLocked, state.tasks, applyGraphEdit],
  );

  function onApplyYaml() {
    validateYaml.mutate(
      { yaml: state.yamlText, baseVersionId: baseVersion.id },
      {
        onSuccess: (tasks) => dispatch({ type: "yamlApplied", tasks }),
        onError: (error) => dispatch({ type: "failed", ...describeError(error) }),
      },
    );
  }

  function onSave() {
    setConflict(null);
    submitEdit.mutate(
      {
        definitionId: definition.id,
        baseVersionId: baseVersion.id,
        editedBy: operator ?? "unknown",
        changeNote: changeNote.trim() || undefined,
        ...(state.yamlDirty ? { yaml: state.yamlText } : { tasks: state.tasks }),
      },
      {
        onSuccess: (request) => dispatch({ type: "saved", request }),
        onError: (error) => {
          if (error instanceof WorkflowApiError && error.status === 409) setConflict(error.message);
          else dispatch({ type: "failed", ...describeError(error) });
        },
      },
    );
  }

  function onApprove() {
    if (!state.pending) return;
    approve.mutate(
      { requestId: state.pending.id, approvedBy: operator ?? "unknown" },
      {
        onSuccess: (version) => onExit(version.id),
        onError: (error) => {
          if (error instanceof WorkflowApiError && error.status === 409) setConflict(error.message);
        },
      },
    );
  }

  function onReject() {
    if (!state.pending) return;
    reject.mutate(
      { requestId: state.pending.id, rejectedBy: operator ?? "unknown", reason: "Rejected from playbook editor" },
      { onSuccess: () => dispatch({ type: "rejected" }) },
    );
  }

  function onDiscard() {
    if (state.changed && !confirmDiscard) {
      setConfirmDiscard(true);
      return;
    }
    onExit();
  }

  const graphTasks = useMemo(() => toGraphTasks(state.tasks), [state.tasks]);
  const panel = state.stepPanel;
  const busy = submitEdit.isPending || validateYaml.isPending || approve.isPending || reject.isPending;

  return (
    <div className="mt-4">
      <div className="flex flex-wrap items-center gap-2 rounded-md border border-grid bg-white p-3">
        <span className="text-sm font-medium text-heading">Editing v{baseVersion.version_number}</span>
        {!locked && (
          <>
            <input
              value={changeNote}
              onChange={(e) => setChangeNote(e.target.value)}
              placeholder="Change note (optional)"
              aria-label="Change note"
              className="min-w-48 flex-1 rounded border border-grid px-2 py-1 text-sm text-heading"
            />
            <button
              type="button"
              onClick={onApplyYaml}
              disabled={!state.yamlDirty || busy}
              className="rounded border border-grid px-3 py-1.5 text-sm text-heading disabled:opacity-50"
            >
              Validate &amp; Apply
            </button>
            <button
              type="button"
              onClick={onSave}
              disabled={!state.changed || state.tasks.length === 0 || busy}
              className="rounded bg-focus px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
            >
              {submitEdit.isPending ? "Saving…" : "Save as new version"}
            </button>
            <button type="button" onClick={onDiscard} disabled={busy} className="rounded border border-grid px-3 py-1.5 text-sm text-heading">
              {confirmDiscard ? "Confirm discard" : "Discard"}
            </button>
          </>
        )}
      </div>

      {conflict && (
        <div className="mt-3 rounded-md border border-danger bg-white p-3 text-sm text-danger" role="alert">
          {conflict}{" "}
          <button type="button" onClick={() => onExit()} className="ml-2 underline">
            Reload
          </button>
        </div>
      )}

      {locked && (
        <div className="mt-3 rounded-md border border-focus bg-white p-3 text-sm text-heading" role="status">
          Saved as a pending edit of v{baseVersion.version_number}. The graph below shows the saved draft; nothing runs until it&apos;s
          approved.
          <div className="mt-2 flex gap-2">
            <button
              type="button"
              onClick={onApprove}
              disabled={busy}
              className="rounded bg-action px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
            >
              {approve.isPending ? "Publishing…" : "Approve & publish"}
            </button>
            <button type="button" onClick={onReject} disabled={busy} className="rounded border border-grid px-3 py-1.5 text-sm text-heading">
              Reject &amp; keep editing
            </button>
          </div>
          {reject.error && <p className="mt-2 text-danger">{reject.error.message}</p>}
          {approve.error && !conflict && <p className="mt-2 text-danger">{approve.error.message}</p>}
        </div>
      )}

      {(state.issueSummary || issues.length > 0) && !locked && (
        <div className="mt-3 rounded-md border border-danger bg-white p-3 text-sm" role="alert" data-testid="playbook-errors">
          <p className="font-medium text-danger">
            {state.issueSummary ?? (liveIssues ? "The YAML has problems -- fix them before applying or saving." : null)}
          </p>
          <ul className="mt-1 list-disc pl-5 text-danger">
            {issues.map((issue, i) => (
              <li key={i}>
                {issue.line != null && <span className="text-muted">line {issue.line}: </span>}
                {issue.path && <code className="text-heading">{issue.path}</code>} {issue.message}
              </li>
            ))}
          </ul>
        </div>
      )}

      {state.yamlDirty && !locked && (
        <p className="mt-3 rounded-md border border-highlight bg-white p-2 text-sm text-highlight">
          The YAML has changes not yet applied -- graph editing is paused until you Validate &amp; Apply or Save.
        </p>
      )}

      <div className="mt-3 grid grid-cols-1 gap-4 xl:grid-cols-2">
        <div className="min-w-0">
          <div className="mb-2 flex items-center justify-between">
            <h3 className="text-xs font-semibold uppercase tracking-wide text-muted">CNCF workflow graph</h3>
            {!graphLocked && (
              <button
                type="button"
                onClick={() => dispatch({ type: "stepPanel", panel: { mode: "insert", index: state.tasks.length } })}
                className="rounded border border-grid px-2 py-1 text-xs text-heading"
              >
                + Add step
              </button>
            )}
          </div>
          <WorkflowGraph
            tasks={graphTasks}
            selectedTaskId={panel?.mode === "edit" ? `task-${panel.index}` : null}
            onSelectTask={(task) => !graphLocked && dispatch({ type: "stepPanel", panel: { mode: "edit", index: task.index } })}
            editActions={editActions}
            errorTaskIndexes={errorTaskIndexes}
          />
          {panel && !graphLocked && functions && (
            <div className="mt-3">
              <StepEditorPanel
                key={`${panel.mode}-${panel.index}`}
                functions={functions as FunctionSpec[]}
                initial={panel.mode === "edit" ? state.tasks[panel.index] ?? null : null}
                title={panel.mode === "edit" ? `Edit step ${panel.index + 1}` : `New step at position ${panel.index + 1}`}
                submitLabel={panel.mode === "edit" ? "Update step" : "Add step"}
                onSubmit={(task) =>
                  applyGraphEdit(
                    panel.mode === "edit" ? replaceTask(state.tasks, panel.index, task) : insertTask(state.tasks, panel.index, task),
                  )
                }
                onCancel={() => dispatch({ type: "stepPanel", panel: null })}
              />
            </div>
          )}
        </div>

        <div className="min-w-0">
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">CNCF Serverless Workflow YAML</h3>
          <YamlEditor
            value={state.yamlText}
            onChange={(yaml) => dispatch({ type: "yamlEdit", yaml })}
            issues={locked ? [] : issues}
            readOnly={locked}
          />
        </div>
      </div>
    </div>
  );
}

function useDebouncedValue<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(value), delayMs);
    return () => clearTimeout(timer);
  }, [value, delayMs]);
  return debounced;
}
