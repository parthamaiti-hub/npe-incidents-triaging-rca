"""Reserved category values. Every other category is free text (see
design/NPE_Incident_PlaybookMappingProcess.md §4).

ANY      -- only on a mapping rule: "the source system is identified, the
            category is not". Never on a playbook.
DEFAULT  -- only on a playbook: the source system's fallback playbook, run
            when the incident's category couldn't be mapped, or was mapped
            to a (system, category) with no playbook. The execution is then
            marked triage_mode="DefaultRCA". Never on a mapping rule -- a
            rule can't route *to* the fallback, only the absence of a
            specific playbook does.
"""

ANY_CATEGORY = "ANY"
DEFAULT_CATEGORY = "DEFAULT"

RESERVED_CATEGORIES = (ANY_CATEGORY, DEFAULT_CATEGORY)
