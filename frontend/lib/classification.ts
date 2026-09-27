// Mirrors app/classification_status.py's display mapping -- the internal
// "resolved"/"any_category_pending_llm"/"manual_triage"/"llm_resolved"
// values (stored on Incident.classification_status) never render as-is in
// the UI. llm_resolved displays the same as
// any_category_pending_llm, deliberately -- both mean "lower confidence
// than a rule match," never "Success".
const LABELS: Record<string, string> = {
  resolved: "Success",
  any_category_pending_llm: "Probable",
  manual_triage: "Not determined",
  llm_resolved: "Probable",
};

const BADGE_CLASSES: Record<string, string> = {
  resolved: "bg-focus/10 text-focus",
  any_category_pending_llm: "bg-highlight/10 text-highlight",
  manual_triage: "bg-muted/10 text-muted",
  llm_resolved: "bg-highlight/10 text-highlight",
};

export function classificationStatusLabel(status: string): string {
  return LABELS[status] ?? status;
}

export function classificationBadgeClass(status: string): string {
  return BADGE_CLASSES[status] ?? "bg-muted/10 text-muted";
}

export const CLASSIFICATION_STATUS_OPTIONS = Object.keys(LABELS);

/** A small inline annotation next to the
 * classification badge when the classification came from the LLM
 * fallback -- e.g. "Probable (LLM, 0.82)" -- so an operator can tell an
 * LLM-driven route from a rule-driven one at a glance, never presented as
 * equal-confidence. Same "small inline annotation next to an existing
 * badge" pattern the Retry tab already uses for mapping_overridden. */
export function classificationMethodAnnotation(
  classificationMethod: string | null | undefined,
  llmConfidence: number | null | undefined,
): string | null {
  if (classificationMethod !== "llm") return null;
  return llmConfidence != null ? `LLM, ${llmConfidence.toFixed(2)}` : "LLM";
}
