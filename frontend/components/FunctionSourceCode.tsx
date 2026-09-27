"use client";

import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import oneLight from "react-syntax-highlighter/dist/esm/styles/prism/one-light";

import { useFunctionSource } from "@/lib/queries/functions";

interface FunctionSourceCodeProps {
  functionId: string | undefined;
  versionNumber: number | null | undefined;
}

/**
 * Pure source-code display (fetch + has_implementation guard + syntax
 * highlighting), with no workflow-task/graph coupling, so both the
 * playbook tab's node panel and the standalone Check Type Registry tab can
 * share it. No behavior change from what FunctionSourcePanel already did.
 */
export function FunctionSourceCode({ functionId, versionNumber }: FunctionSourceCodeProps) {
  const { data: source, isLoading } = useFunctionSource(functionId, versionNumber);

  return (
    <>
      {isLoading && <p className="mt-1 text-sm text-muted">Loading…</p>}
      {source && !source.has_implementation && (
        <p className="mt-1 text-sm text-highlight">No implementation for this version yet -- contract only.</p>
      )}
      {source?.has_implementation && source.code && (
        <div className="mt-1 min-w-0 overflow-x-auto rounded border border-grid">
          <SyntaxHighlighter
            language={source.language === "python" ? "python" : "text"}
            style={oneLight}
            customStyle={{ margin: 0, fontSize: "0.75rem" }}
          >
            {source.code}
          </SyntaxHighlighter>
        </div>
      )}
    </>
  );
}
