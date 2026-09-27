import { humanizeRcaStatus, rcaStatusBadgeClass, type RcaResult } from "@/lib/rca";

/**
 * root_cause_summary / contributing_factors / recommended_actions, labeled
 * "RCA Outcome" (never "resolved") -- this tool diagnoses, it never claims
 * a fix (app/rca_status.py).
 *
 * llmGenerated: true when this execution's rca_meta is
 * present, i.e. LLM/RAG-primary synthesis produced this RCA rather than
 * the deterministic pattern-matcher -- shown with the same "simulated"
 * badge styling WorkflowGraph already uses for stubbed evidence, same
 * "be honest about what's real" convention.
 */
export function RcaSections({ rca, llmGenerated = false }: { rca: RcaResult | null; llmGenerated?: boolean }) {
  if (!rca) {
    return <p className="text-sm text-muted">No RCA available for this incident yet.</p>;
  }

  return (
    <div className="space-y-3 rounded-md border border-grid bg-white p-4">
      <div>
        <span className={`inline-block rounded px-1.5 py-0.5 text-xs font-medium ${rcaStatusBadgeClass(rca.rca_status)}`}>
          {humanizeRcaStatus(rca.rca_status)}
        </span>
        {llmGenerated && (
          <span className="ml-2 inline-block rounded bg-highlight/10 px-1.5 py-0.5 text-xs font-medium text-highlight">
            LLM-generated
          </span>
        )}
        <p className="mt-1 text-sm text-heading">{rca.root_cause_summary}</p>
      </div>

      {rca.contributing_factors.length > 0 && (
        <div>
          <h4 className="text-xs font-semibold uppercase tracking-wide text-muted">Contributing Factors</h4>
          <ul className="mt-1 list-disc pl-5 text-sm text-heading">
            {rca.contributing_factors.map((factor, i) => (
              <li key={i}>{factor}</li>
            ))}
          </ul>
        </div>
      )}

      {rca.recommended_actions.length > 0 && (
        <div>
          <h4 className="text-xs font-semibold uppercase tracking-wide text-muted">Recommended Actions</h4>
          <ul className="mt-1 list-disc pl-5 text-sm text-heading">
            {rca.recommended_actions.map((action, i) => (
              <li key={i}>{action}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
