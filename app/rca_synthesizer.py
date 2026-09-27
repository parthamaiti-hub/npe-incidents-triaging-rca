"""Deterministic RCA synthesis: pattern-match the evidence list produced by
playbook execution into a root cause. Purely rule-based -- no LLM/RAG.
"""

from app import rca_status
from app.correlation import CorrelationContext

WARN_OR_ERROR = ("WARN", "ERROR")

# Every named pattern below is a *correlation* (two signals both showing an
# issue), not a confirmed causal proof -- so even with real (non-stubbed)
# telemetry, the honest ceiling is PROBABLE, never IDENTIFIED. IDENTIFIED is
# reserved for a future, more direct confirmation mechanism this tool
# doesn't have yet. "correlated_systemic_issue" is the one
# exception -- CORRELATED is a distinct confidence tier from PROBABLE, not
# a stronger version of it: it says "this matches a pattern of independent
# reports," not "this specific evidence points at a cause."
PATTERN_RCA_STATUS = {
    "correlated_systemic_issue": rca_status.CORRELATED,
    "data_quality_pipeline_change": rca_status.PROBABLE,
    "environment_config": rca_status.PROBABLE,
    "functional_defect_recent_release": rca_status.PROBABLE,
    "generic_issues_in_checks": rca_status.INCONCLUSIVE,
    "inconclusive": rca_status.INCONCLUSIVE,
}


def _by_check(evidence: list[dict]) -> dict[str, dict]:
    return {e["check"]: e for e in evidence}


def synthesize_rca(evidence: list[dict], correlation: CorrelationContext | None = None) -> dict:
    """evidence: [{"check", "params", "status", "details"}, ...] from
    app.playbook_engine.execute_playbook. correlation:
    set when app.correlation.correlate_incident found CORRELATION_THRESHOLD+
    incidents against the same application/category within the window --
    checked first, ahead of this incident's own evidence, since a
    confirmed burst is itself stronger diagnostic signal than any one
    incident's (currently [STUBBED]) checks.

    Returns {"matched_pattern", "rca_status", "root_cause_summary",
    "contributing_factors", "recommended_actions"}."""
    if correlation is not None:
        return {
            "matched_pattern": "correlated_systemic_issue",
            "rca_status": rca_status.CORRELATED,
            "root_cause_summary": (
                f"Correlated with {correlation.incident_count - 1} other incident(s) against "
                f"{correlation.source_system_id} within {correlation.window_minutes} minutes "
                f"(group {correlation.group_id}). Likely one systemic issue, not independent causes."
            ),
            "contributing_factors": [f"Sibling incidents: {', '.join(correlation.sibling_jira_keys)}"]
            if correlation.sibling_jira_keys
            else [],
            "recommended_actions": [
                "Treat as one systemic issue -- notify once for the group, not per-ticket",
                *(
                    [f"See root cause on the group's representative incident ({correlation.representative_jira_key})"]
                    if correlation.representative_jira_key
                    else []
                ),
            ],
        }

    by_check = _by_check(evidence)

    def status(check: str) -> str | None:
        return by_check.get(check, {}).get("status")

    def details(check: str) -> str:
        return by_check.get(check, {}).get("details", "")

    data_contract_bad = status("data_contract_violations") in WARN_OR_ERROR
    pipeline_changed = status("recent_pipeline_changes") in WARN_OR_ERROR
    env_bad = status("service_health") in WARN_OR_ERROR or status("config_diff") in WARN_OR_ERROR
    func_bad = status("error_logs") in WARN_OR_ERROR and status("recent_deployments") in WARN_OR_ERROR

    if data_contract_bad and pipeline_changed:
        matched_pattern = "data_quality_pipeline_change"
        result = {
            "root_cause_summary": (
                "Likely data quality issue caused by an upstream pipeline change. "
                f"{details('data_contract_violations')}. {details('recent_pipeline_changes')}."
            ),
            "contributing_factors": [
                details("recent_pipeline_changes"),
                details("data_contract_violations"),
            ],
            "recommended_actions": [
                "Review the recent pipeline change for correctness/default-value handling",
                "Backfill/repair affected data per the data contract violation",
                "Raise or add a data-quality alert threshold check",
            ],
        }

    elif env_bad:
        matched_pattern = "environment_config"
        factors = [d for d in (details("service_health"), details("config_diff")) if d]
        result = {
            "root_cause_summary": "Likely environment/configuration issue. " + " ".join(factors),
            "contributing_factors": factors,
            "recommended_actions": [
                "Compare NPE configuration against a baseline environment",
                "Verify service health/connectivity in NPE",
            ],
        }

    elif func_bad:
        matched_pattern = "functional_defect_recent_release"
        result = {
            "root_cause_summary": (
                "Likely functional bug introduced by a recent release. "
                f"{details('error_logs')}. {details('recent_deployments')}."
            ),
            "contributing_factors": [details("error_logs"), details("recent_deployments")],
            "recommended_actions": [
                "Review the recent deployment/commit for the affected component",
                "Add/expand test coverage for the failing scenario",
            ],
        }

    else:
        warn_or_error = [e for e in evidence if e["status"] in WARN_OR_ERROR]
        if warn_or_error:
            matched_pattern = "generic_issues_in_checks"
            result = {
                "root_cause_summary": "Incident likely caused by issues found in automated checks; see evidence.",
                "contributing_factors": [f"{e['check']}: {e['details']}" for e in warn_or_error],
                "recommended_actions": ["Review the WARN/ERROR checks in the evidence list for next steps"],
            }
        else:
            matched_pattern = "inconclusive"
            result = {
                "root_cause_summary": "Root cause inconclusive - manual investigation required.",
                "contributing_factors": [],
                "recommended_actions": ["Escalate for manual investigation"],
            }

    result["matched_pattern"] = matched_pattern
    result["rca_status"] = PATTERN_RCA_STATUS[matched_pattern]
    return result
