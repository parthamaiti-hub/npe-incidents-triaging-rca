"use client";

import { useMemo, useState } from "react";

import { ConfirmDeleteButton } from "@/components/ConfirmDeleteButton";
import { SourceSystemForm } from "@/components/SourceSystemForm";
import { SystemFootprintForm } from "@/components/SystemFootprintForm";
import {
  useDeleteFootprint,
  useDeleteSourceSystem,
  useFootprints,
  useSourceSystems,
  type FootprintInput,
  type SourceSystemInput,
} from "@/lib/queries/catalog";

// System Mapping Data tab -- view/add/edit/delete SOURCE_SYSTEM
// and SYSTEM_FOOTPRINT rows, the catalog data classification is matched
// against. List-on-left/detail-on-right, same split playbooks/page.tsx
// already establishes for workflow definitions.
export default function SystemMappingPage() {
  const { data: sourceSystems, isLoading, isError } = useSourceSystems();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<SourceSystemInput | null>(null);
  const [search, setSearch] = useState("");
  const [environmentFilter, setEnvironmentFilter] = useState("");
  const deleteSourceSystem = useDeleteSourceSystem();

  // At the confirmed target scale (up to ~300 systems
  // across ~6 non-prod environments), an unfiltered list is unusable --
  // client-side filter over the already-fetched list, same approach
  // useCategoryOptions() already takes for deriving filter options from
  // live data rather than a canonical-but-possibly-stale catalog list.
  const environmentOptions = useMemo(() => {
    const values = new Set((sourceSystems ?? []).map((s) => s.environment).filter(Boolean));
    return Array.from(values).sort();
  }, [sourceSystems]);

  const filteredSourceSystems = useMemo(() => {
    const q = search.trim().toLowerCase();
    return (sourceSystems ?? []).filter((s) => {
      const matchesSearch = !q || s.id.toLowerCase().includes(q) || s.name.toLowerCase().includes(q) || s.code.toLowerCase().includes(q);
      const matchesEnvironment = !environmentFilter || s.environment === environmentFilter;
      return matchesSearch && matchesEnvironment;
    });
  }, [sourceSystems, search, environmentFilter]);

  const selected = sourceSystems?.find((s) => s.id === selectedId) ?? null;

  return (
    <main className="p-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold text-heading">System Mapping Data</h1>
        {!creating && !editing && (
          <button
            type="button"
            onClick={() => setCreating(true)}
            className="rounded bg-action px-3 py-1.5 text-sm font-medium text-white"
          >
            Add source system
          </button>
        )}
      </div>
      <p className="mt-1 text-sm text-muted">
        The source systems and footprints that classification is matched against (`SOURCE_SYSTEM` / `SYSTEM_FOOTPRINT`).
      </p>

      {(creating || editing) && (
        <div className="mt-4">
          <SourceSystemForm
            initial={editing ?? undefined}
            onSaved={() => {
              setCreating(false);
              setEditing(null);
            }}
            onCancel={() => {
              setCreating(false);
              setEditing(null);
            }}
          />
        </div>
      )}

      <div className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-[1fr_2fr]">
        <div className="rounded-md border border-grid bg-white">
          <div className="flex flex-wrap gap-2 border-b border-grid p-2">
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search id, name, or code…"
              aria-label="Search source systems"
              className="min-w-0 flex-1 rounded border border-grid px-2 py-1 text-sm text-heading"
            />
            <select
              value={environmentFilter}
              onChange={(e) => setEnvironmentFilter(e.target.value)}
              aria-label="Filter by environment"
              className="rounded border border-grid px-2 py-1 text-sm text-heading"
            >
              <option value="">All environments</option>
              {environmentOptions.map((env) => (
                <option key={env} value={env}>
                  {env}
                </option>
              ))}
            </select>
          </div>

          <div className="overflow-x-auto">
            {isLoading && <p className="p-4 text-sm text-muted">Loading…</p>}
            {isError && <p className="p-4 text-sm text-danger">Failed to load source systems.</p>}
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-grid text-left text-xs uppercase tracking-wide text-muted">
                  <th className="px-3 py-2">Name</th>
                  <th className="px-3 py-2">Environment</th>
                </tr>
              </thead>
              <tbody>
                {filteredSourceSystems.map((s) => (
                  <tr
                    key={s.id}
                    onClick={() => setSelectedId(s.id)}
                    className={`cursor-pointer border-b border-grid last:border-0 hover:bg-canvas ${
                      s.id === selectedId ? "bg-canvas" : ""
                    }`}
                  >
                    <td className="px-3 py-2">
                      <div className="font-medium text-heading">{s.name}</div>
                      <div className="text-xs text-muted">{s.id}</div>
                    </td>
                    <td className="px-3 py-2 text-muted">{s.environment}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {sourceSystems?.length === 0 && <p className="p-4 text-sm text-muted">No source systems configured yet.</p>}
            {!!sourceSystems?.length && filteredSourceSystems.length === 0 && (
              <p className="p-4 text-sm text-muted">No source systems match this search/filter.</p>
            )}
          </div>
          {!isLoading && !!sourceSystems?.length && (
            <p className="border-t border-grid px-3 py-1.5 text-xs text-muted">
              Showing {filteredSourceSystems.length} of {sourceSystems.length}
            </p>
          )}
        </div>

        <div>
          {!selected && <p className="text-sm text-muted">Select a source system on the left, or add a new one.</p>}
          {selected && (
            <SourceSystemDetail
              sourceSystem={selected}
              onEdit={() => setEditing(selected)}
              onDeleted={() => setSelectedId(null)}
              deleteSourceSystem={deleteSourceSystem}
            />
          )}
        </div>
      </div>
    </main>
  );
}

function SourceSystemDetail({
  sourceSystem,
  onEdit,
  onDeleted,
  deleteSourceSystem,
}: {
  sourceSystem: SourceSystemInput;
  onEdit: () => void;
  onDeleted: () => void;
  deleteSourceSystem: ReturnType<typeof useDeleteSourceSystem>;
}) {
  const { data: footprints, isLoading } = useFootprints(sourceSystem.id);
  const [addingFootprint, setAddingFootprint] = useState(false);
  const [editingFootprint, setEditingFootprint] = useState<FootprintInput | null>(null);
  const deleteFootprint = useDeleteFootprint();

  return (
    <div>
      <div className="rounded-md border border-grid bg-white p-4">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div>
            <h2 className="text-sm font-semibold text-heading">
              {sourceSystem.name} <span className="text-xs font-normal text-muted">({sourceSystem.id})</span>
            </h2>
            <p className="text-xs text-muted">{sourceSystem.description}</p>
            <p className="mt-1 text-xs text-muted">
              {sourceSystem.type} · {sourceSystem.environment} · owned by {sourceSystem.owning_team}
            </p>
          </div>
          <div className="flex gap-2">
            <button type="button" onClick={onEdit} className="rounded border border-grid px-2 py-1 text-xs text-heading">
              Edit source system
            </button>
            <ConfirmDeleteButton
              label="Delete source system"
              isPending={deleteSourceSystem.isPending}
              onConfirm={() => deleteSourceSystem.mutate(sourceSystem.id, { onSuccess: onDeleted })}
            />
          </div>
        </div>
        {deleteSourceSystem.isError && (
          <p className="mt-2 text-sm text-danger">{(deleteSourceSystem.error as Error).message}</p>
        )}
      </div>

      <div className="mt-4 rounded-md border border-grid bg-white p-4">
        <div className="flex items-center justify-between">
          <h3 className="text-xs font-semibold uppercase tracking-wide text-muted">Footprints</h3>
          {!addingFootprint && !editingFootprint && (
            <button
              type="button"
              onClick={() => setAddingFootprint(true)}
              className="rounded bg-focus px-3 py-1.5 text-sm font-medium text-white"
            >
              Add footprint
            </button>
          )}
        </div>

        {(addingFootprint || editingFootprint) && (
          <div className="mt-3">
            <SystemFootprintForm
              sourceSystemId={sourceSystem.id}
              initial={editingFootprint ?? undefined}
              onSaved={() => {
                setAddingFootprint(false);
                setEditingFootprint(null);
              }}
              onCancel={() => {
                setAddingFootprint(false);
                setEditingFootprint(null);
              }}
            />
          </div>
        )}

        {isLoading && <p className="mt-3 text-sm text-muted">Loading…</p>}
        {!isLoading && footprints?.length === 0 && <p className="mt-3 text-sm text-muted">No footprints configured for this system yet.</p>}

        {footprints && footprints.length > 0 && (
          <div className="mt-3 overflow-x-auto rounded border border-grid">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-grid text-left text-xs uppercase tracking-wide text-muted">
                  <th className="px-3 py-2">Type</th>
                  <th className="px-3 py-2">Value</th>
                  <th className="px-3 py-2">Notes</th>
                  <th className="px-3 py-2" />
                </tr>
              </thead>
              <tbody>
                {footprints.map((fp) => (
                  <tr key={fp.id} className="border-b border-grid last:border-0">
                    <td className="px-3 py-2 text-heading">{fp.footprint_type}</td>
                    <td className="px-3 py-2">
                      <code className="text-xs">{fp.value}</code>
                    </td>
                    <td className="px-3 py-2 text-muted">{fp.notes ?? "—"}</td>
                    <td className="px-3 py-2">
                      <div className="flex justify-end gap-2">
                        <button
                          type="button"
                          onClick={() => setEditingFootprint(fp)}
                          className="rounded border border-grid px-2 py-1 text-xs text-heading"
                        >
                          Edit
                        </button>
                        <ConfirmDeleteButton
                          isPending={deleteFootprint.isPending}
                          onConfirm={() => deleteFootprint.mutate({ id: fp.id, sourceSystemId: sourceSystem.id })}
                        />
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {deleteFootprint.isError && <p className="mt-2 text-sm text-danger">{(deleteFootprint.error as Error).message}</p>}
      </div>
    </div>
  );
}
