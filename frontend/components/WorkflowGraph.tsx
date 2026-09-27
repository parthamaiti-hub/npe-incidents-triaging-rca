"use client";

import { useMemo } from "react";
import dagre from "@dagrejs/dagre";
import { Background, Handle, Position, ReactFlow, type Edge, type Node, type NodeProps } from "@xyflow/react";
import "@xyflow/react/dist/style.css";

import { isSimulatedEvidence, type GraphTask } from "@/lib/workflowGraph";

const NODE_WIDTH = 220;
const NODE_HEIGHT = 64;

/** Edit-mode actions on one step, by its index in the task list. */
export interface WorkflowGraphEditActions {
  onMoveUp: (index: number) => void;
  onMoveDown: (index: number) => void;
  onEdit: (index: number) => void;
  onInsertAfter: (index: number) => void;
  onDelete: (index: number) => void;
}

interface TaskNodeData {
  task: GraphTask;
  isFirst: boolean;
  isLast: boolean;
  hasError: boolean;
  actions: WorkflowGraphEditActions | null;
  [key: string]: unknown;
}

function layout(
  tasks: GraphTask[],
  errorIndexes: ReadonlySet<number>,
  actions: WorkflowGraphEditActions | null,
): { nodes: Node[]; edges: Edge[] } {
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: "TB", nodesep: 40, ranksep: 56 });
  g.setDefaultEdgeLabel(() => ({}));

  for (const task of tasks) {
    g.setNode(task.id, { width: NODE_WIDTH, height: NODE_HEIGHT });
  }
  for (let i = 0; i < tasks.length - 1; i++) {
    g.setEdge(tasks[i].id, tasks[i + 1].id);
  }
  dagre.layout(g);

  const nodes: Node[] = tasks.map((task, i) => {
    const pos = g.node(task.id);
    const data: TaskNodeData = {
      task,
      isFirst: i === 0,
      isLast: i === tasks.length - 1,
      hasError: errorIndexes.has(task.index),
      actions,
    };
    return {
      id: task.id,
      type: "task",
      position: { x: pos.x - NODE_WIDTH / 2, y: pos.y - NODE_HEIGHT / 2 },
      data,
      draggable: false,
    };
  });

  const edges: Edge[] = tasks.slice(0, -1).map((task, i) => ({
    id: `${task.id}->${tasks[i + 1].id}`,
    source: task.id,
    target: tasks[i + 1].id,
  }));

  return { nodes, edges };
}

const STATUS_STYLES: Record<"OK" | "WARN" | "ERROR", { border: string; dot: string }> = {
  OK: { border: "border-focus", dot: "bg-focus" },
  WARN: { border: "border-highlight", dot: "bg-highlight" },
  ERROR: { border: "border-danger", dot: "bg-danger" },
};

function StepButton({ label, onClick, disabled, children }: { label: string; onClick: () => void; disabled?: boolean; children: React.ReactNode }) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      disabled={disabled}
      // nodrag/nopan: React Flow would otherwise treat the press as a canvas gesture.
      className="nodrag nopan rounded px-1 leading-none text-muted hover:bg-canvas hover:text-heading disabled:opacity-30"
      onClick={(e) => {
        e.stopPropagation(); // don't also fire the node's own click
        onClick();
      }}
    >
      {children}
    </button>
  );
}

function TaskNode({ data, selected }: NodeProps) {
  const { task, isFirst, isLast, hasError, actions } = data as TaskNodeData;
  const status = task.evidence?.status ?? null;
  const style = hasError ? STATUS_STYLES.ERROR : status ? STATUS_STYLES[status] : { border: "border-grid", dot: "bg-muted" };
  const simulated = task.evidence != null && isSimulatedEvidence(task.evidence.details);

  return (
    <div
      className={`rounded-lg border-2 bg-white px-3 py-2 shadow-sm ${style.border} ${selected ? "ring-2 ring-action" : ""}`}
      style={{ width: NODE_WIDTH }}
      data-testid="workflow-step"
      data-error={hasError ? "true" : undefined}
    >
      <Handle type="target" position={Position.Top} style={{ background: "var(--grid)" }} />
      <div className="flex items-center gap-2">
        {status && <span className={`h-2 w-2 shrink-0 rounded-full ${style.dot}`} aria-hidden />}
        <span className="truncate text-sm font-medium text-heading">{task.label}</span>
      </div>
      <div className="mt-1 flex items-center gap-2 text-xs text-muted">
        {task.functionVersionNumber != null && <span>v{task.functionVersionNumber}</span>}
        {simulated && <span className="rounded bg-highlight/10 px-1 text-highlight">simulated</span>}
        {actions && (
          <span className="ml-auto flex items-center gap-0.5">
            <StepButton label={`Move ${task.label} up`} disabled={isFirst} onClick={() => actions.onMoveUp(task.index)}>
              ↑
            </StepButton>
            <StepButton label={`Move ${task.label} down`} disabled={isLast} onClick={() => actions.onMoveDown(task.index)}>
              ↓
            </StepButton>
            <StepButton label={`Edit ${task.label}`} onClick={() => actions.onEdit(task.index)}>
              ✎
            </StepButton>
            <StepButton label={`Insert step after ${task.label}`} onClick={() => actions.onInsertAfter(task.index)}>
              +
            </StepButton>
            <StepButton label={`Delete ${task.label}`} onClick={() => actions.onDelete(task.index)}>
              ✕
            </StepButton>
          </span>
        )}
      </div>
      <Handle type="source" position={Position.Bottom} style={{ background: "var(--grid)" }} />
    </div>
  );
}

const nodeTypes = { task: TaskNode };

const NO_ERRORS: ReadonlySet<number> = new Set();

export interface WorkflowGraphProps {
  tasks: GraphTask[];
  selectedTaskId?: string | null;
  onSelectTask?: (task: GraphTask) => void;
  /** Edit mode: per-step toolbar (reorder, edit, insert, delete). */
  editActions?: WorkflowGraphEditActions | null;
  /** Task indexes to outline as failing validation. */
  errorTaskIndexes?: ReadonlySet<number>;
}

/**
 * One shared graph renderer for 4 call sites:
 * - playbook view: `document` tasks, no `evidence` -> no status coloring.
 * - execution view: `document_snapshot` tasks joined to `evidence` by
 *   index -> nodes colored OK/WARN/ERROR, simulated-evidence badge.
 * - build preview: `generated_document`, same as playbook view.
 * - playbook edit: `editActions` adds a per-step toolbar; validation
 *   failures are outlined via `errorTaskIndexes`.
 *
 * The model is a plain ordered task list, so edges are always derived from
 * order and reordering is done with the up/down buttons, not by dragging.
 */
export function WorkflowGraph({ tasks, selectedTaskId, onSelectTask, editActions = null, errorTaskIndexes = NO_ERRORS }: WorkflowGraphProps) {
  const { nodes, edges } = useMemo(() => layout(tasks, errorTaskIndexes, editActions), [tasks, errorTaskIndexes, editActions]);
  const styledNodes = useMemo(
    () => nodes.map((node) => ({ ...node, selected: node.id === selectedTaskId })),
    [nodes, selectedTaskId],
  );

  if (tasks.length === 0) {
    return <p className="rounded-md border border-grid bg-white p-4 text-sm text-muted">No tasks in this workflow.</p>;
  }

  return (
    <div
      style={{ height: Math.max(240, tasks.length * 110) }}
      className="min-w-0 overflow-hidden rounded-md border border-grid bg-canvas"
    >
      <ReactFlow
        // Remount when the step count changes so fitView re-frames added/removed steps.
        key={tasks.length}
        nodes={styledNodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onNodeClick={(_, node) => {
          const task = tasks.find((t) => t.id === node.id);
          if (task) onSelectTask?.(task);
        }}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
        zoomOnScroll={false}
        panOnScroll
        proOptions={{ hideAttribution: true }}
        fitView
      >
        <Background />
      </ReactFlow>
    </div>
  );
}
