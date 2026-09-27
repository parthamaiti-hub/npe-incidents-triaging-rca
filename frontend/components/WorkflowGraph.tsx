"use client";

import { useMemo } from "react";
import dagre from "@dagrejs/dagre";
import { Background, Handle, Position, ReactFlow, type Edge, type Node, type NodeProps } from "@xyflow/react";
import "@xyflow/react/dist/style.css";

import { isSimulatedEvidence, type GraphTask } from "@/lib/workflowGraph";

const NODE_WIDTH = 220;
const NODE_HEIGHT = 64;

function layout(tasks: GraphTask[]): { nodes: Node[]; edges: Edge[] } {
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

  const nodes: Node[] = tasks.map((task) => {
    const pos = g.node(task.id);
    return {
      id: task.id,
      type: "task",
      position: { x: pos.x - NODE_WIDTH / 2, y: pos.y - NODE_HEIGHT / 2 },
      data: { task },
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

function TaskNode({ data, selected }: NodeProps) {
  const task = (data as { task: GraphTask }).task;
  const status = task.evidence?.status ?? null;
  const style = status ? STATUS_STYLES[status] : { border: "border-grid", dot: "bg-muted" };
  const simulated = task.evidence != null && isSimulatedEvidence(task.evidence.details);

  return (
    <div
      className={`rounded-lg border-2 bg-white px-3 py-2 shadow-sm ${style.border} ${
        selected ? "ring-2 ring-action" : ""
      }`}
      style={{ width: NODE_WIDTH }}
    >
      <Handle type="target" position={Position.Top} style={{ background: "var(--grid)" }} />
      <div className="flex items-center gap-2">
        {status && <span className={`h-2 w-2 shrink-0 rounded-full ${style.dot}`} aria-hidden />}
        <span className="truncate text-sm font-medium text-heading">{task.label}</span>
      </div>
      <div className="mt-1 flex items-center gap-2 text-xs text-muted">
        {task.functionVersionNumber != null && <span>v{task.functionVersionNumber}</span>}
        {simulated && <span className="rounded bg-highlight/10 px-1 text-highlight">simulated</span>}
      </div>
      <Handle type="source" position={Position.Bottom} style={{ background: "var(--grid)" }} />
    </div>
  );
}

const nodeTypes = { task: TaskNode };

export interface WorkflowGraphProps {
  tasks: GraphTask[];
  selectedTaskId?: string | null;
  onSelectTask?: (task: GraphTask) => void;
}

/**
 * One shared graph renderer for 3 call sites:
 * - playbook view: `document` tasks, no `evidence` -> no status coloring.
 * - execution view: `document_snapshot` tasks joined to `evidence` by
 *   index -> nodes colored OK/WARN/ERROR, simulated-evidence badge.
 * - build preview: `generated_document`, same as playbook view.
 *
 * Click-only, no drag-to-edit -- the backend model is a plain ordered task
 * list, not a branching graph, so there's nothing to rearrange here.
 */
export function WorkflowGraph({ tasks, selectedTaskId, onSelectTask }: WorkflowGraphProps) {
  const { nodes, edges } = useMemo(() => layout(tasks), [tasks]);
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
