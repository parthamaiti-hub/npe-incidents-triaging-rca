"""check_type stub implementations (placeholders). Each currently returns a fixed,
deterministic simulated result -- no real backend integration exists yet
(Splunk, AppInsights, data-contract DB, CI/CD APIs). Replace individual
entries as real integrations become available; the playbook engine's
dispatch mechanism (app/playbook_engine.py) doesn't change when they do.

status is always one of "OK" | "WARN" | "ERROR", regardless of check_type,
so app/rca_synthesizer.py's pattern matching stays uniform.

Keyed by (check_type, version_number) -- a function's contract can be
published to a new version via app/routers/functions.py at any time (no
code deploy needed), but its *execution* is still real Python code, so a
version only actually runs once a matching entry exists here. Publishing
v2's contract without adding STUB_RESULTS["error_logs"][2] is deliberate:
two versions of the same function are two different pieces of code, not
just two different declared param lists -- run_check raises for a version
with no entry rather than silently falling back to another version's
behavior.

Version 1 of every check_type is a template-fill stub (below). Version 2,
where published, is real per-function Python code -- see
app/check_implementations.py.
"""

from app.check_implementations import CHECK_IMPLEMENTATIONS_V2

# (status, details_template) -- details_template is filled with the step's
# params via str.format(**params), falling back to the raw template if a
# referenced key is missing.
STUB_RESULTS: dict[str, dict[int, tuple[str, str]]] = {
    # Checks stubbed to report a finding, so the two authored real playbooks
    # (RCA_FIBER_FUNC, RCA_IDS_DATA) each hit a real RCA synthesis
    # pattern rather than only ever landing on "inconclusive".
    "error_logs": {
        1: (
            "WARN",
            "[STUBBED] 3 ERROR-level log entries found for {app} in the last {lookback_minutes} minutes",
        )
    },
    "recent_deployments": {
        1: (
            "WARN",
            "[STUBBED] 1 deployment to {app} found within the last {lookback_minutes} minutes",
        )
    },
    "data_contract_violations": {
        1: (
            "WARN",
            "[STUBBED] contract violation found for {system} (severity >= {severity_threshold})",
        )
    },
    "recent_pipeline_changes": {
        1: (
            "WARN",
            "[STUBBED] recent change found for {related_system} within the last {lookback_minutes} minutes",
        )
    },
    # Everything else stubbed clean (OK), so those patterns don't fire
    # unintentionally.
    "service_health": {1: ("OK", "[STUBBED] {endpoint} responded healthy")},
    "infra_health": {1: ("OK", "[STUBBED] {cluster} reports healthy")},
    "config_diff": {1: ("OK", "[STUBBED] no config drift found for {app} vs {baseline_env}")},
    "feature_flags": {1: ("OK", "[STUBBED] feature flags for {app} match expected state")},
    "email_suppression": {1: ("OK", "[STUBBED] suppression rules enabled for {app}")},
    "pipeline_status": {1: ("OK", "[STUBBED] pipelines matching {pipeline_name_pattern} are running")},
    "pipeline_error_logs": {1: ("OK", "[STUBBED] no pipeline error spikes found")},
    "upstream_source_freshness": {1: ("OK", "[STUBBED] upstream sources within freshness threshold")},
    "upstream_pipeline_status": {1: ("OK", "[STUBBED] upstream pipelines healthy")},
    "schema_mismatch": {1: ("OK", "[STUBBED] no schema drift found")},
    "null_density": {1: ("OK", "[STUBBED] null rates within threshold")},
    "queue_lag": {1: ("OK", "[STUBBED] queue lag within threshold")},
    "data_source_connectivity": {1: ("OK", "[STUBBED] data sources reachable")},
    "apm_traces": {1: ("OK", "[STUBBED] no failed/slow transactions found")},
    "dependent_services_health": {1: ("OK", "[STUBBED] dependent services healthy")},
    "feature_toggle_audit": {1: ("OK", "[STUBBED] no recent toggle changes found")},
}


def _available_versions(check_type: str) -> list[int]:
    versions = sorted(STUB_RESULTS.get(check_type, {}))
    if check_type in CHECK_IMPLEMENTATIONS_V2:
        versions.append(2)
    return versions


def has_implementation(check_type: str, version_number: int) -> bool:
    if version_number in STUB_RESULTS.get(check_type, {}):
        return True
    return version_number == 2 and check_type in CHECK_IMPLEMENTATIONS_V2


async def run_check(check_type: str, params: dict, version_number: int = 1) -> dict:
    if version_number in STUB_RESULTS.get(check_type, {}):
        status, template = STUB_RESULTS[check_type][version_number]
        try:
            details = template.format(**params)
        except (KeyError, IndexError):
            details = template
        return {"status": status, "details": details}

    if version_number == 2 and check_type in CHECK_IMPLEMENTATIONS_V2:
        return await CHECK_IMPLEMENTATIONS_V2[check_type](params)

    if check_type not in STUB_RESULTS and check_type not in CHECK_IMPLEMENTATIONS_V2:
        raise ValueError(f"Unknown check_type: {check_type!r}")
    raise ValueError(
        f"No implementation for check_type {check_type!r} version {version_number} "
        f"(have versions: {_available_versions(check_type)})"
    )

    return {"status": status, "details": details}
