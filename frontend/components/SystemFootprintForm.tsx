"use client";

import { useState } from "react";

import { useCreateFootprint, useUpdateFootprint, type FootprintInput } from "@/lib/queries/catalog";

// Add/edit form for one SYSTEM_FOOTPRINT row, scoped to a fixed
// sourceSystemId (the tab's currently-selected source system) -- not a
// field the operator edits here, since a footprint's parent system is set
// by which detail panel it's being added from, not typed freely.
export function SystemFootprintForm({
  sourceSystemId,
  initial,
  onSaved,
  onCancel,
}: {
  sourceSystemId: string;
  initial?: FootprintInput;
  onSaved: () => void;
  onCancel: () => void;
}) {
  const [values, setValues] = useState<FootprintInput>(
    initial ?? { id: "", source_system_id: sourceSystemId, footprint_type: "", value: "", notes: null },
  );
  const create = useCreateFootprint();
  const update = useUpdateFootprint();
  const isEditing = !!initial;
  const mutation = isEditing ? update : create;

  function set<K extends keyof FootprintInput>(key: K, value: FootprintInput[K]) {
    setValues((prev) => ({ ...prev, [key]: value }));
  }

  const canSave = !!values.id && !!values.footprint_type && !!values.value;

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    mutation.mutate(values, { onSuccess: onSaved });
  }

  return (
    <form onSubmit={handleSubmit} className="rounded border border-grid p-3">
      <h3 className="text-xs font-semibold uppercase tracking-wide text-muted">
        {isEditing ? `Edit ${initial!.id}` : "Add footprint"}
      </h3>

      <div className="mt-2 flex flex-wrap gap-3">
        <label className="text-xs text-muted">
          Id
          <input
            value={values.id}
            onChange={(e) => set("id", e.target.value)}
            disabled={isEditing}
            className="mt-1 block w-32 rounded border border-grid px-2 py-1 text-sm text-heading disabled:bg-canvas disabled:text-muted"
          />
        </label>
        <label className="text-xs text-muted">
          Footprint type
          <input
            value={values.footprint_type}
            onChange={(e) => set("footprint_type", e.target.value)}
            placeholder="e.g. hostname"
            className="mt-1 block w-32 rounded border border-grid px-2 py-1 text-sm text-heading"
          />
        </label>
        <label className="text-xs text-muted">
          Value
          <input
            value={values.value}
            onChange={(e) => set("value", e.target.value)}
            placeholder="regex"
            className="mt-1 block w-48 rounded border border-grid px-2 py-1 text-sm text-heading"
          />
        </label>
        {/* Outside the <label> deliberately -- nested inside it, this
            sentence becomes part of the "Value" field's accessible name
            (screen readers read the whole hint every time), and it also
            makes any text-based lookup of a short label like "Id" collide
            with the word "valid" here. */}
        <span className="mt-4 block w-full text-[11px] text-muted">Value must be a valid regular expression.</span>
        <label className="text-xs text-muted">
          Notes
          <input
            value={values.notes ?? ""}
            onChange={(e) => set("notes", e.target.value || null)}
            className="mt-1 block w-48 rounded border border-grid px-2 py-1 text-sm text-heading"
          />
        </label>
      </div>

      {mutation.isError && <p className="mt-2 text-sm text-danger">{(mutation.error as Error).message}</p>}

      <div className="mt-2 flex gap-2">
        <button
          type="submit"
          disabled={!canSave || mutation.isPending}
          className="rounded bg-focus px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
        >
          {mutation.isPending ? "Saving…" : "Save"}
        </button>
        <button type="button" onClick={onCancel} className="rounded border border-grid px-3 py-1.5 text-sm text-muted">
          Cancel
        </button>
      </div>
    </form>
  );
}
