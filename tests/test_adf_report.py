from app import rca_status
from app.adf_report import APP_NAME, build_rca_comment_adf


def _flatten_text(node: dict) -> str:
    """Collects all text node contents from an ADF document, for easy
    substring assertions without walking the tree in every test."""
    if node.get("type") == "text":
        return node.get("text", "")
    return "".join(_flatten_text(child) for child in node.get("content", []))


RESOLVED_WITH_PLAYBOOK_RESULT = {
    "jira_key": "TT-1",
    "summary": "Activation failed for HSI Gateway devices in QLAB03",
    "status": "To Do",
    "priority": "Medium",
    "classification": {
        "status": "resolved",
        "matched_rule_id": "IMR_HSI_KEYWORD",
        "source_system_id": "SYS_HSI",
        "category": "FUNCTIONAL DEFECT (QA/UAT)",
    },
    "evidence": [
        {"check": "error_logs", "params": {}, "status": "WARN", "details": "[STUBBED] 3 errors found"},
        {"check": "apm_traces", "params": {}, "status": "OK", "details": "[STUBBED] no anomalies"},
    ],
    "rca": {
        "matched_pattern": "functional_defect_recent_release",
        "rca_status": rca_status.PROBABLE,
        "root_cause_summary": "Likely functional bug introduced by a recent release.",
        "contributing_factors": ["3 errors found"],
        "recommended_actions": ["Review the recent deployment"],
    },
}

NO_PLAYBOOK_RESULT = {
    "jira_key": "TT-3",
    "summary": "RSP is not returning ban status",
    "status": "To Do",
    "priority": "High",
    "classification": {
        "status": "resolved",
        "matched_rule_id": "IMR_RSP_KEYWORD",
        "source_system_id": "SYS_RSP",
        "category": "FUNCTIONAL DEFECT (QA/UAT)",
    },
    "evidence": None,
    "rca": {
        "matched_pattern": "no_playbook",
        "rca_status": rca_status.NEED_MANUAL_INTERVENTION,
        "root_cause_summary": "No RCA playbook configured yet for (SYS_RSP, FUNCTIONAL DEFECT (QA/UAT)).",
        "contributing_factors": [],
        "recommended_actions": ["Author an RCA_PLAYBOOK for this (source_system, category) pair"],
    },
}


def test_adf_document_is_well_formed():
    adf = build_rca_comment_adf(RESOLVED_WITH_PLAYBOOK_RESULT)
    assert adf["type"] == "doc"
    assert adf["version"] == 1
    assert isinstance(adf["content"], list) and len(adf["content"]) > 0


def test_adf_includes_app_attribution():
    adf = build_rca_comment_adf(RESOLVED_WITH_PLAYBOOK_RESULT)
    text = _flatten_text({"content": adf["content"]})
    assert APP_NAME in text


def test_adf_never_claims_the_incident_is_resolved():
    # The point of this whole change: RCA scope is diagnosis, not
    # remediation -- neither status line may ever read "resolved" as if the
    # incident itself were fixed. RCA Status uses the 6-value vocabulary;
    # Classification Status uses its own distinct 3-value vocabulary
    # (Success/Probable/Not determined) -- never the raw internal
    # "resolved"/"manual_triage"/"any_category_pending_llm" value.
    adf = build_rca_comment_adf(RESOLVED_WITH_PLAYBOOK_RESULT)
    text = _flatten_text({"content": adf["content"]})
    assert "RCA Status: Probable" in text
    assert "Classification Status: Success" in text
    assert "Classification Status: resolved" not in text


def test_adf_root_cause_panel_colored_warning_for_probable():
    adf = build_rca_comment_adf(RESOLVED_WITH_PLAYBOOK_RESULT)
    panels = [n for n in adf["content"] if n["type"] == "panel"]
    root_cause_panel = next(p for p in panels if "RCA Status" in _flatten_text(p))
    assert root_cause_panel["attrs"]["panelType"] == "warning"


def test_adf_root_cause_panel_colored_warning_for_need_manual_intervention():
    adf = build_rca_comment_adf(NO_PLAYBOOK_RESULT)
    panels = [n for n in adf["content"] if n["type"] == "panel"]
    root_cause_panel = next(p for p in panels if "RCA Status" in _flatten_text(p))
    assert root_cause_panel["attrs"]["panelType"] == "warning"


def test_adf_evidence_bullets_include_status_emoji():
    adf = build_rca_comment_adf(RESOLVED_WITH_PLAYBOOK_RESULT)
    text = _flatten_text({"content": adf["content"]})
    assert "⚠️" in text  # WARN
    assert "✅" in text  # OK


def test_adf_flags_stubbed_evidence_with_disclaimer():
    adf = build_rca_comment_adf(RESOLVED_WITH_PLAYBOOK_RESULT)
    text = _flatten_text({"content": adf["content"]})
    assert "simulated for testing" in text


def test_adf_no_playbook_result_has_no_evidence_section():
    adf = build_rca_comment_adf(NO_PLAYBOOK_RESULT)
    headings = [_flatten_text(n) for n in adf["content"] if n["type"] == "heading"]
    assert "Evidence" not in headings
