"""Workflow orchestration service: build a CNCF Serverless Workflow-shaped
document from an ask (a use case + an ordered list of {call, with} function
requests), render it for approval, execute only approved versions, and keep
every execution reproducible.

Async throughout (an AsyncDatabase handle) to match app.e2e_pipeline, its
only current caller. dataloadscripts/load_catalog.py is sync and doesn't use this module
directly -- it creates the initial static_authored version itself (a much
simpler bulk operation than the build/approve dance below), reusing
app.workflow_spec.TaskSpec for the same validation rules.
"""

import json
import uuid

from pydantic import ValidationError
from pymongo.asynchronous.database import AsyncDatabase
from redis.asyncio import Redis

from app import rca_status
from app.checks import has_implementation
from app.classification import FULLY_CLASSIFIED_STATUSES
from app.config import RCA_SYNTHESIS_LLM_ENABLED
from app.correlation import build_correlation_context, correlate_incident
from app.db import in_transaction
from app.function_registry import FUNCTION_REGISTRY
from app.idempotency import make_redis
from app.models import (
    Incident,
    WorkflowBuildRequest,
    WorkflowDefinition,
    WorkflowDefinitionVersion,
    WorkflowExecution,
    utcnow,
)
from app.playbook_engine import execute_workflow
from app.rca_synthesizer import synthesize_rca
from app.rca_worker import publish_rca_pending
from app.repositories.base import find_one, get, insert, max_version_number, update_fields
from app.workflow_spec import TaskSpec


class WorkflowValidationError(ValueError):
    """`errors` lists every failing task (an editor highlights all
    broken steps at once, not just the first) as {task_index, path, message};
    str(exc) stays the joined human-readable form existing callers use."""

    def __init__(self, message: str, errors: list[dict] | None = None):
        super().__init__(message)
        self.errors = errors or []


class WorkflowConflictError(ValueError):
    """The version an edit was based on is no longer the approved one --
    someone else published in the meantime."""


def _task_error_message(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return "; ".join(e["msg"].removeprefix("Value error, ") for e in exc.errors())
    return str(exc)


def _pin_key(task: dict) -> tuple:
    """What makes a task 'unchanged' for pin preservation -- the name is
    cosmetic, so it's left out; the function, its params and retry aren't."""
    return (task.get("call"), json.dumps(task.get("with") or {}, sort_keys=True), json.dumps(task.get("retry"), sort_keys=True))


def _validate_tasks(requested_functions: list[dict], preserve_pins_from: list[dict] | None = None) -> list[dict]:
    """Validates each {call, with, retry?} against FUNCTION_REGISTRY (unknown
    call or missing required param -> rejected before anything is stored),
    stamps each task with the function's *current* function_version_id/
    function_version_number (from the cache, so no extra DB round-trip --
    FUNCTION_REGISTRY is kept in sync with the active FunctionDefinitionVersion
    by app.function_registry's refresh/write paths), and rejects the build
    if that exact version has no matching app.checks.STUB_RESULTS
    entry -- publishing a function version's contract doesn't by itself make
    it executable; a developer still has to add the matching implementation.

    preserve_pins_from: when editing an existing version, a task
    identical to one in that version keeps its existing pin instead of being
    silently upgraded to the function's current version -- only added or
    modified tasks get re-pinned.

    Every task is checked; all failures are raised together. Returns the
    compiled, spec-shaped task list."""
    existing_pins: dict[tuple, dict] = {}
    for old in preserve_pins_from or []:
        if old.get("function_version_number") is not None:
            existing_pins.setdefault(_pin_key(old), old)

    tasks = []
    errors: list[dict] = []
    for i, raw in enumerate(requested_functions):
        try:
            task = TaskSpec.model_validate(raw)
        except Exception as exc:
            errors.append({"task_index": i, "path": f"requested_functions[{i}]", "message": _task_error_message(exc)})
            continue

        spec = FUNCTION_REGISTRY[task.call]
        version_id, version_number = spec.version_id, spec.version_number or 1
        pinned = existing_pins.get(_pin_key(task.model_dump(by_alias=True, exclude_none=True)))
        if pinned is not None and has_implementation(task.call, pinned["function_version_number"]):
            version_id, version_number = pinned.get("function_version_id"), pinned["function_version_number"]

        if not has_implementation(task.call, version_number):
            errors.append(
                {
                    "task_index": i,
                    "path": f"requested_functions[{i}]",
                    "message": f"{task.call!r} version {version_number} has a published contract but no "
                    f"matching implementation in app.checks.STUB_RESULTS yet",
                }
            )
            continue

        task.function_version_id = version_id
        task.function_version_number = version_number
        tasks.append(task.model_dump(by_alias=True, exclude_none=True))

    if errors:
        raise WorkflowValidationError("; ".join(f"{e['path']}: {e['message']}" for e in errors), errors)
    return tasks


async def build_workflow_from_request(
    db: AsyncDatabase,
    source_system_id: str,
    category: str,
    requested_functions: list[dict],
    requested_by: str,
    use_case_description: str | None = None,
    base_version: WorkflowDefinitionVersion | None = None,
) -> WorkflowBuildRequest:
    """The "ask": an ordered list of {call, with} function requests for a
    (source_system, category) use case. Validates and compiles them into a
    spec-shaped document and stores it as a WorkflowBuildRequest with
    status="rendered" -- generated_document is the JSON to show an
    approver. Raises WorkflowValidationError before storing anything if any
    requested function is unknown or missing a required param.

    base_version: set when this request is an edit of an existing
    version -- unchanged tasks keep that version's pins, and the lineage is
    recorded so approval can refuse a stale edit."""
    generated_document = _validate_tasks(
        requested_functions, preserve_pins_from=base_version.document if base_version is not None else None
    )

    request = WorkflowBuildRequest(
        source_system_id=source_system_id,
        category=category,
        requested_by=requested_by,
        use_case_description=use_case_description,
        requested_functions=requested_functions,
        generated_document=generated_document,
        status="rendered",
        base_version_id=base_version.id if base_version is not None else None,
    )
    return await insert(db, request)


async def _require_current_base(db: AsyncDatabase, definition_id: str, base_version_id: str) -> WorkflowDefinitionVersion:
    base = await get(db, WorkflowDefinitionVersion, base_version_id)
    if base is None or base.workflow_definition_id != definition_id:
        raise ValueError(f"No WorkflowDefinitionVersion {base_version_id!r} for definition {definition_id!r}")
    if base.status != "approved":
        raise WorkflowConflictError(
            f"Version v{base.version_number} is {base.status!r}, not the approved version -- "
            f"a newer version was published since this edit started. Reload and re-apply your changes."
        )
    return base


def preview_tasks(requested_functions: list[dict], base_version: WorkflowDefinitionVersion | None = None) -> list[dict]:
    """Dry-run compile (for validate-yaml): same validation and pinning
    as a real build, nothing stored."""
    return _validate_tasks(
        requested_functions, preserve_pins_from=base_version.document if base_version is not None else None
    )


async def edit_workflow_version(
    db: AsyncDatabase,
    definition_id: str,
    base_version_id: str,
    requested_functions: list[dict],
    edited_by: str,
    change_note: str | None = None,
) -> WorkflowBuildRequest:
    """An edit of an existing playbook is a new build request for
    the same (source_system, category) -- never an in-place change to the
    base version. Approving it (approve_build_request) publishes version N+1
    and supersedes N, exactly like any other build."""
    definition = await get(db, WorkflowDefinition, definition_id)
    if definition is None:
        raise ValueError(f"No WorkflowDefinition {definition_id!r}")
    base = await _require_current_base(db, definition_id, base_version_id)
    return await build_workflow_from_request(
        db,
        definition.source_system_id,
        definition.category,
        requested_functions,
        edited_by,
        change_note or f"Edit of v{base.version_number}",
        base_version=base,
    )


async def approve_build_request(db: AsyncDatabase, request_id: str, approved_by: str) -> WorkflowDefinitionVersion:
    """Only an approved version becomes executable. Creates the
    WorkflowDefinition if this is its first version; supersedes any
    previously-approved version for the same definition so at most one
    version is ever "approved" at a time -- all in one transaction, so a
    reader never sees zero or two approved versions. A concurrent approval
    of the same definition loses on the unique indexes (DuplicateKeyError)
    instead of producing a second approved version."""
    request = await get(db, WorkflowBuildRequest, request_id)
    if request is None:
        raise ValueError(f"No WorkflowBuildRequest {request_id!r}")
    if request.status != "rendered":
        raise ValueError(f"WorkflowBuildRequest {request_id!r} is {request.status!r}, not 'rendered'")
    if request.base_version_id is not None:
        base = await get(db, WorkflowDefinitionVersion, request.base_version_id)
        if base is not None:
            await _require_current_base(db, base.workflow_definition_id, base.id)

    async def publish(session) -> WorkflowDefinitionVersion:
        definition = await find_one(
            db,
            WorkflowDefinition,
            {"source_system_id": request.source_system_id, "category": request.category},
            session=session,
        )
        if definition is None:
            definition = await insert(
                db,
                WorkflowDefinition(
                    id=f"WFD_{request.source_system_id}_{uuid.uuid4().hex[:8]}",
                    source_system_id=request.source_system_id,
                    category=request.category,
                ),
                session=session,
            )

        # Supersede before inserting: the one-approved-per-definition
        # index is checked per write, not at commit.
        await db[WorkflowDefinitionVersion.COLLECTION].update_many(
            {"workflow_definition_id": definition.id, "status": "approved"},
            {"$set": {"status": "superseded"}},
            session=session,
        )
        version = WorkflowDefinitionVersion(
            workflow_definition_id=definition.id,
            version_number=await max_version_number(
                db, WorkflowDefinitionVersion, "workflow_definition_id", definition.id, session=session
            )
            + 1,
            document=request.generated_document,
            status="approved",
            source="dynamic_generated",
            build_request_id=request.id,
            created_by=request.requested_by,
            approved_by=approved_by,
            approved_at=utcnow(),
        )
        await insert(db, version, session=session)
        await db[WorkflowBuildRequest.COLLECTION].update_one(
            {"_id": request.id}, {"$set": {"status": "approved"}}, session=session
        )
        return version

    return await in_transaction(db, publish)


async def reject_build_request(db: AsyncDatabase, request_id: str, rejected_by: str, reason: str | None = None) -> None:
    request = await get(db, WorkflowBuildRequest, request_id)
    if request is None:
        raise ValueError(f"No WorkflowBuildRequest {request_id!r}")
    await update_fields(db, request, {"status": "rejected"})


async def get_active_workflow(
    db: AsyncDatabase, source_system_id: str, category: str
) -> WorkflowDefinitionVersion | None:
    """The version app.e2e_pipeline executes -- the current 'approved'
    version for this (source_system, category), whether it was
    static_authored (via load_catalog.py) or dynamic_generated (via
    build_workflow_from_request + approve_build_request)."""
    definition = await find_one(db, WorkflowDefinition, {"source_system_id": source_system_id, "category": category})
    if definition is None:
        return None
    return await find_one(db, WorkflowDefinitionVersion, {"workflow_definition_id": definition.id, "status": "approved"})


RETRYABLE_VERSION_STATUSES = ("approved", "superseded")


async def get_version_for_retry(db: AsyncDatabase, version_id: str) -> WorkflowDefinitionVersion:
    """Resolves a workflow version for a retry run. Unlike
    normal execution (which only ever runs the current 'approved' version),
    a retry may deliberately target a superseded version -- the Retry
    tab's version picker lists all versions a
    definition has ever had, and replaying an older-but-once-approved
    version against historical evidence is a legitimate diagnostic action.
    draft/pending_approval/rejected stay blocked since those were never
    vetted at all."""
    version = await get(db, WorkflowDefinitionVersion, version_id)
    if version is None:
        raise ValueError(f"No WorkflowDefinitionVersion {version_id!r}")
    if version.status not in RETRYABLE_VERSION_STATUSES:
        raise WorkflowValidationError(
            f"WorkflowDefinitionVersion {version_id!r} is {version.status!r}, "
            f"not one of {RETRYABLE_VERSION_STATUSES}"
        )
    return version


async def execute_and_record(
    db: AsyncDatabase,
    version: WorkflowDefinitionVersion,
    jira_key: str | None = None,
    incident_id: str | None = None,
    triggered_by: str = "manual_execute",
    mapping_overridden: bool = False,
    requested_by: str | None = None,
    operator_context: str | None = None,
    redis_client: Redis | None = None,
) -> WorkflowExecution:
    """Runs the interpreter and persists a reproducible record: a defensive
    snapshot of the exact document executed, the resulting evidence, and
    the synthesized RCA, all on the same row so the UI can read RCA
    outcomes without re-running the workflow.

    operator_context is operator-supplied free text from the
    Retry tab, persisted for audit and, once RCA_SYNTHESIS_LLM_ENABLED,
    passed through to the LLM prompt as its own labeled tier."""
    execution = WorkflowExecution(
        jira_key=jira_key,
        incident_id=incident_id,
        incident_key=await _incident_key_for(db, incident_id),
        workflow_definition_version_id=version.id,
        document_snapshot=version.document,
        triggered_by=triggered_by,
        mapping_overridden=mapping_overridden,
        requested_by=requested_by,
        operator_context=operator_context,
        status="running",
    )
    await insert(db, execution)

    try:
        evidence = await execute_workflow(version.document)
    except Exception:
        await update_fields(db, execution, {"status": "failed", "completed_at": utcnow()})
        raise

    # A confirmed burst against the same application is
    # itself diagnostic signal, checked ahead of this run's own evidence.
    correlation = None
    if incident_id is not None:
        incident = await get(db, Incident, incident_id)
        if incident is not None:
            group = await correlate_incident(db, incident)
            if group is not None:
                correlation = await build_correlation_context(db, group, exclude_incident_id=incident.id)

    completed = {"evidence": evidence, "status": "completed", "completed_at": utcnow()}
    if correlation is not None or not RCA_SYNTHESIS_LLM_ENABLED:
        # Correlated executions stay fully deterministic --
        # no LLM call, no queueing, unambiguous and free. Same when the
        # LLM-primary path is off: today's synchronous deterministic
        # synthesis, unchanged.
        rca = synthesize_rca(evidence, correlation=correlation)
        await update_fields(db, execution, {**completed, "rca": rca, "rca_status": rca["rca_status"]})
    else:
        # Not correlated, LLM-primary is on -- hand
        # off to app.rca_worker via Redis Stream rather than blocking this
        # request on an LLM call. rca/rca_status stay None ("pending")
        # until the worker writes them back. Written before publishing so
        # the worker's rca write can never be followed by this one; neither
        # write touches the other's fields anyway.
        await update_fields(db, execution, completed)
        redis = redis_client or make_redis()
        await publish_rca_pending(redis, execution.id, incident_id, jira_key, evidence, operator_context)

    return execution


async def record_unexecuted_attempt(
    db: AsyncDatabase,
    *,
    jira_key: str | None,
    incident_id: str | None,
    rca: dict,
    triggered_by: str = "rca",
    requested_by: str | None = None,
    operator_context: str | None = None,
) -> WorkflowExecution:
    """Records an RCA attempt that never reached an executable workflow --
    no playbook configured for the (source_system, category), or
    classification itself didn't resolve. This gives every RCA attempt
    exactly one WorkflowExecution row regardless of outcome, which is what
    the dashboard/stats/retry-history
    endpoints read latest-per-incident from."""
    execution = WorkflowExecution(
        jira_key=jira_key,
        incident_id=incident_id,
        incident_key=await _incident_key_for(db, incident_id),
        workflow_definition_version_id=None,
        document_snapshot=[],
        evidence=[],
        rca=rca,
        rca_status=rca["rca_status"],
        triggered_by=triggered_by,
        requested_by=requested_by,
        operator_context=operator_context,
        status="completed",
        completed_at=utcnow(),
    )
    return await insert(db, execution)


async def _incident_key_for(db: AsyncDatabase, incident_id: str | None) -> str | None:
    if incident_id is None:
        return None
    incident = await get(db, Incident, incident_id)
    return incident.incident_key if incident is not None else None


def no_playbook_rca(source_system_id: str, category: str) -> dict:
    return {
        "matched_pattern": "no_playbook",
        "rca_status": rca_status.NEED_MANUAL_INTERVENTION,
        "root_cause_summary": f"No RCA playbook configured yet for ({source_system_id}, {category}).",
        "contributing_factors": [],
        "recommended_actions": ["Author an RCA_PLAYBOOK for this (source_system, category) pair"],
    }


def not_classified_rca(classification_status: str) -> dict:
    return {
        "matched_pattern": "not_classified",
        "rca_status": rca_status.NEED_MANUAL_INTERVENTION,
        "root_cause_summary": f"Could not classify this incident (status={classification_status}).",
        "contributing_factors": [],
        "recommended_actions": ["Route for manual triage"],
    }


async def run_rca_for_incident(
    db: AsyncDatabase,
    incident: Incident,
    *,
    triggered_by: str,
    version: WorkflowDefinitionVersion | None = None,
    requested_by: str | None = None,
    operator_context: str | None = None,
) -> WorkflowExecution:
    """One RCA attempt for an already-classified incident, recorded as a
    WorkflowExecution whatever the outcome: runs `version` if given (a
    retry override), else the active playbook for the incident's
    (source_system, category); records 'no playbook' / 'not classified'
    when there is nothing to run. Shared by the worker's automatic RCA,
    retry, and POST /rca/{jira_key}."""
    if version is None:
        if incident.classification_status not in FULLY_CLASSIFIED_STATUSES:
            return await record_unexecuted_attempt(
                db,
                jira_key=incident.jira_key,
                incident_id=incident.id,
                rca=not_classified_rca(incident.classification_status),
                triggered_by=triggered_by,
                requested_by=requested_by,
                operator_context=operator_context,
            )
        version = await get_active_workflow(db, incident.source_system_id, incident.category)
        if version is None:
            return await record_unexecuted_attempt(
                db,
                jira_key=incident.jira_key,
                incident_id=incident.id,
                rca=no_playbook_rca(incident.source_system_id, incident.category),
                triggered_by=triggered_by,
                requested_by=requested_by,
                operator_context=operator_context,
            )
        mapping_overridden = False
    else:
        active = None
        if incident.classification_status in FULLY_CLASSIFIED_STATUSES:
            active = await get_active_workflow(db, incident.source_system_id, incident.category)
        mapping_overridden = active is None or version.id != active.id

    return await execute_and_record(
        db,
        version,
        jira_key=incident.jira_key,
        incident_id=incident.id,
        triggered_by=triggered_by,
        mapping_overridden=mapping_overridden,
        requested_by=requested_by,
        operator_context=operator_context,
    )
