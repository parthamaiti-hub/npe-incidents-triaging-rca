from app import rca_status
from app.correlation import CorrelationContext
from app.rca_synthesizer import synthesize_rca


def _evidence(check, status, details="", **params):
    return {"check": check, "params": params, "status": status, "details": details}


def test_data_quality_pipeline_change_pattern():
    evidence = [
        _evidence("data_contract_violations", "WARN", "nulls exceed threshold"),
        _evidence("schema_mismatch", "OK", "no drift"),
        _evidence("null_density", "WARN", "4.2% nulls"),
        _evidence("recent_pipeline_changes", "WARN", "change found"),
    ]
    rca = synthesize_rca(evidence)
    assert rca["matched_pattern"] == "data_quality_pipeline_change"
    assert rca["rca_status"] == rca_status.PROBABLE
    assert "nulls exceed threshold" in rca["root_cause_summary"]
    assert len(rca["contributing_factors"]) == 2
    assert len(rca["recommended_actions"]) == 3


def test_environment_config_pattern():
    evidence = [
        _evidence("service_health", "ERROR", "endpoint unreachable"),
        _evidence("config_diff", "OK", "no drift"),
    ]
    rca = synthesize_rca(evidence)
    assert rca["matched_pattern"] == "environment_config"
    assert rca["rca_status"] == rca_status.PROBABLE
    assert "endpoint unreachable" in rca["root_cause_summary"]


def test_functional_defect_pattern():
    evidence = [
        _evidence("error_logs", "WARN", "3 errors found"),
        _evidence("recent_deployments", "WARN", "1 deployment found"),
        _evidence("apm_traces", "OK", "no anomalies"),
    ]
    rca = synthesize_rca(evidence)
    assert rca["matched_pattern"] == "functional_defect_recent_release"
    assert rca["rca_status"] == rca_status.PROBABLE
    assert "3 errors found" in rca["root_cause_summary"]
    assert "1 deployment found" in rca["root_cause_summary"]


def test_generic_issues_fallback_when_no_specific_pattern_matches():
    evidence = [
        _evidence("null_density", "WARN", "elevated nulls"),
        _evidence("queue_lag", "OK", "within threshold"),
    ]
    rca = synthesize_rca(evidence)
    assert rca["matched_pattern"] == "generic_issues_in_checks"
    assert rca["rca_status"] == rca_status.INCONCLUSIVE
    assert any("null_density" in f for f in rca["contributing_factors"])


def test_inconclusive_when_everything_ok():
    evidence = [
        _evidence("service_health", "OK", "healthy"),
        _evidence("config_diff", "OK", "no drift"),
    ]
    rca = synthesize_rca(evidence)
    assert rca["matched_pattern"] == "inconclusive"
    assert rca["rca_status"] == rca_status.INCONCLUSIVE
    assert rca["contributing_factors"] == []


def test_empty_evidence_is_inconclusive():
    rca = synthesize_rca([])
    assert rca["matched_pattern"] == "inconclusive"
    assert rca["rca_status"] == rca_status.INCONCLUSIVE


def test_correlation_context_wins_over_the_incidents_own_evidence():
    # A confirmed burst against the same application is
    # checked first -- even evidence that would otherwise match a strong
    # pattern (functional_defect_recent_release) is superseded by CORRELATED.
    evidence = [
        _evidence("error_logs", "WARN", "3 errors found"),
        _evidence("recent_deployments", "WARN", "1 deployment found"),
    ]
    correlation = CorrelationContext(
        group_id="CG-1",
        source_system_id="SYS_HSI",
        incident_count=3,
        window_minutes=30,
        sibling_jira_keys=["TT-1", "TT-2"],
        representative_jira_key="TT-1",
    )
    rca = synthesize_rca(evidence, correlation=correlation)
    assert rca["matched_pattern"] == "correlated_systemic_issue"
    assert rca["rca_status"] == rca_status.CORRELATED
    assert "2 other incident(s)" in rca["root_cause_summary"]
    assert "SYS_HSI" in rca["root_cause_summary"]
    assert "TT-1, TT-2" in rca["contributing_factors"][0]
    assert any("TT-1" in a for a in rca["recommended_actions"])


def test_correlation_context_with_no_siblings_listed_omits_factor():
    correlation = CorrelationContext(
        group_id="CG-1",
        source_system_id="SYS_HSI",
        incident_count=2,
        window_minutes=30,
        sibling_jira_keys=[],
        representative_jira_key=None,
    )
    rca = synthesize_rca([], correlation=correlation)
    assert rca["contributing_factors"] == []
    assert len(rca["recommended_actions"]) == 1


def test_no_pattern_ever_claims_identified():
    # Correlation-based pattern matches are never a confirmed causal proof,
    # even with real (non-stubbed) evidence -- IDENTIFIED is reserved for a
    # future, more direct confirmation mechanism this tool doesn't have yet.
    from app.rca_synthesizer import PATTERN_RCA_STATUS

    assert rca_status.IDENTIFIED not in PATTERN_RCA_STATUS.values()
