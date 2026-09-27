"""Registry for check_type v2 ("functional dummy") implementations.

2026-09-23: each check_type's actual code moved out of this file into its
own module under app/check_types/ (e.g. app/check_types/error_logs_v2.py),
one file per (check_type, version) -- independently editable without
touching 19 unrelated functions in the same file. This module is now just
the manifest: check_type name -> that module's run() function.
app.checks.run_check dispatches through CHECK_IMPLEMENTATIONS_V2 exactly as
before -- only where each function's code physically lives has changed.

To add a real v3 for some check_type once an integration exists: add
app/check_types/<name>_v3.py (same `run(params)` shape), then extend
app.checks.run_check's dispatch to look it up -- its `if version_number ==
2` branch is intentionally still hardcoded to v2 today, a deliberate scope
decision to keep this split mechanical rather than also generalizing the
version dispatch.
"""

from app.check_types import (
    apm_traces_v2,
    config_diff_v2,
    data_contract_violations_v2,
    data_source_connectivity_v2,
    dependent_services_health_v2,
    email_suppression_v2,
    error_logs_v2,
    feature_flags_v2,
    feature_toggle_audit_v2,
    infra_health_v2,
    null_density_v2,
    pipeline_error_logs_v2,
    pipeline_status_v2,
    queue_lag_v2,
    recent_deployments_v2,
    recent_pipeline_changes_v2,
    schema_mismatch_v2,
    service_health_v2,
    upstream_pipeline_status_v2,
    upstream_source_freshness_v2,
)

# check_type -> its v2 callable. app.checks.run_check dispatches here for
# version_number == 2; app.checks.has_implementation checks membership here
# too. Every check_type from app.function_registry.FUNCTION_REGISTRY has an
# entry -- keep this in sync when a new check_type is added there.
CHECK_IMPLEMENTATIONS_V2 = {
    "service_health": service_health_v2.run,
    "infra_health": infra_health_v2.run,
    "config_diff": config_diff_v2.run,
    "feature_flags": feature_flags_v2.run,
    "email_suppression": email_suppression_v2.run,
    "recent_deployments": recent_deployments_v2.run,
    "pipeline_status": pipeline_status_v2.run,
    "pipeline_error_logs": pipeline_error_logs_v2.run,
    "recent_pipeline_changes": recent_pipeline_changes_v2.run,
    "upstream_source_freshness": upstream_source_freshness_v2.run,
    "upstream_pipeline_status": upstream_pipeline_status_v2.run,
    "data_contract_violations": data_contract_violations_v2.run,
    "schema_mismatch": schema_mismatch_v2.run,
    "null_density": null_density_v2.run,
    "queue_lag": queue_lag_v2.run,
    "data_source_connectivity": data_source_connectivity_v2.run,
    "error_logs": error_logs_v2.run,
    "apm_traces": apm_traces_v2.run,
    "dependent_services_health": dependent_services_health_v2.run,
    "feature_toggle_audit": feature_toggle_audit_v2.run,
}
