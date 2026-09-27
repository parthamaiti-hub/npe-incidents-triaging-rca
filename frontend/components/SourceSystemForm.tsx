"use client";

import { useState } from "react";

import { useCreateSourceSystem, useTeams, useUpdateSourceSystem, type SourceSystemInput } from "@/lib/queries/catalog";

const EMPTY: SourceSystemInput = {
  id: "",
  name: "",
  code: "",
  type: "",
  description: "",
  owning_team: "",
  environment: "",
  owning_team_id: null,
};

// Add/edit form for one SOURCE_SYSTEM row. `initial` being
// present means edit mode -- id renders read-only there, since PUT
// requires body.id === path id (not a rename; changing identity means
// delete + recreate, and this form doesn't pretend otherwise).
export function SourceSystemForm({
  initial,
  onSaved,
  onCancel,
}: {
  initial?: SourceSystemInput;
  onSaved: () => void;
  onCancel: () => void;
}) {
  const [values, setValues] = useState<SourceSystemInput>(initial ?? EMPTY);
  const { data: teams } = useTeams();
  const create = useCreateSourceSystem();
  const update = useUpdateSourceSystem();
  const isEditing = !!initial;
  const mutation = isEditing ? update : create;

  function set<K extends keyof SourceSystemInput>(key: K, value: SourceSystemInput[K]) {
    setValues((prev) => ({ ...prev, [key]: value }));
  }

  const canSave = !!values.id && !!values.name && !!values.code && !!values.type && !!values.description && !!values.owning_team && !!values.environment;

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    mutation.mutate(values, { onSuccess: onSaved });
  }

  return (
    <form onSubmit={handleSubmit} className="rounded-md border border-grid bg-white p-4">
      <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">
        {isEditing ? `Edit ${initial!.id}` : "Add source system"}
      </h2>

      <div className="mt-3 flex flex-wrap gap-3">
        <label className="text-xs text-muted">
          Id
          <input
            value={values.id}
            onChange={(e) => set("id", e.target.value)}
            disabled={isEditing}
            placeholder="e.g. SYS_DCD"
            className="mt-1 block w-40 rounded border border-grid px-2 py-1 text-sm text-heading disabled:bg-canvas disabled:text-muted"
          />
        </label>
        <label className="text-xs text-muted">
          Name
          <input
            value={values.name}
            onChange={(e) => set("name", e.target.value)}
            className="mt-1 block w-48 rounded border border-grid px-2 py-1 text-sm text-heading"
          />
        </label>
        <label className="text-xs text-muted">
          Code
          <input
            value={values.code}
            onChange={(e) => set("code", e.target.value)}
            className="mt-1 block w-28 rounded border border-grid px-2 py-1 text-sm text-heading"
          />
        </label>
        <label className="text-xs text-muted">
          Type
          <input
            value={values.type}
            onChange={(e) => set("type", e.target.value)}
            placeholder="e.g. Application"
            className="mt-1 block w-40 rounded border border-grid px-2 py-1 text-sm text-heading"
          />
        </label>
        <label className="text-xs text-muted">
          Environment
          <input
            value={values.environment}
            onChange={(e) => set("environment", e.target.value)}
            placeholder="e.g. NPE"
            className="mt-1 block w-28 rounded border border-grid px-2 py-1 text-sm text-heading"
          />
        </label>
        <label className="text-xs text-muted">
          Owning team (display)
          <input
            value={values.owning_team}
            onChange={(e) => set("owning_team", e.target.value)}
            className="mt-1 block w-40 rounded border border-grid px-2 py-1 text-sm text-heading"
          />
        </label>
        <label className="text-xs text-muted">
          Owning team (linked, optional)
          <select
            value={values.owning_team_id ?? ""}
            onChange={(e) => set("owning_team_id", e.target.value || null)}
            className="mt-1 block w-48 rounded border border-grid px-2 py-1 text-sm text-heading"
          >
            <option value="">None</option>
            {teams?.map((t) => (
              <option key={t.id} value={t.id}>
                {t.name}
              </option>
            ))}
          </select>
        </label>
        <label className="w-full text-xs text-muted">
          Description
          <input
            value={values.description}
            onChange={(e) => set("description", e.target.value)}
            className="mt-1 block w-full rounded border border-grid px-2 py-1 text-sm text-heading"
          />
        </label>
      </div>

      {mutation.isError && <p className="mt-2 text-sm text-danger">{(mutation.error as Error).message}</p>}

      <div className="mt-3 flex gap-2">
        <button
          type="submit"
          disabled={!canSave || mutation.isPending}
          className="rounded bg-action px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
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
