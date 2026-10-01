"""Declared contract for every callable check_type -- the CNCF Serverless
Workflow spec's `use.functions`/`use.retries` concept, expressed in Python
instead of duplicated as YAML boilerplate in every workflow document.

Covers all 20 check_types, so every existing playbook -- real or
fictional, any category -- validates against it. Only the FUNC category's
checks (service_health/error_logs/apm_traces/recent_deployments/
feature_flags/dependent_services_health) are exercised by shipped
playbooks, but the registry itself is a complete inventory.

Every entry's implementation resolves to the existing
app.checks.run_check(check_type, params) -- this registry is the declared
contract in front of that stub, not a rewrite of it.
"""

from typing import Literal

from pydantic import BaseModel


class ParamSpec(BaseModel):
    name: str
    type: Literal["string", "int", "float", "bool", "list[string]", "dict"]
    required: bool = True


class RetryPolicy(BaseModel):
    max_attempts: int = 3
    delay_seconds: int = 2
    exponential_backoff: bool = True


class FunctionSpec(BaseModel):
    name: str
    description: str
    params: list[ParamSpec]
    default_retry: RetryPolicy = RetryPolicy()
    # Server-assigned, populated once this entry is backed by a real
    # FunctionDefinitionVersion row (None for the built-in bootstrap
    # defaults before any DB seed/refresh) -- any value submitted by a
    # client on write is ignored, the server always sets these itself.
    version_id: str | None = None
    version_number: int | None = None
    # Whether app.checks.STUB_RESULTS actually has an entry for
    # (name, version_number) -- i.e. whether this version can be executed
    # at all, not just declared. Server-computed (app/routers/functions.py);
    # not populated by the FUNCTION_REGISTRY cache itself since checking it
    # requires app.checks, which this module deliberately doesn't import
    # (contract vs. implementation stay separate concerns).
    has_implementation: bool | None = None
    # Lifecycle status of this version (Sol-104): draft | active |
    # deprecated | retired (legacy rows may say superseded == deprecated).
    # None for the built-in defaults, which behave as active.
    status: str | None = None

    def required_param_names(self) -> set[str]:
        return {p.name for p in self.params if p.required}


def _p(name: str, type: str = "string", required: bool = True) -> ParamSpec:
    return ParamSpec(name=name, type=type, required=required)


DEFAULT_FUNCTION_REGISTRY: dict[str, FunctionSpec] = {
    "service_health": FunctionSpec(
        name="service_health",
        description="Call a service health/status endpoint and detect if the app in NPE is up and responsive.",
        params=[_p("env"), _p("endpoint"), _p("timeout_ms", "int", required=False)],
    ),
    "infra_health": FunctionSpec(
        name="infra_health",
        description="Check underlying infra (cluster, nodes, pods) for errors/unhealthy state.",
        params=[_p("env"), _p("cluster"), _p("namespace", required=False)],
    ),
    "config_diff": FunctionSpec(
        name="config_diff",
        description="Compare NPE configuration vs a baseline env and highlight differences that may explain the issue.",
        params=[_p("env"), _p("baseline_env"), _p("app"), _p("config_key_prefix", required=False)],
    ),
    "feature_flags": FunctionSpec(
        name="feature_flags",
        description="Verify critical feature flags/toggles for a given app in NPE are in expected states.",
        params=[_p("env"), _p("app"), _p("flags_expected", "dict", required=False)],
    ),
    "email_suppression": FunctionSpec(
        name="email_suppression",
        description="Ensure outbound email/notification suppression rules are enabled for NPE.",
        params=[_p("env"), _p("app"), _p("notification_service")],
    ),
    "recent_deployments": FunctionSpec(
        name="recent_deployments",
        description="Check if there was a recent deployment/rollback that might correlate with the incident.",
        params=[_p("env"), _p("app"), _p("lookback_minutes", "int"), _p("min_severity", required=False)],
    ),
    "pipeline_status": FunctionSpec(
        name="pipeline_status",
        description="Verify status of data pipelines/jobs (running, failed, skipped) relevant to the incident.",
        params=[_p("env"), _p("pipeline_name_pattern"), _p("lookback_minutes", "int", required=False)],
    ),
    "pipeline_error_logs": FunctionSpec(
        name="pipeline_error_logs",
        description="Scan error logs for pipelines in a timeframe, looking for failures, spikes, or new error patterns.",
        params=[
            _p("env"),
            _p("pipeline_name_pattern"),
            _p("lookback_minutes", "int"),
            _p("error_threshold", "int", required=False),
        ],
    ),
    "recent_pipeline_changes": FunctionSpec(
        name="recent_pipeline_changes",
        description="Check if there were code/config changes to pipelines that feed the affected system/tables.",
        params=[_p("env"), _p("related_system"), _p("related_tables", "list[string]"), _p("lookback_minutes", "int")],
    ),
    "upstream_source_freshness": FunctionSpec(
        name="upstream_source_freshness",
        description="Measure freshness/lag of upstream inputs (tables, feeds) and detect stale or delayed sources.",
        params=[_p("env"), _p("pipelines", "list[string]"), _p("freshness_threshold_minutes", "int")],
    ),
    "upstream_pipeline_status": FunctionSpec(
        name="upstream_pipeline_status",
        description="Check status and health of upstream pipelines that DAS/DCD/DPS depend on.",
        params=[_p("env"), _p("pipelines", "list[string]"), _p("lookback_minutes", "int", required=False)],
    ),
    "data_contract_violations": FunctionSpec(
        name="data_contract_violations",
        description="Query data-quality / contract monitoring to find any violations (nulls, thresholds) related to the system.",
        params=[_p("env"), _p("system"), _p("severity_threshold")],
    ),
    "schema_mismatch": FunctionSpec(
        name="schema_mismatch",
        description="Compare schemas (columns, types) in NPE to a reference env and detect breaking diffs.",
        params=[_p("env"), _p("reference_env"), _p("schema_pattern")],
    ),
    "null_density": FunctionSpec(
        name="null_density",
        description="Profile data for high null/empty rates in key columns or tables beyond a threshold.",
        params=[_p("env"), _p("tables", "list[string]"), _p("null_threshold_pct", "float")],
    ),
    "queue_lag": FunctionSpec(
        name="queue_lag",
        description="Check message queue/topic lag to detect backlogs in processing flows (for DPS/DO).",
        params=[_p("env"), _p("queues", "list[string]"), _p("lag_threshold", "int")],
    ),
    "data_source_connectivity": FunctionSpec(
        name="data_source_connectivity",
        description="Verify that analytics/report services (DAS) can connect to their data sources (DBs, warehouses, APIs).",
        params=[_p("env"), _p("app"), _p("data_sources", "list[string]")],
    ),
    "error_logs": FunctionSpec(
        name="error_logs",
        description="Scan application logs for errors/exceptions related to the incident timeframe and context.",
        params=[_p("env"), _p("app"), _p("lookback_minutes", "int"), _p("severity_levels", "list[string]", required=False)],
    ),
    "apm_traces": FunctionSpec(
        name="apm_traces",
        description="Query APM/trace system for failed or slow transactions correlating with the incident.",
        params=[_p("env"), _p("app"), _p("lookback_minutes", "int"), _p("latency_threshold_ms", "int", required=False)],
    ),
    "dependent_services_health": FunctionSpec(
        name="dependent_services_health",
        description="Check health of known downstream/upstream services that the target app depends on.",
        params=[_p("env"), _p("app"), _p("dependencies", "list[string]")],
    ),
    "feature_toggle_audit": FunctionSpec(
        name="feature_toggle_audit",
        description="Inspect recent changes to feature toggles/experiments impacting behavior.",
        params=[_p("env"), _p("app"), _p("lookback_minutes", "int", required=False)],
    ),
}


# The live, synchronously-readable registry every validator/dispatcher in
# this codebase (app.workflow_spec.TaskSpec, app.playbook_engine) imports by
# name. Starts as a copy of the built-in defaults above so catalog
# loading/tests work with zero DB setup; app.main's lifespan refreshes it
# from the function_definition table (the real source of truth once
# app/routers/functions.py's CRUD is in play) on startup, and every write
# through that router updates it directly. Mutated in place (never
# rebound) so every `from app.function_registry import FUNCTION_REGISTRY`
# import sees live updates through the same dict object.
FUNCTION_REGISTRY: dict[str, FunctionSpec] = dict(DEFAULT_FUNCTION_REGISTRY)

# Every DB-backed version of every function, keyed (name, version_number)
# -- not only the active one. A playbook task pinned to an older (or a
# draft) version is validated and executed against *that* version's
# contract, never the active one's (Sol-104 V4/V5). Same in-place-mutation
# discipline as FUNCTION_REGISTRY.
FUNCTION_VERSIONS: dict[tuple[str, int], FunctionSpec] = {}

# Statuses a task may be pinned to. `draft` is allowed in a build request
# (to try a new version out) but refused at approval -- see
# app.task_pinning. `superseded` is the pre-Sol-104 name for `deprecated`.
RUNNABLE_STATUSES = ("draft", "active", "deprecated", "superseded")


def reset_function_registry(entries: dict[str, FunctionSpec], versions: dict[tuple[str, int], FunctionSpec] | None = None) -> None:
    FUNCTION_REGISTRY.clear()
    FUNCTION_REGISTRY.update(entries)
    if versions is not None:
        FUNCTION_VERSIONS.clear()
        FUNCTION_VERSIONS.update(versions)


def snapshot_function_registry() -> tuple[dict, dict]:
    """For tests: (FUNCTION_REGISTRY, FUNCTION_VERSIONS) copies to hand back
    to reset_function_registry afterwards."""
    return dict(FUNCTION_REGISTRY), dict(FUNCTION_VERSIONS)


def spec_from_row(row) -> FunctionSpec:
    return FunctionSpec(
        name=row.function_definition_id,
        description=row.description,
        params=[ParamSpec(**p) for p in row.params],
        default_retry=RetryPolicy(**row.default_retry),
        version_id=row.id,
        version_number=row.version_number,
        status=row.status,
    )


def cache_function_version(spec: FunctionSpec) -> None:
    """Keeps both caches in step after one version row was written."""
    FUNCTION_VERSIONS[(spec.name, spec.version_number)] = spec
    if spec.status == "active":
        FUNCTION_REGISTRY[spec.name] = spec
    elif FUNCTION_REGISTRY.get(spec.name) is not None and FUNCTION_REGISTRY[spec.name].version_number == spec.version_number:
        # This version was the active one and has just been demoted.
        FUNCTION_REGISTRY[spec.name] = spec


def forget_function(name: str) -> None:
    FUNCTION_REGISTRY.pop(name, None)
    for key in [k for k in FUNCTION_VERSIONS if k[0] == name]:
        del FUNCTION_VERSIONS[key]


def is_known_function(name: str) -> bool:
    return name in FUNCTION_REGISTRY or any(k[0] == name for k in FUNCTION_VERSIONS)


def active_version_number(name: str) -> int | None:
    spec = FUNCTION_REGISTRY.get(name)
    if spec is None:
        return None
    return spec.version_number or 1


def get_function_spec(name: str, version_number: int) -> FunctionSpec | None:
    """The contract of exactly (name, version_number), or None.

    Falls back, in order, to the active spec when it *is* that version, and
    to the built-in default for version 1 -- load_function_registry seeds
    every v1 from DEFAULT_FUNCTION_REGISTRY, so on an unseeded database (or
    in a process that hasn't refreshed from the DB) the default is v1's
    contract by construction."""
    spec = FUNCTION_VERSIONS.get((name, version_number))
    if spec is not None:
        return spec
    active = FUNCTION_REGISTRY.get(name)
    if active is not None and (active.version_number or 1) == version_number:
        return active
    if version_number == 1:
        return DEFAULT_FUNCTION_REGISTRY.get(name)
    return None


def is_runnable(spec: FunctionSpec) -> bool:
    return spec.status is None or spec.status in RUNNABLE_STATUSES


async def refresh_function_registry_from_db(db) -> None:
    """Reloads both caches from the database: FUNCTION_VERSIONS from every
    FunctionDefinitionVersion, FUNCTION_REGISTRY from the active ones. A
    no-op if the collection is empty (not seeded yet) -- keeps the built-in
    defaults rather than wiping them, so an unseeded DB doesn't break
    validation."""
    from app.models import FunctionDefinitionVersion
    from app.repositories.base import find

    _load_rows(await find(db, FunctionDefinitionVersion))


def refresh_function_registry_from_db_sync(db) -> None:
    """Same as refresh_function_registry_from_db, for the sync load scripts
    (dataloadscripts/load_catalog.py runs in its own process, so without
    this it would only ever see the built-in defaults)."""
    from app.models import FunctionDefinitionVersion

    _load_rows([FunctionDefinitionVersion.from_doc(doc) for doc in db[FunctionDefinitionVersion.COLLECTION].find()])


def _load_rows(rows) -> None:
    if not rows:
        FUNCTION_VERSIONS.clear()
        return
    specs = [spec_from_row(row) for row in rows]
    reset_function_registry(
        {spec.name: spec for spec in specs if spec.status == "active"},
        {(spec.name, spec.version_number): spec for spec in specs},
    )
