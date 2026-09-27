// Narrows the backend's untyped `rca: dict | null` JSONB column into the
// real app.rca_synthesizer.synthesize_rca() output shape, and mirrors
// app/adf_report.py's Jira-comment panel coloring rule so a
// given rca_status reads the same severity here as it does in the Jira
// comment: error=Identified, warning=Probable/NeedManualIntervention/
// IssueCouldNotBeTraced, note=Inconclusive/Correlated.

export interface RcaResult {
  matched_pattern: string;
  rca_status: string;
  root_cause_summary: string;
  contributing_factors: string[];
  recommended_actions: string[];
}

export function asRcaResult(rca: unknown): RcaResult | null {
  if (!rca || typeof rca !== "object") return null;
  const r = rca as Record<string, unknown>;
  return {
    matched_pattern: String(r.matched_pattern ?? ""),
    rca_status: String(r.rca_status ?? ""),
    root_cause_summary: String(r.root_cause_summary ?? ""),
    contributing_factors: Array.isArray(r.contributing_factors) ? r.contributing_factors.map(String) : [],
    recommended_actions: Array.isArray(r.recommended_actions) ? r.recommended_actions.map(String) : [],
  };
}

export const RCA_STATUS_OPTIONS = [
  "Identified",
  "Probable",
  "Inconclusive",
  "Correlated",
  "IssueCouldNotBeTraced",
  "NeedManualIntervention",
];

const BADGE_CLASSES: Record<string, string> = {
  Identified: "bg-danger/10 text-danger",
  Probable: "bg-highlight/10 text-highlight",
  NeedManualIntervention: "bg-highlight/10 text-highlight",
  IssueCouldNotBeTraced: "bg-highlight/10 text-highlight",
  Inconclusive: "bg-muted/10 text-muted",
  Correlated: "bg-muted/10 text-muted",
};

export function rcaStatusBadgeClass(status: string | null | undefined): string {
  if (!status) return "bg-muted/10 text-muted";
  return BADGE_CLASSES[status] ?? "bg-muted/10 text-muted";
}

export function humanizeRcaStatus(status: string | null | undefined): string {
  if (!status) return "—";
  return status.replace(/([a-z])([A-Z])/g, "$1 $2");
}
