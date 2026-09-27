"use client";

import { useState } from "react";

import { useOperatorIdentity } from "@/components/OperatorIdentityProvider";

/** Nav-bar affordance to see/change the current operator identity -- the
 * single place identity is edited; forms elsewhere just display it. */
export function OperatorIdentityChip() {
  const { name, setName } = useOperatorIdentity();
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState("");

  if (editing) {
    return (
      <form
        onSubmit={(e) => {
          e.preventDefault();
          setName(value);
          setEditing(false);
        }}
        className="flex items-center gap-1"
      >
        <input
          autoFocus
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="e.g. jane.doe"
          className="w-32 rounded border border-grid px-1.5 py-0.5 text-xs text-heading"
        />
        <button type="submit" className="text-xs text-focus hover:underline">
          save
        </button>
      </form>
    );
  }

  return (
    <button
      type="button"
      onClick={() => {
        setValue(name ?? "");
        setEditing(true);
      }}
      title="Change operator identity"
      className="text-xs text-muted hover:text-heading"
    >
      {name ?? "unidentified"}
    </button>
  );
}
