"use client";

import Link from "next/link";
import { useMemo, useState } from "react";

import { FunctionSourcePanel } from "@/components/FunctionSourcePanel";
import { PlaybookEditor } from "@/components/PlaybookEditor";
import { WorkflowGraph } from "@/components/WorkflowGraph";
import { useMappingRulesForSourceSystem, useSourceSystems } from "@/lib/queries/catalog";
import { useVersionYaml, useWorkflowDefinitions, useWorkflowVersions } from "@/lib/queries/workflows";
import { toGraphTasks, type GraphTask } from "@/lib/workflowGraph";

// Tab 2 -- Playbooks for RCA: definition list on the
// left, detail (category/matched rule header + graph + function source
// panel) on the right.
export default function PlaybooksPage() {
  const { data: definitions, isLoading, isError } = useWorkflowDefinitions();
  const { data: sourceSystems } = useSourceSystems();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  // While a playbook is being edited, switching to another one is blocked so
  // an unsaved draft can't be silently lost.
  const [editing, setEditing] = useState(false);

  const sourceSystemName = useMemo(() => {
    const map = new Map(sourceSystems?.map((s) => [s.id, s.name]) ?? []);
    return (id: string) => map.get(id) ?? id;
  }, [sourceSystems]);

  const selected = definitions?.find((d) => d.id === selectedId) ?? null;

  return (
    <main className="p-6">
      <h1 className="text-xl font-semibold text-heading">Playbooks for RCA</h1>
      <div className="mt-4 flex items-center justify-end">
        <Link href="/playbooks/build" className="rounded bg-action px-3 py-1.5 text-sm font-medium text-white">
          Create new workflow
        </Link>
      </div>

      <div className="mt-2 grid grid-cols-1 gap-4 lg:grid-cols-[1fr_3fr]">
        <div className="rounded-md border border-grid bg-white">
          {isLoading && <p className="p-4 text-sm text-muted">Loading…</p>}
          {isError && <p className="p-4 text-sm text-danger">Failed to load workflows.</p>}
          <ul className="divide-y divide-grid">
            {definitions?.map((def) => (
              <li key={def.id}>
                <button
                  type="button"
                  onClick={() => setSelectedId(def.id)}
                  disabled={editing && def.id !== selectedId}
                  title={editing && def.id !== selectedId ? "Finish or discard the current edit first" : undefined}
                  className={`block w-full px-3 py-2 text-left text-sm hover:bg-canvas disabled:cursor-not-allowed disabled:opacity-50 ${
                    def.id === selectedId ? "bg-canvas font-medium text-heading" : "text-muted"
                  }`}
                >
                  <div className="text-heading">{sourceSystemName(def.source_system_id)}</div>
                  <div className="text-xs text-muted">{def.category}</div>
                </button>
              </li>
            ))}
          </ul>
          {definitions?.length === 0 && <p className="p-4 text-sm text-muted">No workflows defined yet.</p>}
        </div>

        <div>
          {selected ? (
            <WorkflowDetail key={selected.id} definition={selected} editing={editing} onEditingChange={setEditing} />
          ) : (
            <p className="text-sm text-muted">Select a workflow on the left.</p>
          )}
        </div>
      </div>
    </main>
  );
}

function WorkflowDetail({
  definition,
  editing,
  onEditingChange,
}: {
  definition: { id: string; source_system_id: string; category: string };
  editing: boolean;
  onEditingChange: (editing: boolean) => void;
}) {
  const { data: versions, isLoading } = useWorkflowVersions(definition.id);
  const { data: mappingRules } = useMappingRulesForSourceSystem(definition.source_system_id);
  const [selectedVersionId, setSelectedVersionId] = useState<string | null>(null);
  const [selectedTask, setSelectedTask] = useState<GraphTask | null>(null);

  const approvedVersion = versions?.find((v) => v.status === "approved") ?? null;
  const activeVersion = versions?.find((v) => v.id === selectedVersionId) ?? approvedVersion ?? versions?.[versions.length - 1] ?? null;
  const matchedRule = mappingRules?.find((r) => r.category === definition.category) ?? null;

  const tasks = activeVersion ? toGraphTasks(activeVersion.document) : [];

  return (
    <div>
      <div className="rounded-md border border-grid bg-white p-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <h2 className="text-sm font-semibold text-heading">{definition.category}</h2>
            <p className="text-xs text-muted">
              Matched rule:{" "}
              {matchedRule ? (
                <>
                  <code>{matchedRule.id}</code> ({matchedRule.signal_type}: <code>{matchedRule.signal_pattern}</code>)
                </>
              ) : (
                "none found"
              )}
            </p>
          </div>
          <div className="flex items-center gap-3">
            {versions && versions.length > 0 && (
              <label className="text-xs text-muted">
                Version
                <select
                  value={activeVersion?.id ?? ""}
                  onChange={(e) => setSelectedVersionId(e.target.value)}
                  disabled={editing}
                  className="ml-2 rounded border border-grid px-2 py-1 text-sm text-heading"
                >
                  {versions.map((v) => (
                    <option key={v.id} value={v.id}>
                      v{v.version_number} ({v.status})
                    </option>
                  ))}
                </select>
              </label>
            )}
            {!editing && activeVersion?.status === "approved" && (
              <button
                type="button"
                onClick={() => {
                  setSelectedTask(null);
                  onEditingChange(true);
                }}
                className="rounded bg-focus px-3 py-1 text-sm font-medium text-white"
              >
                Edit
              </button>
            )}
          </div>
        </div>
        {!editing && activeVersion && activeVersion.status !== "approved" && (
          <p className="mt-2 text-xs text-muted">Only the approved version can be edited; older versions are kept read-only.</p>
        )}
      </div>

      {isLoading && <p className="mt-4 text-sm text-muted">Loading…</p>}
      {!isLoading && versions?.length === 0 && <p className="mt-4 text-sm text-muted">This workflow has no versions yet.</p>}

      {editing && activeVersion && (
        <EditorLoader
          definition={definition}
          baseVersion={activeVersion}
          onExit={(publishedVersionId) => {
            if (publishedVersionId) setSelectedVersionId(publishedVersionId);
            onEditingChange(false);
          }}
        />
      )}

      {!editing && activeVersion && (
        <div className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-[2fr_1fr]">
          <WorkflowGraph tasks={tasks} selectedTaskId={selectedTask?.id ?? null} onSelectTask={setSelectedTask} />
          <FunctionSourcePanel task={selectedTask} onClose={() => setSelectedTask(null)} />
        </div>
      )}
    </div>
  );
}

/** Fetches the base version's YAML, then mounts the editor with it -- so the
 * editor's draft is initialized once, from complete data. */
function EditorLoader({
  definition,
  baseVersion,
  onExit,
}: {
  definition: { id: string; source_system_id: string; category: string };
  baseVersion: { id: string; version_number: number; document: unknown[] };
  onExit: (publishedVersionId?: string) => void;
}) {
  const { data: yaml, error, isLoading } = useVersionYaml(baseVersion.id);
  if (isLoading) return <p className="mt-4 text-sm text-muted">Loading YAML…</p>;
  if (error || yaml === undefined) {
    return (
      <p className="mt-4 text-sm text-danger">
        Failed to load this version&apos;s YAML{error ? `: ${error.message}` : ""}.{" "}
        <button type="button" onClick={() => onExit()} className="underline">
          Back
        </button>
      </p>
    );
  }
  return <PlaybookEditor definition={definition} baseVersion={baseVersion} initialYaml={yaml} onExit={onExit} />;
}
