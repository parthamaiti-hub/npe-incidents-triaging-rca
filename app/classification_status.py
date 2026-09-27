"""Display-layer vocabulary for the classification outcome (did we determine
which source_system/category?) -- distinct from RCA status (app.rca_status,
the diagnostic confidence of the root cause itself).

app.classification's internal "resolved" / "any_category_pending_llm" /
"manual_triage" values stay as-is (used throughout the codebase and stored
on Incident.classification_status) -- this is only the human-facing label
shown in reports, so internal jargon like "resolved" never reads as if the
incident itself were fixed.
"""

SUCCESS = "Success"
PROBABLE = "Probable"
NOT_DETERMINED = "Not determined"

DISPLAY_MAP = {
    "resolved": SUCCESS,
    "any_category_pending_llm": PROBABLE,
    "manual_triage": NOT_DETERMINED,
    # An LLM-resolved incident displays the same as a
    # pending-LLM one, deliberately -- both mean "lower confidence than a
    # rule match," never "Success". See Incident.classification_method for
    # the machine-readable distinction between the two.
    "llm_resolved": PROBABLE,
}


def display_status(internal_status: str) -> str:
    return DISPLAY_MAP.get(internal_status, internal_status)
