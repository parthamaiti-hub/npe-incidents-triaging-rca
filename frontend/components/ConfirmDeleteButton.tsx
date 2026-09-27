"use client";

import { useState } from "react";

// No modal library and no native `confirm()` are used anywhere
// else in this codebase -- this is a small, reusable two-step inline
// confirm (Delete -> Confirm delete? Yes/No) for the System Mapping Data
// tab's row actions, and any future editable table that needs the same.
export function ConfirmDeleteButton({
  onConfirm,
  isPending,
  label = "Delete",
}: {
  onConfirm: () => void;
  isPending?: boolean;
  label?: string;
}) {
  const [confirming, setConfirming] = useState(false);

  if (confirming) {
    return (
      <span className="inline-flex items-center gap-1 text-xs">
        <span className="text-muted">Confirm delete?</span>
        <button
          type="button"
          onClick={() => {
            setConfirming(false);
            onConfirm();
          }}
          disabled={isPending}
          className="rounded border border-danger px-2 py-1 text-danger disabled:opacity-50"
        >
          {isPending ? "Deleting…" : "Yes"}
        </button>
        <button
          type="button"
          onClick={() => setConfirming(false)}
          disabled={isPending}
          className="rounded border border-grid px-2 py-1 text-muted"
        >
          No
        </button>
      </span>
    );
  }

  return (
    <button
      type="button"
      onClick={() => setConfirming(true)}
      className="rounded border border-grid px-2 py-1 text-xs text-danger"
    >
      {label}
    </button>
  );
}
