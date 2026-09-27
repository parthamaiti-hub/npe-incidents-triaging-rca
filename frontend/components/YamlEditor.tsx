"use client";

import { useEffect, useMemo, useState } from "react";
import CodeMirror from "@uiw/react-codemirror";
import { yaml } from "@codemirror/lang-yaml";
import { lintGutter, setDiagnostics, type Diagnostic } from "@codemirror/lint";
import type { EditorState } from "@codemirror/state";
import type { EditorView } from "@codemirror/view";

import type { ApiIssue } from "@/lib/workflowDraft";

interface YamlEditorProps {
  value: string;
  onChange: (value: string) => void;
  /** Validation problems to mark in the gutter (those with a line number). */
  issues: ApiIssue[];
  readOnly?: boolean;
}

function toDiagnostics(state: EditorState, issues: ApiIssue[]): Diagnostic[] {
  const doc = state.doc;
  return issues
    .filter((issue) => issue.line != null)
    .map((issue) => {
      const line = doc.line(Math.min(Math.max(issue.line!, 1), doc.lines));
      const from = Math.min(line.from + Math.max((issue.column ?? 1) - 1, 0), line.to);
      return {
        from,
        to: line.to > from ? line.to : from,
        severity: "error",
        message: issue.path ? `${issue.path}: ${issue.message}` : issue.message,
      };
    });
}

/**
 * CNCF Serverless Workflow YAML pane. Validity is decided by the server; this
 * component only displays the issues it's handed, as gutter/underline marks.
 * Loaded client-only (next/dynamic, ssr: false) by its caller.
 */
export function YamlEditor({ value, onChange, issues, readOnly = false }: YamlEditorProps) {
  // The editor view is created asynchronously by @uiw/react-codemirror, so
  // it's captured via onCreateEditor rather than read from a ref on mount.
  const [view, setView] = useState<EditorView | null>(null);
  const extensions = useMemo(() => [yaml(), lintGutter()], []);

  // Push externally-computed diagnostics into the editor whenever they (or
  // the text they refer to) change.
  useEffect(() => {
    if (!view) return;
    view.dispatch(setDiagnostics(view.state, toDiagnostics(view.state, issues)));
  }, [view, issues, value]);

  return (
    <div className="min-w-0 overflow-hidden rounded-md border border-grid bg-white text-sm" aria-label="Playbook YAML" role="region">
      <CodeMirror
        onCreateEditor={setView}
        value={value}
        onChange={onChange}
        extensions={extensions}
        editable={!readOnly}
        readOnly={readOnly}
        height="480px"
        basicSetup={{ foldGutter: false, autocompletion: false }}
      />
    </div>
  );
}
