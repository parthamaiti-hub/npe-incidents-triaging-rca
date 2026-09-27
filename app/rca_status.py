"""Canonical RCA outcome status vocabulary (user-proposed).

Deliberately distinct from app.classification's "resolved" / "manual_triage"
/ "any_category_pending_llm" status, which is about whether a
source_system/category was *determined* -- that's a classification-stage
concept. This is about the RCA's own diagnostic confidence. Reusing
"resolved" for RCA output was misleading: this tool's scope is diagnosis,
not remediation, so nothing here ever claims the incident is fixed.
"""

IDENTIFIED = "Identified"
PROBABLE = "Probable"
INCONCLUSIVE = "Inconclusive"
CORRELATED = "Correlated"
ISSUE_COULD_NOT_BE_TRACED = "IssueCouldNotBeTraced"
NEED_MANUAL_INTERVENTION = "NeedManualIntervention"

DESCRIPTIONS = {
    IDENTIFIED: "Matched to a definitive root cause.",
    PROBABLE: "Narrowed to a high-probability cause from correlated evidence -- not certain.",
    INCONCLUSIVE: "Evidence gathered but could not be correlated to a specific root cause.",
    CORRELATED: "Linked to a larger, already-tracked upstream/systemic failure.",
    ISSUE_COULD_NOT_BE_TRACED: "The incident's reported errors/traces could not be located in telemetry.",
    NEED_MANUAL_INTERVENTION: "Requires manual domain expertise, or no automated check exists for this system yet.",
}

# CORRELATED and ISSUE_COULD_NOT_BE_TRACED aren't produced by the current
# synthesizer -- CORRELATED needs cross-incident/known-outage tracking, and
# ISSUE_COULD_NOT_BE_TRACED needs a check that verifies the ticket's own
# reported error/trace actually exists in telemetry. Both are future work,
# not implemented yet; listed here so the vocabulary is complete regardless.
