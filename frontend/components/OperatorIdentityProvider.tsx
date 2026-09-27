"use client";

import { createContext, useCallback, useContext, useState, useSyncExternalStore } from "react";

const STORAGE_KEY = "npe-operator-identity";

function subscribe(callback: () => void) {
  window.addEventListener("storage", callback);
  return () => window.removeEventListener("storage", callback);
}

function getSnapshot(): string | null {
  try {
    return localStorage.getItem(STORAGE_KEY);
  } catch {
    return null; // private window / storage blocked -- falls back to prompting every load
  }
}

function getServerSnapshot(): string | null {
  return null; // localStorage doesn't exist during SSR; hydrates to the real value client-side
}

interface OperatorIdentityContextValue {
  name: string | null;
  setName: (name: string) => void;
}

const OperatorIdentityContext = createContext<OperatorIdentityContextValue | null>(null);

/**
 * Dev-mode "who am I" -- there's no auth backend yet, but every write the app makes
 * (build/approve/retry/feedback) needs *some* identity string. Prompts
 * once, persists to localStorage (per-browser, not shared/synced), and
 * every write call reads it from here instead of its own inline field.
 *
 * Reads localStorage via useSyncExternalStore rather than
 * useState+useEffect: localStorage is a synchronous external store React
 * doesn't know about, and this is the API built for exactly that --
 * correctly handles the SSR/hydration gap without a setState-in-effect
 * cascading-render pattern.
 */
export function OperatorIdentityProvider({ children }: { children: React.ReactNode }) {
  const name = useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);

  const setName = useCallback((newName: string) => {
    const trimmed = newName.trim();
    if (!trimmed) return;
    try {
      localStorage.setItem(STORAGE_KEY, trimmed);
    } catch {
      // ignore -- storage blocked; the prompt just reappears next load
    }
    // A same-tab localStorage write never fires "storage" (only other tabs
    // get that event) -- dispatch one ourselves so this tab's
    // useSyncExternalStore subscriber re-reads immediately.
    window.dispatchEvent(new StorageEvent("storage", { key: STORAGE_KEY, newValue: trimmed }));
  }, []);

  return (
    <OperatorIdentityContext.Provider value={{ name, setName }}>
      {children}
      {name === null && <IdentityPrompt onSubmit={setName} />}
    </OperatorIdentityContext.Provider>
  );
}

export function useOperatorIdentity() {
  const ctx = useContext(OperatorIdentityContext);
  if (!ctx) throw new Error("useOperatorIdentity must be used within OperatorIdentityProvider");
  return ctx;
}

function IdentityPrompt({ onSubmit }: { onSubmit: (name: string) => void }) {
  const [value, setValue] = useState("");

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-heading/40">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          onSubmit(value);
        }}
        className="w-80 rounded-md border border-grid bg-white p-4 shadow-lg"
      >
        <h2 className="text-sm font-semibold text-heading">Who&apos;s operating this session?</h2>
        <p className="mt-1 text-xs text-muted">
          No login yet -- this name is attached to anything you build, approve, retry, or give feedback on.
        </p>
        <input
          autoFocus
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="e.g. jane.doe"
          className="mt-3 block w-full rounded border border-grid px-2 py-1 text-sm text-heading"
        />
        <button
          type="submit"
          disabled={!value.trim()}
          className="mt-3 rounded bg-action px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
        >
          Continue
        </button>
      </form>
    </div>
  );
}
