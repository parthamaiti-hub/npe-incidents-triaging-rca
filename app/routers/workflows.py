"""REST API over app.workflow_orchestrator -- the same build -> render ->
approve/reject -> execute service scripts/build_workflow.py already drives
from the CLI, now reachable so a frontend can build/inspect workflows
without a terminal.
"""

import datetime
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import RCA_SYNTHESIS_LLM_ENABLED
from app.db import get_async_session
from app.embeddings import embed_feedback
from app.llm_client import make_openai_client
from app.models import (
    RcaFeedback,
    RcaPatternType,
    WorkflowBuildRequest,
    WorkflowDefinition,
    WorkflowDefinitionVersion,
    WorkflowExecution,
)
from app.workflow_orchestrator import (
    WorkflowConflictError,
    WorkflowValidationError,
    approve_build_request,
    build_workflow_from_request,
    edit_workflow_version,
    execute_and_record,
    get_active_workflow,
    preview_tasks,
    reject_build_request,
)
from app.workflow_yaml import ParsedWorkflow, WorkflowYamlError, cncf_yaml_to_tasks, tasks_to_cncf_yaml

router = APIRouter(prefix="/workflows", tags=["workflows"])


class WorkflowDefinitionOut(BaseModel):
    id: str
    source_system_id: str
    category: str
    model_config = {"from_attributes": True}


class WorkflowDefinitionVersionOut(BaseModel):
    id: str
    workflow_definition_id: str
    version_number: int
    document: list[dict]
    status: str
    source: str
    build_request_id: str | None
    created_by: str
    created_at: datetime.datetime
    approved_by: str | None
    approved_at: datetime.datetime | None
    model_config = {"from_attributes": True}


class WorkflowBuildRequestOut(BaseModel):
    id: str
    source_system_id: str
    category: str
    requested_by: str
    use_case_description: str | None
    requested_functions: list[dict]
    generated_document: list[dict] | None
    status: str
    created_at: datetime.datetime
    base_version_id: str | None = None
    model_config = {"from_attributes": True}


class WorkflowExecutionOut(BaseModel):
    id: str
    jira_key: str | None
    incident_id: str | None
    workflow_definition_version_id: str | None
    document_snapshot: list[dict]
    evidence: list[dict] | None
    rca: dict | None
    rca_status: str | None
    triggered_by: str
    requested_by: str | None
    mapping_overridden: bool
    status: str
    started_at: datetime.datetime
    completed_at: datetime.datetime | None
    # Operator-supplied hint for RCA synthesis, and the
    # audit trail (model/prompt version/retrieved context) for an
    # LLM-derived RCA. Both null on a deterministic-path execution.
    operator_context: str | None
    rca_meta: dict | None
    model_config = {"from_attributes": True}


class RcaFeedbackOut(BaseModel):
    id: str
    workflow_execution_id: str
    comment: str | None
    confidence_score: int
    given_by: str
    created_at: datetime.datetime
    corrected_pattern_id: str | None
    corrected_rca_status: str | None
    model_config = {"from_attributes": True}


class RcaPatternTypeOut(BaseModel):
    id: str
    description: str
    max_rca_status: str
    status: str
    model_config = {"from_attributes": True}


class RcaFeedbackIn(BaseModel):
    comment: str | None = None
    confidence_score: int = Field(ge=1, le=5)
    given_by: str
    # Optional structured correction -- cleaner RAG grounding
    # than free-text comment alone. Neither required; a plain comment+score
    # is still a fully valid feedback entry.
    corrected_pattern_id: str | None = None
    corrected_rca_status: str | None = None


class BuildWorkflowRequestIn(BaseModel):
    source_system_id: str
    category: str
    requested_functions: list[dict]
    requested_by: str
    use_case_description: str | None = None


class ApproveRequestIn(BaseModel):
    approved_by: str


class RejectRequestIn(BaseModel):
    rejected_by: str
    reason: str | None = None


class ExecuteRequestIn(BaseModel):
    jira_key: str | None = None
    operator_context: str | None = None


class WorkflowYamlOut(BaseModel):
    yaml: str


class RenderYamlIn(BaseModel):
    tasks: list[dict]
    name: str = "playbook"
    version: str = "draft"


class ValidateYamlIn(BaseModel):
    yaml: str
    # When given, the preview pins exactly as the eventual edit would.
    base_version_id: str | None = None


class ValidateYamlOut(BaseModel):
    tasks: list[dict]


class EditWorkflowIn(BaseModel):
    """Exactly one of `yaml` (from the YAML pane) or `tasks` (from
    graph edits) -- both compile to the same requested_functions list."""

    base_version_id: str
    yaml: str | None = None
    tasks: list[dict] | None = None
    edited_by: str
    change_note: str | None = None

    @model_validator(mode="after")
    def exactly_one_source(self) -> "EditWorkflowIn":
        if (self.yaml is None) == (self.tasks is None):
            raise ValueError("provide exactly one of 'yaml' or 'tasks'")
        return self


def _playbook_name(definition: WorkflowDefinition) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", definition.category.lower()).strip("-")
    return f"{definition.source_system_id}-{slug}"


def _yaml_422(exc: WorkflowYamlError) -> HTTPException:
    return HTTPException(
        422, {"kind": exc.kind, "message": str(exc), "errors": [e.as_dict() for e in exc.errors]}
    )


def _validation_422(exc: WorkflowValidationError, parsed: ParsedWorkflow | None) -> HTTPException:
    """Maps task-index errors from _validate_tasks back onto YAML lines when
    the tasks came from YAML, so the editor can mark the offending entry."""
    errors = []
    for e in exc.errors:
        i = e["task_index"]
        line = parsed.task_lines[i] if parsed is not None and i < len(parsed.task_lines) else None
        errors.append(
            {
                "message": e["message"],
                "path": f"do[{i}]" if parsed is not None else e["path"],
                "line": line,
                "column": None,
                "task_index": i,
            }
        )
    return HTTPException(422, {"kind": "registry", "message": str(exc), "errors": errors})


def _parse_yaml_or_422(text: str) -> ParsedWorkflow:
    try:
        return cncf_yaml_to_tasks(text)
    except WorkflowYamlError as exc:
        raise _yaml_422(exc)


@router.post("/build-requests", response_model=WorkflowBuildRequestOut, status_code=201)
async def create_build_request(payload: BuildWorkflowRequestIn, session: AsyncSession = Depends(get_async_session)):
    try:
        request = await build_workflow_from_request(
            session,
            payload.source_system_id,
            payload.category,
            payload.requested_functions,
            payload.requested_by,
            payload.use_case_description,
        )
    except WorkflowValidationError as exc:
        raise HTTPException(422, str(exc))
    return WorkflowBuildRequestOut.model_validate(request, from_attributes=True)


@router.get("/build-requests", response_model=list[WorkflowBuildRequestOut])
async def list_build_requests(
    status: str | None = None,
    source_system_id: str | None = None,
    session: AsyncSession = Depends(get_async_session),
):
    stmt = select(WorkflowBuildRequest).order_by(WorkflowBuildRequest.created_at.desc())
    if status is not None:
        stmt = stmt.where(WorkflowBuildRequest.status == status)
    if source_system_id is not None:
        stmt = stmt.where(WorkflowBuildRequest.source_system_id == source_system_id)
    rows = (await session.scalars(stmt)).all()
    return [WorkflowBuildRequestOut.model_validate(r, from_attributes=True) for r in rows]


@router.get("/build-requests/{request_id}", response_model=WorkflowBuildRequestOut)
async def get_build_request(request_id: str, session: AsyncSession = Depends(get_async_session)):
    row = await session.get(WorkflowBuildRequest, request_id)
    if row is None:
        raise HTTPException(404, f"WorkflowBuildRequest {request_id!r} not found")
    return WorkflowBuildRequestOut.model_validate(row, from_attributes=True)


@router.post("/build-requests/{request_id}/approve", response_model=WorkflowDefinitionVersionOut)
async def approve(request_id: str, payload: ApproveRequestIn, session: AsyncSession = Depends(get_async_session)):
    try:
        version = await approve_build_request(session, request_id, payload.approved_by)
    except WorkflowConflictError as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return WorkflowDefinitionVersionOut.model_validate(version, from_attributes=True)


@router.post("/build-requests/{request_id}/reject", status_code=204)
async def reject(request_id: str, payload: RejectRequestIn, session: AsyncSession = Depends(get_async_session)):
    try:
        await reject_build_request(session, request_id, payload.rejected_by, payload.reason)
    except ValueError as exc:
        raise HTTPException(404, str(exc))


@router.get("/definitions", response_model=list[WorkflowDefinitionOut])
async def list_definitions(session: AsyncSession = Depends(get_async_session)):
    rows = (await session.scalars(select(WorkflowDefinition))).all()
    return [WorkflowDefinitionOut.model_validate(r, from_attributes=True) for r in rows]


@router.get("/definitions/{definition_id}/versions", response_model=list[WorkflowDefinitionVersionOut])
async def list_versions(definition_id: str, session: AsyncSession = Depends(get_async_session)):
    if await session.get(WorkflowDefinition, definition_id) is None:
        raise HTTPException(404, f"WorkflowDefinition {definition_id!r} not found")
    rows = (
        await session.scalars(
            select(WorkflowDefinitionVersion)
            .where(WorkflowDefinitionVersion.workflow_definition_id == definition_id)
            .order_by(WorkflowDefinitionVersion.version_number)
        )
    ).all()
    return [WorkflowDefinitionVersionOut.model_validate(r, from_attributes=True) for r in rows]


@router.get("/active", response_model=WorkflowDefinitionVersionOut | None)
async def active_workflow(
    source_system_id: str, category: str, session: AsyncSession = Depends(get_async_session)
):
    version = await get_active_workflow(session, source_system_id, category)
    if version is None:
        return None
    return WorkflowDefinitionVersionOut.model_validate(version, from_attributes=True)


@router.get("/versions/{version_id}/yaml", response_model=WorkflowYamlOut)
async def version_yaml(version_id: str, session: AsyncSession = Depends(get_async_session)):
    """A stored version rendered as CNCF Serverless Workflow YAML
    for the playbook editor's YAML pane."""
    version = await session.get(WorkflowDefinitionVersion, version_id)
    if version is None:
        raise HTTPException(404, f"WorkflowDefinitionVersion {version_id!r} not found")
    definition = await session.get(WorkflowDefinition, version.workflow_definition_id)
    return WorkflowYamlOut(
        yaml=tasks_to_cncf_yaml(version.document, name=_playbook_name(definition), version=version.version_number)
    )


@router.post("/render-yaml", response_model=WorkflowYamlOut)
async def render_yaml(payload: RenderYamlIn):
    """Graph edits -> YAML pane. Pure conversion, no validation
    against the registry (the draft may be mid-edit)."""
    bad = [i for i, t in enumerate(payload.tasks) if not isinstance(t.get("call"), str)]
    if bad:
        raise HTTPException(422, f"tasks{bad}: every task needs a 'call'")
    return WorkflowYamlOut(yaml=tasks_to_cncf_yaml(payload.tasks, name=payload.name, version=payload.version))


@router.post("/validate-yaml", response_model=ValidateYamlOut)
async def validate_yaml(payload: ValidateYamlIn, session: AsyncSession = Depends(get_async_session)):
    """Dry run -- YAML syntax, supported-DSL shape, then the same
    registry/implementation checks a real build runs. Never persists. 422
    detail is {kind: syntax|schema|registry, message, errors: [{message,
    path, line, column, task_index}]}."""
    parsed = _parse_yaml_or_422(payload.yaml)
    base = None
    if payload.base_version_id is not None:
        base = await session.get(WorkflowDefinitionVersion, payload.base_version_id)
    try:
        tasks = preview_tasks(parsed.tasks, base)
    except WorkflowValidationError as exc:
        raise _validation_422(exc, parsed)
    return ValidateYamlOut(tasks=tasks)


@router.post("/definitions/{definition_id}/edits", response_model=WorkflowBuildRequestOut, status_code=201)
async def edit_definition(
    definition_id: str, payload: EditWorkflowIn, session: AsyncSession = Depends(get_async_session)
):
    """Saves an edit of the definition's approved version as a
    'rendered' build request (approve it via
    /build-requests/{id}/approve to publish version N+1). 409 if
    base_version_id is no longer the approved version."""
    parsed = _parse_yaml_or_422(payload.yaml) if payload.yaml is not None else None
    tasks = parsed.tasks if parsed is not None else payload.tasks
    try:
        request = await edit_workflow_version(
            session, definition_id, payload.base_version_id, tasks, payload.edited_by, payload.change_note
        )
    except WorkflowValidationError as exc:
        raise _validation_422(exc, parsed)
    except WorkflowConflictError as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(404, str(exc))
    return WorkflowBuildRequestOut.model_validate(request, from_attributes=True)


@router.post("/versions/{version_id}/execute", response_model=WorkflowExecutionOut)
async def execute_version(
    version_id: str, payload: ExecuteRequestIn, session: AsyncSession = Depends(get_async_session)
):
    """Runs an already-approved version on demand (independent of the
    classify-then-lookup path app.e2e_pipeline drives) -- useful for a
    frontend "re-run this workflow" action."""
    version = await session.get(WorkflowDefinitionVersion, version_id)
    if version is None:
        raise HTTPException(404, f"WorkflowDefinitionVersion {version_id!r} not found")
    if version.status != "approved":
        raise HTTPException(422, f"Version {version_id!r} is {version.status!r}, not 'approved'")
    execution = await execute_and_record(
        session, version, jira_key=payload.jira_key, triggered_by="manual_execute", operator_context=payload.operator_context
    )
    return WorkflowExecutionOut.model_validate(execution, from_attributes=True)


@router.get("/rca-patterns", response_model=list[RcaPatternTypeOut])
async def list_rca_patterns(session: AsyncSession = Depends(get_async_session)):
    """The RCA_PATTERN_TYPE catalog, for the feedback
    form's "correct pattern" dropdown -- active patterns only, so a
    retired one can't be newly selected as a correction (existing feedback
    rows that reference one keep displaying it via corrected_pattern_id
    regardless)."""
    rows = (await session.scalars(select(RcaPatternType).where(RcaPatternType.status == "active"))).all()
    return [RcaPatternTypeOut.model_validate(r, from_attributes=True) for r in rows]


@router.get("/executions", response_model=list[WorkflowExecutionOut])
async def list_executions(jira_key: str | None = None, session: AsyncSession = Depends(get_async_session)):
    stmt = select(WorkflowExecution).order_by(WorkflowExecution.started_at.desc())
    if jira_key is not None:
        stmt = stmt.where(WorkflowExecution.jira_key == jira_key)
    rows = (await session.scalars(stmt)).all()
    return [WorkflowExecutionOut.model_validate(r, from_attributes=True) for r in rows]


@router.get("/executions/{execution_id}", response_model=WorkflowExecutionOut)
async def get_execution(execution_id: str, session: AsyncSession = Depends(get_async_session)):
    row = await session.get(WorkflowExecution, execution_id)
    if row is None:
        raise HTTPException(404, f"WorkflowExecution {execution_id!r} not found")
    return WorkflowExecutionOut.model_validate(row, from_attributes=True)


@router.post("/executions/{execution_id}/feedback", response_model=RcaFeedbackOut, status_code=201)
async def add_feedback(
    execution_id: str, payload: RcaFeedbackIn, session: AsyncSession = Depends(get_async_session)
):
    """Operator feedback on one RCA attempt -- comment plus
    a 1-5 confidence score, plus an optional structured
    correction. Multiple entries per execution are allowed, a feedback
    thread rather than a single overwritable field."""
    execution = await session.get(WorkflowExecution, execution_id)
    if execution is None:
        raise HTTPException(404, f"WorkflowExecution {execution_id!r} not found")
    feedback = RcaFeedback(
        workflow_execution_id=execution_id,
        comment=payload.comment,
        confidence_score=payload.confidence_score,
        given_by=payload.given_by,
        corrected_pattern_id=payload.corrected_pattern_id,
        corrected_rca_status=payload.corrected_rca_status,
    )
    session.add(feedback)
    await session.commit()

    # Feeds the RAG corpus future RCA synthesis grounds on --
    # gated on RCA_SYNTHESIS_LLM_ENABLED (the feature that actually reads
    # this corpus), not just "always embed," so this never makes an
    # OpenAI call with no real key behind it while the feature is off.
    if RCA_SYNTHESIS_LLM_ENABLED:
        client = make_openai_client()
        await embed_feedback(session, client, feedback, execution)
        await session.commit()

    return RcaFeedbackOut.model_validate(feedback, from_attributes=True)


@router.get("/executions/{execution_id}/feedback", response_model=list[RcaFeedbackOut])
async def list_feedback(execution_id: str, session: AsyncSession = Depends(get_async_session)):
    if await session.get(WorkflowExecution, execution_id) is None:
        raise HTTPException(404, f"WorkflowExecution {execution_id!r} not found")
    rows = (
        await session.scalars(
            select(RcaFeedback)
            .where(RcaFeedback.workflow_execution_id == execution_id)
            .order_by(RcaFeedback.created_at)
        )
    ).all()
    return [RcaFeedbackOut.model_validate(r, from_attributes=True) for r in rows]
